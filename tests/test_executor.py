import json
import time

from agent.contracts import ToolResult, ToolStatus
from agent.executor import ToolExecutor, create_file_executor
from agent.registry import ToolRegistry, ToolSpec
from agent.tools.files import DocumentFileTool
from agent.trace import EventLog


def test_file_tool_call_is_validated_and_logged(tmp_path):
    (tmp_path / "policy.md").write_text("Policy text", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    executor = create_file_executor(DocumentFileTool(tmp_path), EventLog(events))
    result = executor.run("read_file", {"path": "policy.md"}, run_id="r1", step=1)
    executor.close()

    assert result.status == ToolStatus.OK
    assert result.data["text"] == "Policy text"
    logged = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in logged] == ["tool_started", "tool_finished"]


def test_unknown_tool_and_bad_arguments_return_structured_errors(tmp_path):
    executor = create_file_executor(DocumentFileTool(tmp_path), EventLog(tmp_path / "events.jsonl"))
    unknown = executor.run("shell", {}, run_id="r1", step=1)
    invalid = executor.run("read_file", {"other": "policy.md"}, run_id="r1", step=2)
    executor.close()
    assert unknown.error_code == "unknown_tool"
    assert invalid.error_code == "invalid_args"
    assert invalid.message == "missing required argument: path"


def test_timeout_is_reported_and_logged(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolSpec(
        "slow", "test tool", {"type": "object", "properties": {}, "required": []},
        lambda args: (time.sleep(0.05), ToolResult(ToolStatus.OK))[1],
    ))
    executor = ToolExecutor(registry, EventLog(tmp_path / "events.jsonl"), timeout_seconds=0.001)
    result = executor.run("slow", {}, run_id="r1", step=1)
    executor.close()
    assert result.status == ToolStatus.ERROR
    assert result.error_code == "timeout"
