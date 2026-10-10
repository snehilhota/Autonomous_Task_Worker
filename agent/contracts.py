"""Shared result shape returned by worker tools."""

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    UNKNOWN = "unknown"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    data: Any = None
    error_code: str | None = None
    message: str | None = None
    evidence: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON-ready tool contract."""
        result = asdict(self)
        result["status"] = self.status.value
        return result
