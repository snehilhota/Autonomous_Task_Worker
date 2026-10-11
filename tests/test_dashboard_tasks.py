import json
import sqlite3
from datetime import date

from agent.loop import TaskState
from agent.trace import EventLog
from env.db.seed import reset_database
from env.drafts import create_order_draft
from env.verifier import verify_draft
from env.server import create_app


class FakeExecutor:
    def close(self):
        pass


class DashboardFakeLoop:
    def __init__(self, database_path, event_path):
        self.database_path = database_path
        self.log = EventLog(event_path)

    def start(self, task):
        self.log.append("run_started", run_id="dashboard-run", step=0, task=task)
        if "espresso" in task.casefold():
            with sqlite3.connect(self.database_path) as db:
                db.row_factory = sqlite3.Row
                draft = create_order_draft(db, "BEANS-ESP", 2, 17,
                                           business_date=date(2026, 10, 12))
            state = TaskState("dashboard-run", task, status="waiting_for_approval",
                              draft_id=draft["draft_id"], report="Owner approval is required.")
        else:
            state = TaskState("dashboard-run", task, status="waiting_for_user",
                              question="Which tea should I reorder?")
        self.log.append("run_paused", run_id=state.run_id, step=0, reason=state.status)
        return state

    def resume(self, state, answer=None):
        if state.status == "waiting_for_user":
            state.status, state.question = "completed", None
            state.report = f"Clarification received: {answer}"
        else:
            state.verification = verify_draft(self.database_path, state.draft_id, self.log.path)
            state.status = "completed" if state.verification["status"] == "verified" else "verification_failed"
            state.report = "Approved order verified." if state.status == "completed" else "Verification failed."
        self.log.append("run_finished", run_id=state.run_id, step=state.step,
                        status=state.status, report=state.report)
        return state


def fake_factory(database_path, event_path):
    return DashboardFakeLoop(database_path, event_path), FakeExecutor()


def test_dashboard_starts_task_and_resumes_clarification_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    database = reset_database(tmp_path / "dashboard.sqlite3")
    event_path = tmp_path / "events.jsonl"
    client = create_app(database, event_path, agent_factory=fake_factory).test_client()

    response = client.post("/tasks", data={"task": "Order more tea."})
    assert response.status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert "waiting_for_user" in page
    assert "Which tea should I reorder?" in page

    response = client.post("/tasks/dashboard-run/answer", data={"answer": "Assam tea"})
    assert response.status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert "Clarification received: Assam tea" in page
    assert "completed" in page
    with sqlite3.connect(database) as db:
        state = json.loads(db.execute(
            "SELECT state_json FROM task_runs WHERE run_id='dashboard-run'"
        ).fetchone()[0])
    assert state["status"] == "completed"


def test_dashboard_approval_resumes_and_shows_verified_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    database = reset_database(tmp_path / "dashboard.sqlite3")
    client = create_app(database, tmp_path / "events.jsonl", agent_factory=fake_factory).test_client()

    client.post("/tasks", data={"task": "Restock espresso beans."})
    with sqlite3.connect(database) as db:
        draft_id = db.execute("SELECT draft_id FROM order_drafts").fetchone()[0]
    page = client.get("/").get_data(as_text=True)
    assert draft_id in page
    response = client.post(f"/drafts/{draft_id}/decision", data={"action": "approve"})
    assert response.status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert "Owner approved the proposed order; independent verification passed." in page
    assert "Verification: <span class=\"status\">verified" in page
    with sqlite3.connect(database) as db:
        state = json.loads(db.execute(
            "SELECT state_json FROM task_runs WHERE run_id='dashboard-run'"
        ).fetchone()[0])
        assert state["status"] == "completed"


def test_dashboard_displays_and_serves_only_saved_png_evidence(tmp_path):
    database = reset_database(tmp_path / "dashboard.sqlite3")
    evidence_dir = tmp_path / "browser_evidence"
    evidence_dir.mkdir()
    image_path = evidence_dir / "run-proof.png"
    image_path.write_bytes(b"png-test-evidence")
    event_path = tmp_path / "events.jsonl"
    EventLog(event_path).append("tool_finished", run_id="evidence-run", step=2,
                               tool="submit_purchase_order",
                               result={"status": "ok", "evidence": {"screenshot": str(image_path)}})
    app = create_app(database, event_path)
    app.config["EVIDENCE_DIR"] = str(evidence_dir)
    client = app.test_client()

    page = client.get("/").get_data(as_text=True)
    assert "/evidence/run-proof.png" in page
    response = client.get("/evidence/run-proof.png")
    assert response.status_code == 200
    assert response.data == b"png-test-evidence"
    assert client.get("/evidence/../events.jsonl").status_code == 404
