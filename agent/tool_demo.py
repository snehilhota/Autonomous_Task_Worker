"""Run a real, deterministic file-tool demonstration without an LLM."""

from pathlib import Path
from uuid import uuid4

from agent.executor import create_file_executor
from agent.tools.files import DocumentFileTool
from agent.trace import EventLog


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    event_log = EventLog(project_root / "runs" / "events.jsonl")
    executor = create_file_executor(DocumentFileTool(), event_log)
    run_id = f"file-demo-{uuid4().hex[:8]}"
    try:
        for step, (name, args) in enumerate([
            ("list_files", {"directory": "."}),
            ("list_files", {"directory": "supplier_notes"}),
            ("read_file", {"path": "ordering_policy.md"}),
        ], start=1):
            result = executor.run(name, args, run_id=run_id, step=step)
            print(f"{name}: {result.status.value} — {result.message or 'completed'}")
    finally:
        executor.close()
    print(f"Events written to {event_log.path}")


if __name__ == "__main__":
    main()
