"""Deterministic supplier and purchase-order policy (R7–R18)."""

from datetime import date, timedelta
import sqlite3
from typing import Any

from env.clock import get_business_date


OPEN_STATUSES = ("Placed", "Confirmed", "In Transit")
MAX_PO_VALUE_INR = 15_000
APPROVAL_VALUE_INR = 5_000


def _ceil_pack(quantity: int, pack_size: int) -> int:
    return ((quantity + pack_size - 1) // pack_size) * pack_size


def _arrival_date(order_date: date, lead_days: int, delivery_days: set[int]) -> date | None:
    candidate = order_date + timedelta(days=lead_days)
    # A valid delivery day is guaranteed by seeded data; the bound protects bad configuration.
    for _ in range(8):
        if candidate.weekday() != 6 and candidate.weekday() in delivery_days:
            return candidate
        candidate += timedelta(days=1)
    return None


def supplier_options(
    connection: sqlite3.Connection,
    sku: str,
    *,
    business_date: date | None = None,
    need_by: date | None = None,
) -> dict[str, Any]:
    """Return the deterministic recommended option for every supplier carrying SKU."""
    business_date = business_date or get_business_date()
    item = connection.execute(
        "SELECT * FROM inventory_items WHERE sku = ?", (sku,)
    ).fetchone()
    if item is None:
        return {"sku": sku, "decision": "blocked", "reason_codes": ["unknown_item"], "options": []}

    expiry = date.fromisoformat(item["expiry_date"]) if item["expiry_date"] else None
    usable = 0 if expiry is not None and expiry < business_date else item["on_hand"]
    orders = connection.execute(
        "SELECT quantity, status, expected_delivery FROM purchase_orders WHERE sku = ?", (sku,)
    ).fetchall()
    incoming = sum(
        order["quantity"] for order in orders
        if order["status"] in OPEN_STATUSES
        and date.fromisoformat(order["expected_delivery"]) >= business_date
    )
    effective = usable + incoming
    overdue_exists = any(
        order["status"] in OPEN_STATUSES
        and date.fromisoformat(order["expected_delivery"]) < business_date
        for order in orders
    )

    base_reasons = []
    if usable > item["threshold"]:
        base_reasons.append("stock_above_threshold")
    elif effective > item["threshold"]:
        base_reasons.append("already_covered")

    deadline = need_by or business_date + timedelta(days=2 if usable == 0 else 5)
    rows = connection.execute(
        """SELECT s.id, s.name, s.status, s.lead_days, s.minimum_order_value_inr,
                  c.unit_price_inr, c.pack_size
           FROM supplier_catalog c JOIN suppliers s ON s.id = c.supplier_id
           WHERE c.sku = ? ORDER BY s.id""", (sku,),
    ).fetchall()
    options = []
    for row in rows:
        supplier_id = row["id"]
        delivery_days = {
            entry[0] for entry in connection.execute(
                "SELECT weekday FROM supplier_delivery_days WHERE supplier_id = ?", (supplier_id,)
            ).fetchall()
        }
        arrival = _arrival_date(business_date, row["lead_days"], delivery_days)
        pack = row["pack_size"]
        min_qty = _ceil_pack(max(1, item["threshold"] - effective + 1), pack)
        stock_max = max(0, item["max_stock"] - effective)
        value_max = MAX_PO_VALUE_INR // row["unit_price_inr"]
        max_qty = (min(stock_max, value_max) // pack) * pack
        reasons = []
        if row["status"] != "active":
            reasons.append("supplier_not_active")
        if min_qty > max_qty:
            reasons.append("no_valid_quantity")
        quantity = min(max(_ceil_pack(max(0, item["target"] - effective), pack), min_qty), max_qty) if max_qty else 0
        total = quantity * row["unit_price_inr"]
        if quantity and total < row["minimum_order_value_inr"]:
            reasons.append("below_min_order_value")
        if arrival is None or arrival > deadline:
            reasons.append("delivery_too_late")
        options.append({
            "supplier_id": supplier_id, "supplier": row["name"], "status": row["status"],
            "quantity": quantity, "unit_price_inr": row["unit_price_inr"],
            "total_inr": total, "pack_size": pack, "min_qty": min_qty,
            "max_qty": max_qty, "arrival": arrival.isoformat() if arrival else None,
            "eligible": not reasons, "reason_codes": reasons,
        })

    eligible = [option for option in options if option["eligible"]]
    lowest_unit_price = min((option["unit_price_inr"] for option in eligible), default=None)
    for option in eligible:
        option["approval_reasons"] = []
        if option["total_inr"] > APPROVAL_VALUE_INR:
            option["approval_reasons"].append("order_value_over_5000")
        if lowest_unit_price is not None and option["unit_price_inr"] > 1.10 * lowest_unit_price:
            option["approval_reasons"].append("price_premium_over_10pct")
        if overdue_exists:
            option["approval_reasons"].append("overdue_order_exists")
        option["decision"] = "needs_approval" if option["approval_reasons"] else "permitted"
    preferred = min(eligible, key=lambda option: (
        option["unit_price_inr"], option["arrival"], option["supplier_id"]
    ), default=None)
    if base_reasons:
        decision = "blocked"
        reasons = base_reasons
    elif not eligible:
        decision = "blocked"
        reasons = ["no_eligible_supplier"]
    else:
        decision = preferred["decision"]
        reasons = list(preferred["approval_reasons"])

    return {
        "sku": sku, "usable": usable, "incoming": incoming, "effective": effective,
        "deadline": deadline.isoformat(), "decision": decision, "reason_codes": reasons,
        "preferred_supplier_id": preferred["supplier_id"] if preferred else None,
        "preferred_supplier": preferred,
        "options": options,
    }


def evaluate_order(
    connection: sqlite3.Connection,
    sku: str,
    supplier_id: int,
    quantity: int,
    *,
    business_date: date | None = None,
    need_by: date | None = None,
) -> dict[str, Any]:
    """Validate a proposed supplier/quantity and return its policy decision (R7–R18)."""
    business_date = business_date or get_business_date()
    result: dict[str, Any] = {
        "sku": sku, "supplier_id": supplier_id, "quantity": quantity,
        "decision": "blocked", "reason_codes": [], "approval_reasons": [],
    }
    item = connection.execute("SELECT * FROM inventory_items WHERE sku = ?", (sku,)).fetchone()
    if item is None:
        result["reason_codes"] = ["unknown_item"]
        return result

    expiry = date.fromisoformat(item["expiry_date"]) if item["expiry_date"] else None
    usable = 0 if expiry is not None and expiry < business_date else item["on_hand"]
    orders = connection.execute(
        "SELECT quantity, status, expected_delivery FROM purchase_orders WHERE sku = ?", (sku,)
    ).fetchall()
    incoming = sum(
        order["quantity"] for order in orders
        if order["status"] in OPEN_STATUSES
        and date.fromisoformat(order["expected_delivery"]) >= business_date
    )
    effective = usable + incoming
    overdue_exists = any(
        order["status"] in OPEN_STATUSES
        and date.fromisoformat(order["expected_delivery"]) < business_date
        for order in orders
    )
    result.update(usable=usable, incoming=incoming, effective=effective)

    hard_blocks = []
    if usable > item["threshold"]:
        hard_blocks.append("stock_above_threshold")
    elif effective > item["threshold"]:
        hard_blocks.append("already_covered")

    supplier = connection.execute(
        "SELECT * FROM suppliers WHERE id = ?", (supplier_id,)
    ).fetchone()
    catalog = connection.execute(
        "SELECT unit_price_inr, pack_size FROM supplier_catalog WHERE supplier_id = ? AND sku = ?",
        (supplier_id, sku),
    ).fetchone()
    if supplier is None:
        hard_blocks.append("unknown_supplier")
        result["reason_codes"] = hard_blocks
        return result
    if supplier["status"] != "active":
        hard_blocks.append("supplier_not_active")
    if catalog is None:
        hard_blocks.append("item_not_carried")
        result["reason_codes"] = hard_blocks
        return result

    unit_price = catalog["unit_price_inr"]
    pack_size = catalog["pack_size"]
    total = quantity * unit_price if isinstance(quantity, int) else None
    result.update(unit_price_inr=unit_price, pack_size=pack_size, total_inr=total)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        hard_blocks.append("qty_out_of_range")
    else:
        min_qty = _ceil_pack(max(1, item["threshold"] - effective + 1), pack_size)
        max_qty = (min(item["max_stock"] - effective, MAX_PO_VALUE_INR // unit_price) // pack_size) * pack_size
        result.update(min_qty=min_qty, max_qty=max(0, max_qty))
        if quantity > max_qty:
            hard_blocks.append("qty_out_of_range")
        elif quantity % pack_size:
            hard_blocks.append("qty_not_pack_multiple")
        elif quantity < min_qty:
            hard_blocks.append("qty_out_of_range")

    if total is not None and total < supplier["minimum_order_value_inr"]:
        hard_blocks.append("below_min_order_value")

    delivery_days = {
        row[0] for row in connection.execute(
            "SELECT weekday FROM supplier_delivery_days WHERE supplier_id = ?", (supplier_id,)
        ).fetchall()
    }
    arrival = _arrival_date(business_date, supplier["lead_days"], delivery_days)
    deadline = need_by or business_date + timedelta(days=2 if usable == 0 else 5)
    result.update(arrival=arrival.isoformat() if arrival else None, deadline=deadline.isoformat())
    if arrival is None or arrival > deadline:
        hard_blocks.append("delivery_too_late")

    if hard_blocks:
        result["reason_codes"] = list(dict.fromkeys(hard_blocks))
        return result

    # Compare the selected supplier against alternatives that can fulfil this same quantity
    # under the catalogue, stock, cap, MOV, active-status, and delivery rules.
    comparable = []
    alternatives = connection.execute(
        """SELECT s.id, s.status, s.lead_days, s.minimum_order_value_inr,
                  c.unit_price_inr, c.pack_size
           FROM supplier_catalog c JOIN suppliers s ON s.id = c.supplier_id
           WHERE c.sku = ?""", (sku,),
    ).fetchall()
    for alternative in alternatives:
        if alternative["status"] != "active" or quantity % alternative["pack_size"]:
            continue
        alternative_total = quantity * alternative["unit_price_inr"]
        if (alternative_total > MAX_PO_VALUE_INR
                or alternative_total < alternative["minimum_order_value_inr"]
                or effective + quantity > item["max_stock"]):
            continue
        days = {
            row[0] for row in connection.execute(
                "SELECT weekday FROM supplier_delivery_days WHERE supplier_id = ?",
                (alternative["id"],),
            ).fetchall()
        }
        alternative_arrival = _arrival_date(business_date, alternative["lead_days"], days)
        if alternative_arrival is not None and alternative_arrival <= deadline:
            comparable.append(alternative["unit_price_inr"])
    lowest_price = min(comparable, default=unit_price)

    approval_reasons = []
    if total > APPROVAL_VALUE_INR:
        approval_reasons.append("order_value_over_5000")
    if unit_price > 1.10 * lowest_price:
        approval_reasons.append("price_premium_over_10pct")
    if overdue_exists:
        approval_reasons.append("overdue_order_exists")
    result["approval_reasons"] = approval_reasons
    result["reason_codes"] = approval_reasons.copy()
    result["decision"] = "needs_approval" if approval_reasons else "permitted"
    return result
