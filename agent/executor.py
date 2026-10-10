"""Validate and run registered tools, recording each call and its result."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any

from agent.contracts import ToolResult, ToolStatus
from agent.registry import ToolRegistry, register_browser_tools, register_file_tools
from agent.trace import EventLog


def _validate_arguments(parameters: dict[str, Any], args: Any) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be an object"
    properties = parameters.get("properties", {})
    required = parameters.get("required", [])
    missing = [name for name in required if name not in args]
    unknown = set(args) - set(properties)
    if missing:
        return f"missing required argument: {missing[0]}"
    if unknown:
        return f"unknown argument: {sorted(unknown)[0]}"
    for name, value in args.items():
        expected_type = properties[name].get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"{name} must be a string"
    return None


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, event_log: EventLog, timeout_seconds: float = 5):
        self.registry = registry
        self.event_log = event_log
        self.timeout_seconds = timeout_seconds
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="brew-tool")

    def run(self, name: str, args: Any, *, run_id: str, step: int) -> ToolResult:
        self.event_log.append("tool_started", run_id=run_id, step=step, tool=name, args=args)
        tool = self.registry.get(name)
        if tool is None:
            result = ToolResult(ToolStatus.ERROR, error_code="unknown_tool", message=f"Unknown tool: {name}")
        else:
            problem = _validate_arguments(tool.parameters, args)
            if problem:
                result = ToolResult(ToolStatus.ERROR, error_code="invalid_args", message=problem)
            else:
                future = self._workers.submit(tool.handler, args)
                try:
                    result = future.result(timeout=self.timeout_seconds)
                    if not isinstance(result, ToolResult):
                        result = ToolResult(ToolStatus.ERROR, error_code="invalid_result", message="Tool returned an invalid result.")
                except TimeoutError:
                    future.cancel()
                    result = ToolResult(ToolStatus.ERROR, error_code="timeout", message="Tool exceeded its time limit.")
                except Exception as exc:
                    result = ToolResult(ToolStatus.ERROR, error_code="tool_error", message=str(exc))
        self.event_log.append(
            "tool_finished", run_id=run_id, step=step, tool=name, result=result.to_dict()
        )
        return result

    def close(self) -> None:
        self._workers.shutdown(wait=False, cancel_futures=True)


def create_file_executor(file_tool, event_log: EventLog, timeout_seconds: float = 5) -> ToolExecutor:
    registry = ToolRegistry()
    register_file_tools(registry, file_tool)
    return ToolExecutor(registry, event_log, timeout_seconds)


def create_browser_executor(browser_tool, event_log: EventLog, timeout_seconds: float = 10) -> ToolExecutor:
    registry = ToolRegistry()
    register_browser_tools(registry, browser_tool)
    return ToolExecutor(registry, event_log, timeout_seconds)
