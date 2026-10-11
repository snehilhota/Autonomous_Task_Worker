import sqlite3
from datetime import date

import pytest

from env.db.seed import reset_database
from env.ordering import evaluate_order, supplier_options


BUSINESS_DATE = date(2026, 10, 12)


@pytest.fixture
def database(tmp_path):
    path = reset_database(tmp_path / "brew.sqlite3")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("sku", "supplier_id", "quantity", "total", "arrival_offset", "decision", "reasons"),
    [
        ("TEA-ASM", 3, 6, 3120, 2, "permitted", []),
        ("MILK-FC", 4, 60, 3480, 2, "permitted", []),
        ("CREAM", 4, 10, 3200, 2, "permitted", []),
        ("BEANS-ESP", 2, 17, 13260, 4, "needs_approval", ["order_value_over_5000"]),
        ("TEA-GRN", 3, 4, 2560, 2, "permitted", []),
        ("CUP-12", 5, 50, 4750, 4, "needs_approval", ["overdue_order_exists"]),
        ("BREAD-SD", 6, 24, 2040, 1, "permitted", []),
        ("CROIS", 6, 48, 1824, 1, "permitted", []),
        ("BLUEB", 4, 6, 2880, 2, "permitted", []),
    ],
)
def test_preferred_supplier_matches_manual_golden_rows(
    database, sku, supplier_id, quantity, total, arrival_offset, decision, reasons
):
    result = supplier_options(database, sku, business_date=BUSINESS_DATE)
    preferred = result["preferred_supplier"]
    assert result["preferred_supplier_id"] == supplier_id
    assert preferred["quantity"] == quantity
    assert preferred["total_inr"] == total
    assert preferred["arrival"] == date.fromordinal(BUSINESS_DATE.toordinal() + arrival_offset).isoformat()
    assert result["decision"] == decision
    assert result["reason_codes"] == reasons


def test_supplier_specific_eligibility_and_price_premium(database):
    espresso = supplier_options(database, "TEA-GRN", business_date=BUSINESS_DATE)
    metro = next(option for option in espresso["options"] if option["supplier_id"] == 4)
    assert metro["eligible"] is False
    assert "below_min_order_value" in metro["reason_codes"]

    berries = supplier_options(database, "BLUEB", business_date=BUSINESS_DATE)
    sunrise = next(option for option in berries["options"] if option["supplier_id"] == 6)
    assert sunrise["decision"] == "needs_approval"
    assert sunrise["approval_reasons"] == ["price_premium_over_10pct"]


def test_coverage_and_suspended_supplier_are_blocked(database):
    covered = supplier_options(database, "SYR-CAR", business_date=BUSINESS_DATE)
    assert covered["decision"] == "blocked"
    assert covered["reason_codes"] == ["already_covered"]

    milk = supplier_options(database, "MILK-FC", business_date=BUSINESS_DATE)
    suspended = next(option for option in milk["options"] if option["supplier_id"] == 7)
    assert suspended["eligible"] is False
    assert suspended["reason_codes"] == ["supplier_not_active"]


def test_out_item_uses_two_day_deadline_and_unknown_item_is_blocked(database):
    tea = supplier_options(database, "TEA-ASM", business_date=BUSINESS_DATE)
    assert tea["deadline"] == "2026-10-14"
    unknown = supplier_options(database, "NOT-A-SKU", business_date=BUSINESS_DATE)
    assert unknown["decision"] == "blocked"
    assert unknown["reason_codes"] == ["unknown_item"]


@pytest.mark.parametrize(
    ("sku", "supplier_id", "quantity", "need_by", "reason"),
    [
        ("MILK-FC", 1, 100, None, "qty_out_of_range"),
        ("MILK-FC", 1, 7, None, "qty_not_pack_multiple"),
        ("MILK-FC", 7, 54, None, "supplier_not_active"),
        ("BEANS-ESP", 4, 20, None, "qty_out_of_range"),
        ("BEANS-ESP", 2, 17, date(2026, 10, 14), "delivery_too_late"),
        ("SYR-CAR", 3, 12, None, "already_covered"),
    ],
)
def test_selected_order_hard_blocks(database, sku, supplier_id, quantity, need_by, reason):
    decision = evaluate_order(
        database, sku, supplier_id, quantity,
        business_date=BUSINESS_DATE, need_by=need_by,
    )
    assert decision["decision"] == "blocked"
    assert reason in decision["reason_codes"]


def test_selected_order_decision_includes_approval_reasons(database):
    espresso = evaluate_order(database, "BEANS-ESP", 2, 17, business_date=BUSINESS_DATE)
    assert espresso["decision"] == "needs_approval"
    assert espresso["reason_codes"] == ["order_value_over_5000"]

    cups = evaluate_order(database, "CUP-12", 5, 50, business_date=BUSINESS_DATE)
    assert cups["reason_codes"] == ["overdue_order_exists"]

    berries = evaluate_order(database, "BLUEB", 6, 6, business_date=BUSINESS_DATE)
    assert berries["decision"] == "needs_approval"
    assert berries["reason_codes"] == ["price_premium_over_10pct"]


def test_supplier_choice_must_carry_item_and_meet_supplier_minimum(database):
    not_carried = evaluate_order(database, "CROIS", 5, 12, business_date=BUSINESS_DATE)
    assert not_carried["reason_codes"] == ["item_not_carried"]

    below_minimum = evaluate_order(database, "TEA-GRN", 4, 4, business_date=BUSINESS_DATE)
    assert below_minimum["reason_codes"] == ["below_min_order_value"]
