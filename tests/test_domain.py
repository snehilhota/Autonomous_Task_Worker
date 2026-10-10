from datetime import date, timedelta

from env.domain import InventoryItem, PurchaseOrder, expiry_class, item_snapshot, order_snapshot

BUSINESS_DATE = date(2026, 10, 12)


def test_expiry_boundaries_and_missing_date():
    assert [expiry_class(day) for day in (-1, 0, 3, 4, 7, 8, None)] == [
        "expired", "critical", "critical", "warning", "warning", "fine", "none"
    ]


def test_expired_stock_is_unusable_and_overdue_order_is_not_incoming():
    item = InventoryItem("BREAD-SD", 8, 6, BUSINESS_DATE - timedelta(days=1))
    orders = [
        PurchaseOrder("BREAD-SD", 12, "Placed", BUSINESS_DATE - timedelta(days=1)),
        PurchaseOrder("BREAD-SD", 30, "Cancelled", BUSINESS_DATE + timedelta(days=1)),
    ]
    assert item_snapshot(item, orders, BUSINESS_DATE) == {
        "usable": 0, "incoming": 0, "effective": 0,
        "level": "OUT", "needs_order": True, "expiry": "expired",
    }


def test_incoming_order_can_cover_a_low_item_without_changing_low_alert():
    item = InventoryItem("SYR-CAR", on_hand=2, threshold=4)
    orders = [PurchaseOrder("SYR-CAR", 12, "Confirmed", BUSINESS_DATE + timedelta(days=1))]
    state = item_snapshot(item, orders, BUSINESS_DATE)
    assert state["level"] == "LOW"
    assert state["effective"] == 14
    assert state["needs_order"] is False


def test_order_overdue_only_when_open_and_before_business_date():
    assert order_snapshot(PurchaseOrder("CUP-12", 30, "Confirmed", BUSINESS_DATE - timedelta(days=1)), BUSINESS_DATE) == {
        "is_open": True, "overdue": True
    }
    assert order_snapshot(PurchaseOrder("CUP-12", 30, "Delivered", BUSINESS_DATE - timedelta(days=1)), BUSINESS_DATE) == {
        "is_open": False, "overdue": False
    }


def test_business_date_defaults_to_system_local_day_and_supports_override(monkeypatch):
    from env import clock

    class FakeDate:
        @classmethod
        def today(cls):
            return BUSINESS_DATE

        @classmethod
        def fromisoformat(cls, value):
            return date.fromisoformat(value)

    monkeypatch.delenv("BUSINESS_DATE", raising=False)
    monkeypatch.setattr(clock, "date", FakeDate)
    assert clock.get_business_date() == BUSINESS_DATE
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-15")
    assert clock.get_business_date() == date(2026, 10, 15)
