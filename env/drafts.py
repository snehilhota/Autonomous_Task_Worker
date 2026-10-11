"""Persistent order drafts and human approval records."""

from dataclasses import dataclass
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from env.ordering import evaluate_order


class DraftError(ValueError):
    """A requested draft transition is not allowed."""


@dataclass(frozen=True)
class TrustedSubmission:
    draft_id: str
    decision: str
    supplier_id: int
    sku: str
    quantity: int
    expected_delivery: str
    unit_price_inr: int
    approval_valid: bool
    status: str


def _draft_hash(fields: dict) -> str:
    stable = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(stable.encode("utf-8")).hexdigest()


def create_order_draft(
    connection: sqlite3.Connection,
    sku: str,
    supplier_id: int,
    quantity: int,
    *,
    need_by=None,
    business_date=None,
) -> dict:
    """Evaluate and persist an immutable proposal; policy values come from Python/DB."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise DraftError("A draft needs a positive whole-number quantity.")
    if connection.execute("SELECT 1 FROM inventory_items WHERE sku = ?", (sku,)).fetchone() is None:
        raise DraftError("A draft needs a known inventory item.")
    if connection.execute("SELECT 1 FROM suppliers WHERE id = ?", (supplier_id,)).fetchone() is None:
        raise DraftError("A draft needs a known supplier.")
    decision = evaluate_order(
        connection, sku, supplier_id, quantity,
        business_date=business_date, need_by=need_by,
    )
    if decision["decision"] == "blocked":
        # Blocked attempts are still stored so the owner can see why no write was made.
        expected_delivery = decision.get("arrival") or decision.get("deadline")
        unit_price = decision.get("unit_price_inr") or 1
    else:
        expected_delivery = decision["arrival"]
        unit_price = decision["unit_price_inr"]

    if not expected_delivery:
        # The schema needs a date even when the supplier was unknown; use the evaluation day.
        from env.clock import get_business_date
        expected_delivery = (business_date or get_business_date()).isoformat()

    payload = {
        "sku": sku,
        "supplier_id": supplier_id,
        "quantity": quantity,
        "unit_price_inr": unit_price,
        "expected_delivery": expected_delivery,
        "policy_decision": decision["decision"],
        "reason_codes": decision["reason_codes"],
    }
    draft_hash = _draft_hash(payload)
    baseline_orders = [dict(row) for row in connection.execute(
        "SELECT * FROM purchase_orders ORDER BY po_number"
    ).fetchall()]
    draft_id = f"D-{uuid4().hex}"
    status = {
        "permitted": "ready",
        "needs_approval": "awaiting_approval",
        "blocked": "blocked",
    }[decision["decision"]]
    with connection:
        connection.execute(
            """INSERT INTO order_drafts
               (draft_id, sku, supplier_id, quantity, unit_price_inr, expected_delivery,
                policy_decision, reason_codes_json, decision_json, baseline_purchase_orders_json,
                draft_hash, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, sku, supplier_id, quantity, unit_price, expected_delivery,
             decision["decision"], json.dumps(decision["reason_codes"]),
             json.dumps(decision, sort_keys=True), json.dumps(baseline_orders, sort_keys=True),
             draft_hash, status),
        )
    return {"draft_id": draft_id, **payload, "draft_hash": draft_hash,
            "status": status, "decision": decision}


def record_human_decision(
    connection: sqlite3.Connection,
    draft_id: str,
    action: str,
    *,
    actor: str = "owner",
) -> dict:
    """Record an owner approval/rejection for the exact current draft hash."""
    if action not in {"approve", "reject"}:
        raise DraftError("Decision must be approve or reject.")
    draft = connection.execute(
        "SELECT draft_hash, policy_decision, status FROM order_drafts WHERE draft_id = ?",
        (draft_id,),
    ).fetchone()
    if draft is None:
        raise DraftError("Draft not found.")
    if draft["policy_decision"] != "needs_approval" or draft["status"] != "awaiting_approval":
        raise DraftError("Only a draft awaiting approval can receive an owner decision.")
    with connection:
        connection.execute(
            "INSERT INTO draft_approvals (draft_id, draft_hash, action, actor) VALUES (?, ?, ?, ?)",
            (draft_id, draft["draft_hash"], action, actor),
        )
        status = "approved" if action == "approve" else "rejected"
        connection.execute("UPDATE order_drafts SET status = ? WHERE draft_id = ?", (status, draft_id))
    return {"draft_id": draft_id, "draft_hash": draft["draft_hash"],
            "action": action, "status": status}


