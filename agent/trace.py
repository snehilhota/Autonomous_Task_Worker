"""Append-only JSONL event log with basic secret-field redaction."""

from datetime import datetime, timezone
from collections import deque
import json
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = {"api_key", "authorization", "password", "secret", "credentials"}
_RESERVED_KEYS = {"ts", "type", "run_id", "step"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.casefold() in _SENSITIVE_KEYS else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


class EventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event_type: str, *, run_id: str, step: int | None = None, **details: Any) -> dict[str, Any]:
        if not event_type or not run_id:
            raise ValueError("event_type and run_id are required")
        if _RESERVED_KEYS.intersection(details):
            raise ValueError("event details cannot replace reserved fields")
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "step": step,
            "type": event_type,
            **_redact(details),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as event_file:
            event_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read recent valid events, newest first; an absent log is an empty state."""
        if limit < 1 or not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as event_file:
            lines = deque(event_file, maxlen=limit)
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(events))
