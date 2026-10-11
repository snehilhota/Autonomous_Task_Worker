from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import date
from threading import Thread

import pytest
from playwright.sync_api import expect, sync_playwright
from flask import Flask
from werkzeug.serving import make_server

from agent.contracts import ToolStatus
from agent.executor import create_browser_executor
from agent.tools.browser import BrewBrowserTool, OrderSubmissionDraft
from agent.trace import EventLog
from env.db.seed import reset_database
from env.drafts import create_order_draft, get_trusted_submission
from env.verifier import verify_draft
from env.server import create_app


@pytest.fixture
def local_brew_server(tmp_path):
    database = reset_database(tmp_path / "browser.sqlite3")
    app = create_app(database, tmp_path / "events.jsonl")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_real_browser_reads_seeded_dashboard(local_brew_server, tmp_path):
    log = EventLog(Path(tmp_path) / "events.jsonl")
    screenshot_dir = tmp_path / "screenshots"
    executor = create_browser_executor(
        BrewBrowserTool(local_brew_server, evidence_dir=screenshot_dir), log
    )
    try:
        result = executor.run("read_dashboard", {}, run_id="browser-test", step=1)
    finally:
        executor.close()

    assert result.status == ToolStatus.OK, result.to_dict()
    assert result.data["content_label"] == "UNTRUSTED PAGE CONTENT"
    assert "Full-cream milk" in result.data["content"]
    assert "PO-1007" in result.data["content"]
    assert result.evidence["url"] == f"{local_brew_server}/"
    screenshot = Path(result.evidence["screenshot"])
    assert screenshot.is_file() and screenshot.stat().st_size > 0
    assert [event["type"] for event in log.read_recent(limit=2)] == ["tool_finished", "tool_started"]


@pytest.mark.parametrize(("method", "expected_rows", "visible_value"), [
    ("read_inventory", 17, "MILK-FC"),
    ("read_suppliers", 34, "Quick Dairy & More"),
    ("read_purchase_orders", 8, "PO-1007"),
])
def test_real_browser_reads_full_seeded_data_pages(
    local_brew_server, tmp_path, method, expected_rows, visible_value
):
    tool = BrewBrowserTool(local_brew_server, evidence_dir=tmp_path / "shots")
    result = getattr(tool, method)()

    assert result.status == ToolStatus.OK
    assert len(result.data["rows"]) == expected_rows
    assert visible_value in " ".join(result.data["rows"])
    assert Path(result.evidence["screenshot"]).is_file()
    queries = {
        "read_inventory": "SELECT sku, name, category, unit, on_hand, threshold, target, max_stock, "
                           "COALESCE(strftime('%d-%m-%Y', expiry_date), '—') FROM inventory_items ORDER BY sku",
        "read_suppliers": "SELECT s.id, s.name, s.status, i.sku, i.name, c.unit_price_inr, c.pack_size, "
                          "s.minimum_order_value_inr, s.lead_days, group_concat(d.weekday, ',') "
                          "FROM supplier_catalog c JOIN suppliers s ON s.id=c.supplier_id "
                          "JOIN inventory_items i ON i.sku=c.sku LEFT JOIN supplier_delivery_days d "
                          "ON d.supplier_id=s.id GROUP BY s.id, i.sku ORDER BY s.id, i.sku",
        "read_purchase_orders": "SELECT p.po_number, p.sku, i.name, s.name, p.quantity, p.unit_price_inr, "
                               "p.status, strftime('%d-%m-%Y', p.created_date), "
                               "strftime('%d-%m-%Y', p.expected_delivery) FROM purchase_orders p "
                               "JOIN inventory_items i ON i.sku=p.sku JOIN suppliers s ON s.id=p.supplier_id "
                               "ORDER BY p.po_number",
    }
    with sqlite3.connect(tmp_path / "browser.sqlite3") as connection:
        expected = ["\t".join(str(value) for value in row)
                    for row in connection.execute(queries[method]).fetchall()]
    assert result.data["rows"] == expected


