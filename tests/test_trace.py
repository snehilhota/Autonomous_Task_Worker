import json

from agent.contracts import ToolResult, ToolStatus
from agent.trace import EventLog


def test_tool_result_has_the_shared_shape():
    assert ToolResult(ToolStatus.ERROR, error_code="timeout").to_dict() == {
        "status": "error", "data": None, "error_code": "timeout",
        "message": None, "evidence": None,
    }


def test_event_log_appends_jsonl_and_redacts_sensitive_fields(tmp_path):
    path = tmp_path / "runs" / "events.jsonl"
    log = EventLog(path)
    log.append(
        "tool_finished", run_id="run-1", step=2,
        tool="read_file", api_key="do-not-log", outcome={"password": "hidden", "status": "ok"},
    )

    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["type"] == "tool_finished"
    assert event["run_id"] == "run-1" and event["step"] == 2
    assert event["api_key"] == "[REDACTED]"
    assert event["outcome"] == {"password": "[REDACTED]", "status": "ok"}
    assert "do-not-log" not in path.read_text(encoding="utf-8")


def test_event_log_reads_recent_events_newest_first(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("tool_started", run_id="r1", step=1, tool="list_files")
    log.append("tool_finished", run_id="r1", step=1, tool="list_files")
    assert [event["type"] for event in log.read_recent(limit=1)] == ["tool_finished"]
