"""Deterministic Brew & Co. rules that do not depend on the language model."""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from env.clock import get_business_date


with Path(__file__).with_name("policy.json").open(encoding="utf-8") as policy_file:
    POLICY = json.load(policy_file)


@dataclass(frozen=True)
class InventoryItem:
    sku: str
    on_hand: int
    threshold: int
    expiry_date: date | None = None


@dataclass(frozen=True)
class PurchaseOrder:
    sku: str
    quantity: int
    status: str
    expected_delivery: date


def expiry_class(days_left: int | None) -> str:
    """Classify expiry relative to BUSINESS_DATE; None means no expiry date."""
    if days_left is None:
        return "none"
    if days_left < 0:
        return "expired"
    if days_left <= POLICY["critical_expiry_max_days"]:
        return "critical"
    if days_left <= POLICY["warning_expiry_max_days"]:
        return "warning"
    return "fine"


def item_snapshot(
    item: InventoryItem,
    orders: list[PurchaseOrder],
    business_date: date | None = None,
) -> dict[str, int | str | bool]:
    """Return stock and ordering indicators for one item (R1–R6)."""
    business_date = business_date or get_business_date()
    days_left = (item.expiry_date - business_date).days if item.expiry_date else None
    usable = 0 if days_left is not None and days_left < 0 else item.on_hand
    open_statuses = POLICY["open_order_statuses"]
    incoming = sum(
        order.quantity
        for order in orders
        if order.sku == item.sku
        and order.status in open_statuses
        and order.expected_delivery >= business_date
    )
    effective = usable + incoming
    level = "OUT" if usable == 0 else "LOW" if usable <= item.threshold else "OK"
    return {
        "usable": usable,
        "incoming": incoming,
        "effective": effective,
        "level": level,
        "needs_order": effective <= item.threshold,
        "expiry": expiry_class(days_left),
    }


def order_snapshot(order: PurchaseOrder, business_date: date | None = None) -> dict[str, bool]:
    """Mark whether an order is open and whether its delivery is overdue (R3–R4)."""
    business_date = business_date or get_business_date()
    is_open = order.status in POLICY["open_order_statuses"]
    return {"is_open": is_open, "overdue": is_open and order.expected_delivery < business_date}