def test_order_form_shows_live_price_estimate_in_real_browser(local_brew_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{local_brew_server}/new-purchase-order")
            page.locator('[data-testid="supplier-id"]').select_option("3")
            page.locator('[data-testid="sku"]').select_option("TEA-ASM")
            page.locator('[data-testid="quantity"]').fill("6")
            expect(page.locator('[data-testid="price-estimate"]')).to_contain_text("Estimated total: ₹3,120")
        finally:
            browser.close()


def test_authorized_draft_is_submitted_through_real_browser(local_brew_server, tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    database = tmp_path / "browser.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        draft = create_order_draft(connection, "TEA-ASM", 3, 6, business_date=date(2026, 10, 12))

    def lookup_draft(requested_id):
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            return get_trusted_submission(connection, requested_id)

    def verify(requested_id):
        return verify_draft(database, requested_id, tmp_path / "events.jsonl")

    log = EventLog(tmp_path / "events.jsonl")
    executor = create_browser_executor(
        BrewBrowserTool(local_brew_server, evidence_dir=tmp_path / "shots",
                        draft_lookup=lambda draft_id: lookup_draft(draft_id)
                        if draft_id == draft["draft_id"] else None,
                        verification_callback=verify),
        log,
    )
    try:
        result = executor.run("submit_purchase_order", {"draft_id": draft["draft_id"]},
                              run_id="browser-write-test", step=1)
    finally:
        executor.close()

    assert result.status == ToolStatus.OK, result.to_dict()
    assert result.data["po_number"] == "PO-1009"
    assert result.data["verification"]["status"] == "verified"
    assert Path(result.evidence["screenshot"]).is_file()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sku, supplier_id, quantity, unit_price_inr, status, expected_delivery "
            "FROM purchase_orders WHERE po_number = 'PO-1009'"
        ).fetchone()
        submitted_draft = connection.execute(
            "SELECT status, submitted_po_number FROM order_drafts WHERE draft_id = ?",
            (draft["draft_id"],),
        ).fetchone()
    assert row == ("TEA-ASM", 3, 6, 520, "Placed", "2026-10-14")
    assert submitted_draft == ("submitted", "PO-1009")


def test_browser_write_requires_permitted_or_approved_draft():
    draft = OrderSubmissionDraft("needs_approval", 3, "TEA-ASM", 6, "2026-10-14", 520)
    result = BrewBrowserTool(draft_lookup=lambda _draft_id: draft).submit_purchase_order("tea-draft")

    assert result.status == ToolStatus.NEEDS_APPROVAL
    assert result.error_code == "approval_required"


def test_browser_write_rejects_a_draft_already_marked_submitted():
    draft = OrderSubmissionDraft("permitted", 3, "TEA-ASM", 6, "2026-10-14", 520,
                                 status="submitted")
    result = BrewBrowserTool(draft_lookup=lambda _draft_id: draft).submit_purchase_order("tea-draft")

    assert result.status == ToolStatus.BLOCKED
    assert result.error_code == "draft_already_submitted"


@pytest.mark.parametrize(("fault", "expected_status", "expected_po_count"), [
    ("fail_next_submit", ToolStatus.ERROR, 8),
    ("drop_response_after_commit", ToolStatus.UNKNOWN, 9),
])
def test_test_only_submit_faults_report_the_correct_outcome(
    tmp_path, monkeypatch, fault, expected_status, expected_po_count
):
    monkeypatch.setenv("BREW_ENABLE_TEST_FAULTS", "1")
    database = reset_database(tmp_path / "faults.sqlite3")
    app = create_app(database, tmp_path / "events.jsonl")
    app.config["NEXT_SUBMIT_FAULT"] = fault
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    draft = OrderSubmissionDraft("permitted", 3, "TEA-ASM", 6, "2026-10-14", 520)
    try:
        result = BrewBrowserTool(
            f"http://127.0.0.1:{server.server_port}", evidence_dir=tmp_path / "shots",
            draft_lookup=lambda _draft_id: draft,
        ).submit_purchase_order("fault-test")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.status == expected_status
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM purchase_orders").fetchone()[0] == expected_po_count


def test_browser_reports_missing_form_element_as_structured_error():
    app = Flask("missing-form")
    app.add_url_rule("/new-purchase-order", view_func=lambda: "<h1>Incomplete form</h1>")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    draft = OrderSubmissionDraft("permitted", 3, "TEA-ASM", 6, "2026-10-14", 520)
    try:
        result = BrewBrowserTool(
            f"http://127.0.0.1:{server.server_port}",
            draft_lookup=lambda _draft_id: draft,
        ).submit_purchase_order("missing-form")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.status == ToolStatus.ERROR
    assert result.error_code == "browser_error"


def test_browser_reports_missing_page_as_structured_error():
    app = Flask("missing-page")
    app.add_url_rule("/inventory", view_func=lambda: ("Page missing", 404))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = BrewBrowserTool(f"http://127.0.0.1:{server.server_port}").read_inventory()
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.status == ToolStatus.ERROR
    assert result.error_code == "page_unavailable"


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.5:5000", "http://127.0.0.1:5000/admin"])
def test_browser_refuses_nonlocal_or_nonroot_base_url(url):
    with pytest.raises(ValueError):
        BrewBrowserTool(url)
