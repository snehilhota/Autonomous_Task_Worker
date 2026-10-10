from datetime import date
import sqlite3

from env.db.seed import reset_database
from env.domain import InventoryItem, PurchaseOrder, item_snapshot, order_snapshot


ANCHOR = date(2026, 10, 12)


def test_seed_matches_appendix_c_monitoring_snapshot(tmp_path):
    database = reset_database(tmp_path / "demo.sqlite3")
    with sqlite3.connect(database) as db:
        items = db.execute("SELECT sku, on_hand, threshold, expiry_date FROM inventory_items").fetchall()
        orders = db.execute(
            "SELECT po_number, sku, quantity, status, expected_delivery FROM purchase_orders"
        ).fetchall()
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("inventory_items", "suppliers", "supplier_catalog", "purchase_orders")
        }

    assert counts == {
        "inventory_items": 17, "suppliers": 7, "supplier_catalog": 34, "purchase_orders": 8
    }

    order_objects = [PurchaseOrder(sku, qty, status, date.fromisoformat(delivery))
                     for _, sku, qty, status, delivery in orders]
    states = {
        sku: item_snapshot(
            InventoryItem(sku, on_hand, threshold, date.fromisoformat(expiry) if expiry else None),
            order_objects,
            ANCHOR,
        )
        for sku, on_hand, threshold, expiry in items
    }
    assert {sku for sku, state in states.items() if state["level"] != "OK"} == {
        "MILK-FC", "CREAM", "BEANS-ESP", "TEA-ASM", "TEA-GRN", "SYR-CAR",
        "CUP-12", "BREAD-SD", "CROIS", "BLUEB",
    }
    assert {sku for sku, state in states.items() if state["needs_order"]} == {
        "MILK-FC", "CREAM", "BEANS-ESP", "TEA-ASM", "TEA-GRN",
        "CUP-12", "BREAD-SD", "CROIS", "BLUEB",
    }
    assert {sku for sku, state in states.items() if state["expiry"] == "expired"} == {"BREAD-SD"}
    assert {sku for sku, state in states.items() if state["expiry"] == "critical"} == {
        "CREAM", "MILK-FC", "BLUEB"
    }
    assert {sku for sku, state in states.items() if state["expiry"] == "warning"} == {
        "CROIS", "MILK-OAT"
    }
    assert {po for po, sku, qty, status, due in orders if status in {"Placed", "Confirmed", "In Transit"}} == {
        "PO-1001", "PO-1002", "PO-1007", "PO-1008"
    }
    assert {po for po, sku, qty, status, due in orders if order_snapshot(
        PurchaseOrder(sku, qty, status, date.fromisoformat(due)), ANCHOR
    )["overdue"]} == {"PO-1007"}
