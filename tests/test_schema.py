import sqlite3
from pathlib import Path


SCHEMA = Path(__file__).parents[1] / "env" / "db" / "schema.sql"


def make_database():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    return connection


def test_schema_creates_the_environment_tables():
    connection = make_database()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "inventory_items", "suppliers", "supplier_delivery_days",
        "supplier_catalog", "purchase_orders",
    } <= tables


def test_schema_rejects_sunday_delivery_and_invalid_order_status():
    connection = make_database()
    connection.execute("INSERT INTO inventory_items VALUES ('TEA', 'Tea', 'Tea', 'kg', 0, 1, 2, 4, NULL)")
    connection.execute("INSERT INTO suppliers VALUES (1, 'Supplier', 'active', 1, 100)")

    try:
        connection.execute("INSERT INTO supplier_delivery_days VALUES (1, 6)")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("Sunday deliveries must be rejected")

    try:
        connection.execute(
            "INSERT INTO purchase_orders VALUES ('PO-1009', 'TEA', 1, 1, 100, 'Draft', '2026-10-12', '2026-10-13', 'worker', NULL)"
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("Unknown order statuses must be rejected")
