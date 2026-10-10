from env.db.seed import reset_database
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