def approve_and_place_order(
    connection: sqlite3.Connection,
    draft_id: str,
    *,
    actor: str = "owner",
    business_date=None,
) -> str:
    """Atomically record owner approval and place the exact approved draft as a PO."""
    from env.clock import get_business_date

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        draft = connection.execute(
            "SELECT * FROM order_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise DraftError("Draft not found.")
        if draft["policy_decision"] != "needs_approval" or draft["status"] != "awaiting_approval":
            raise DraftError("Only a draft awaiting approval can be approved and placed.")

        fields = {
            "sku": draft["sku"],
            "supplier_id": draft["supplier_id"],
            "quantity": draft["quantity"],
            "unit_price_inr": draft["unit_price_inr"],
            "expected_delivery": draft["expected_delivery"],
            "policy_decision": draft["policy_decision"],
            "reason_codes": json.loads(draft["reason_codes_json"]),
        }
        if _draft_hash(fields) != draft["draft_hash"]:
            raise DraftError("Draft details changed after evaluation; refusing to place the order.")

        latest = connection.execute(
            "SELECT po_number FROM purchase_orders ORDER BY po_number DESC LIMIT 1"
        ).fetchone()
        next_number = int(latest["po_number"].split("-")[1]) + 1 if latest else 1001
        po_number = f"PO-{next_number}"
        connection.execute(
            "INSERT INTO draft_approvals (draft_id, draft_hash, action, actor) VALUES (?, ?, 'approve', ?)",
            (draft_id, draft["draft_hash"], actor),
        )
        connection.execute(
            """INSERT INTO purchase_orders
               (po_number, sku, supplier_id, quantity, unit_price_inr, status,
                created_date, expected_delivery, created_by, notes)
               VALUES (?, ?, ?, ?, ?, 'Placed', ?, ?, 'worker', NULL)""",
            (po_number, draft["sku"], draft["supplier_id"], draft["quantity"],
             draft["unit_price_inr"], (business_date or get_business_date()).isoformat(),
             draft["expected_delivery"]),
        )
        cursor = connection.execute(
            "UPDATE order_drafts SET status = 'submitted', submitted_po_number = ? "
            "WHERE draft_id = ? AND status = 'awaiting_approval'",
            (po_number, draft_id),
        )
        if cursor.rowcount != 1:
            raise DraftError("Draft is no longer awaiting approval.")
    return po_number


def approval_is_valid(connection: sqlite3.Connection, draft_id: str) -> bool:
    row = connection.execute(
        """SELECT d.draft_hash, d.status, a.action, a.draft_hash AS approved_hash
           FROM order_drafts d LEFT JOIN draft_approvals a ON a.draft_id = d.draft_id
           WHERE d.draft_id = ? ORDER BY a.approval_id DESC LIMIT 1""",
        (draft_id,),
    ).fetchone()
    return bool(row and row["status"] == "approved" and row["action"] == "approve"
                and row["approved_hash"] == row["draft_hash"])


def get_trusted_submission(connection: sqlite3.Connection, draft_id: str) -> TrustedSubmission | None:
    """Load browser-submit fields from storage after rechecking state and approval."""
    row = connection.execute(
        """SELECT draft_id, policy_decision, supplier_id, sku, quantity,
                  expected_delivery, unit_price_inr, status
           FROM order_drafts WHERE draft_id = ?""",
        (draft_id,),
    ).fetchone()
    if row is None:
        return None
    approved = approval_is_valid(connection, draft_id)
    return TrustedSubmission(
        draft_id=row["draft_id"], decision=row["policy_decision"],
        supplier_id=row["supplier_id"], sku=row["sku"], quantity=row["quantity"],
        expected_delivery=row["expected_delivery"], unit_price_inr=row["unit_price_inr"],
        approval_valid=approved, status=row["status"],
    )


def mark_draft_submitted(connection: sqlite3.Connection, draft_id: str, po_number: str) -> None:
    """Atomically bind one created PO to a draft; unique FK prevents duplicate binding."""
    row = get_trusted_submission(connection, draft_id)
    if row is None:
        raise DraftError("Draft not found.")
    if row.status == "submitted":
        raise DraftError("draft_already_submitted")
    po = connection.execute(
        """SELECT sku, supplier_id, quantity, unit_price_inr, expected_delivery, status
           FROM purchase_orders WHERE po_number = ?""",
        (po_number,),
    ).fetchone()
    if po is None or (po["sku"], po["supplier_id"], po["quantity"], po["unit_price_inr"],
                      po["expected_delivery"], po["status"]) != (
            row.sku, row.supplier_id, row.quantity, row.unit_price_inr,
            row.expected_delivery, "Placed"):
        raise DraftError("The purchase order does not match this draft.")
    if row.decision == "blocked" or row.status in {"blocked", "rejected"}:
        raise DraftError("Draft is blocked or rejected.")
    if row.decision == "needs_approval" and not row.approval_valid:
        raise DraftError("A valid owner approval is required.")
    if row.status != "ready" and row.status != "approved":
        raise DraftError("Draft is not ready for submission.")
    with connection:
        cursor = connection.execute(
            "UPDATE order_drafts SET status = 'submitted', submitted_po_number = ? "
            "WHERE draft_id = ? AND status = ?",
            (po_number, draft_id, row.status),
        )
        if cursor.rowcount != 1:
            raise DraftError("draft_already_submitted")
