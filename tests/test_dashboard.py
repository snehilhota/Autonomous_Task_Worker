from env.db.seed import reset_database
from env.drafts import create_order_draft
from env.server import create_app
from agent.trace import EventLog


def test_dashboard_shows_snapshot_and_dd_mm_yyyy_dates(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    database = reset_database(tmp_path / "dashboard.sqlite3")
    event_path = tmp_path / "events.jsonl"
    EventLog(event_path).append("tool_finished", run_id="test-run", step=1,
                               tool="read_file", result={"status": "ok"})
    response = create_app(database, event_path).test_client().get("/")

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Business date: <strong>12-10-2026</strong>" in page
    assert "Low and out of stock" in page
    assert "Full-cream milk" in page
    assert "Sourdough loaf" in page
    assert "14-10-2026" in page  # milk expiry is D+2
    assert "PO-1007" in page and "Overdue" in page
    assert "Worker activity" in page and "read_file" in page


def test_dashboard_explains_how_to_create_missing_database(tmp_path):
    response = create_app(tmp_path / "missing.sqlite3", tmp_path / "no-events.jsonl").test_client().get("/")
    assert response.status_code == 503
    assert "python -m env.db.seed" in response.get_data(as_text=True)


def test_dashboard_shows_pending_draft_and_owner_can_approve_or_reject(tmp_path):
    from datetime import date
    import sqlite3

    database = reset_database(tmp_path / "approvals.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        draft = create_order_draft(connection, "BEANS-ESP", 2, 17,
                                   business_date=date(2026, 10, 12))
    client = create_app(database, tmp_path / "events.jsonl").test_client()
    page = client.get("/").get_data(as_text=True)
    assert draft["draft_id"] in page
    assert "order_value_over_5000" in page
    response = client.post(f"/drafts/{draft['draft_id']}/decision", data={"action": "approve"})
    assert response.status_code == 303
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM order_drafts WHERE draft_id = ?", (draft["draft_id"],)
        ).fetchone()[0] == "submitted"
        assert connection.execute(
            "SELECT submitted_po_number FROM order_drafts WHERE draft_id = ?", (draft["draft_id"],)
        ).fetchone()[0] == "PO-1009"
        assert connection.execute(
            "SELECT sku, supplier_id, quantity, unit_price_inr, status FROM purchase_orders "
            "WHERE po_number = 'PO-1009'"
        ).fetchone() == ("BEANS-ESP", 2, 17, 780, "Placed")
    assert "PO-1009" in client.get("/").get_data(as_text=True)

    rejected = None
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rejected = create_order_draft(connection, "CUP-12", 5, 50,
                                      business_date=date(2026, 10, 12))
    client.post(f"/drafts/{rejected['draft_id']}/decision", data={"action": "reject"})
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM order_drafts WHERE draft_id = ?", (rejected["draft_id"],)
        ).fetchone()[0] == "rejected"
        assert connection.execute("SELECT count(*) FROM purchase_orders").fetchone()[0] == 9
