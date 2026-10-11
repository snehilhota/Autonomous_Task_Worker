"""Independent post-write verification against a fresh system-of-record read."""

import json
from pathlib import Path
import sqlite3
from typing import Any

from agent.trace import EventLog


def verify_draft(database_path: str | Path, draft_id: str,
                 event_log_path: str | Path | None = None) -> dict[str, Any]:
    """Re-read the database independently and compare the exact expected write."""
    checks: dict[str, dict[str, Any]] = {}
    try:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            draft = connection.execute(
                "SELECT * FROM order_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if draft is None:
                checks["draft_exists"] = {"passed": False, "detail": "Draft not found."}
                return _finish(draft_id, checks, event_log_path)
            checks["draft_exists"] = {"passed": True, "detail": draft_id}
            baseline = json.loads(draft["baseline_purchase_orders_json"])
            baseline_by_id = {row["po_number"]: row for row in baseline}
            current_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM purchase_orders ORDER BY po_number"
            ).fetchall()]
            current_by_id = {row["po_number"]: row for row in current_rows}
            po_number = draft["submitted_po_number"]
            po = current_by_id.get(po_number) if po_number else None

            checks["po_exists"] = {
                "passed": po is not None,
                "detail": po_number or "Draft has no linked purchase order.",
            }
            matches = bool(po and (
                po["sku"] == draft["sku"]
                and po["supplier_id"] == draft["supplier_id"]
                and po["quantity"] == draft["quantity"]
                and po["unit_price_inr"] == draft["unit_price_inr"]
                and po["expected_delivery"] == draft["expected_delivery"]
                and po["status"] == "Placed"
                and po["created_by"] == "worker"
            ))
            checks["matches_draft"] = {
                "passed": matches,
                "detail": "PO fields match draft." if matches else "PO missing or fields differ from draft.",
            }

            new_order_ids = set(current_by_id) - set(baseline_by_id)
            exact_one = po_number is not None and new_order_ids == {po_number}
            checks["exactly_one_new_order"] = {
                "passed": exact_one,
                "detail": sorted(new_order_ids),
            }
            baseline_unchanged = all(current_by_id.get(key) == value
                                     for key, value in baseline_by_id.items())
            checks["baseline_unchanged"] = {
                "passed": baseline_unchanged,
                "detail": "All pre-existing purchase orders are unchanged."
                if baseline_unchanged else "A pre-existing purchase order changed or disappeared.",
            }
            correct_state = draft["status"] == "submitted" and draft["policy_decision"] in {
                "permitted", "needs_approval"
            }
            checks["draft_state"] = {
                "passed": correct_state,
                "detail": draft["status"],
            }
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        checks["database_read"] = {"passed": False, "detail": str(exc)}
        return _finish(draft_id, checks, event_log_path)
    return _finish(draft_id, checks, event_log_path)


def _finish(draft_id: str, checks: dict[str, dict[str, Any]],
            event_log_path: str | Path | None) -> dict[str, Any]:
    status = "verified" if checks and all(check["passed"] for check in checks.values()) else "failed"
    result = {"draft_id": draft_id, "status": status, "checks": checks}
    if event_log_path is not None:
        EventLog(event_log_path).append(
            "verification_finished", run_id=draft_id, step=None, result=result
        )
    return result
