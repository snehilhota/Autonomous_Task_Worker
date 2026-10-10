"""Create a fresh local Brew & Co. database from manual Appendix B."""

from datetime import date, timedelta
from pathlib import Path
import sqlite3
import sys


SEED_ANCHOR = date(2026, 10, 12)
SCHEMA = Path(__file__).with_name("schema.sql")

# expiry offsets are converted once from the manual's D=2026-10-12 anchor.
ITEMS = [
    ("MILK-FC", "Full-cream milk", "Dairy", "litre", 6, 20, 60, 80, 2),
    ("MILK-OAT", "Oat milk", "Dairy alternative", "litre", 18, 12, 36, 48, 7),
    ("CREAM", "Whipping cream", "Dairy", "litre", 3, 4, 12, 16, 1),
    ("BUTTER", "Butter", "Dairy", "kg", 9, 5, 15, 20, 20),
    ("BEANS-ESP", "Espresso beans", "Coffee", "kg", 4, 8, 24, 32, 60),
    ("BEANS-FIL", "Filter coffee beans", "Coffee", "kg", 12, 6, 18, 24, 75),
    ("TEA-ASM", "Assam tea", "Tea", "kg", 0, 2, 6, 8, None),
    ("TEA-GRN", "Green tea", "Tea", "kg", 2, 2, 6, 8, 150),
    ("SYR-VAN", "Vanilla syrup", "Syrup", "bottle", 5, 4, 12, 16, 200),
    ("SYR-CAR", "Caramel syrup", "Syrup", "bottle", 2, 4, 12, 16, 200),
    ("CUP-12", "12oz paper cups", "Packaging", "sleeve", 14, 20, 60, 100, None),
    ("LID-12", "12oz lids", "Packaging", "sleeve", 40, 20, 60, 100, None),
    ("BREAD-SD", "Sourdough loaf", "Bakery", "loaf", 8, 6, 24, 30, -1),
    ("CROIS", "Croissant", "Bakery", "piece", 12, 20, 60, 80, 4),
    ("BLUEB", "Blueberries", "Produce", "kg", 2, 3, 8, 10, 3),
    ("SUGAR", "Sugar", "Dry goods", "kg", 25, 10, 40, 60, None),
    ("COCOA", "Cocoa powder", "Dry goods", "kg", 5, 3, 10, 15, 240),
]

# supplier: (id, name, status, lead days, MOV, delivery weekdays; Mon=0, Sun=6)
SUPPLIERS = [
    (1, "DairyFresh Co.", "active", 1, 800, (0, 1, 2, 3, 4, 5)),
    (2, "Himalaya Roasters", "active", 3, 1500, (1, 4)),
    (3, "GreenLeaf Tea Traders", "active", 2, 600, (0, 1, 2, 3, 4, 5)),
    (4, "Metro Wholesale Mart", "active", 2, 2700, (0, 1, 2, 3, 4, 5)),
    (5, "PackRight Supplies", "active", 4, 1000, (0, 1, 2, 3, 4)),
    (6, "Sunrise Bakery Supply", "active", 1, 500, (0, 1, 2, 3, 4, 5)),
    (7, "Quick Dairy & More", "suspended", 1, 500, (0, 1, 2, 3, 4, 5)),
]

# Each catalogue row is (supplier_id, SKU, price per item unit, pack multiple).
CATALOG = [
    (1, "MILK-FC", 62, 6), (1, "MILK-OAT", 140, 6), (1, "CREAM", 340, 1), (1, "BUTTER", 560, 1),
    (2, "BEANS-ESP", 780, 1), (2, "BEANS-FIL", 700, 1), (2, "COCOA", 900, 1),
    (3, "TEA-ASM", 520, 1), (3, "TEA-GRN", 640, 1), (3, "SYR-VAN", 410, 6), (3, "SYR-CAR", 410, 6),
    (4, "MILK-FC", 58, 12), (4, "MILK-OAT", 130, 12), (4, "CREAM", 320, 2), (4, "BUTTER", 540, 5),
    (4, "BEANS-ESP", 820, 5), (4, "BEANS-FIL", 740, 5), (4, "TEA-ASM", 540, 1), (4, "TEA-GRN", 660, 1),
    (4, "SYR-VAN", 395, 12), (4, "SYR-CAR", 395, 12), (4, "CUP-12", 105, 20), (4, "LID-12", 60, 20),
    (4, "BLUEB", 480, 2), (4, "SUGAR", 52, 5), (4, "COCOA", 880, 5),
    (5, "CUP-12", 95, 10), (5, "LID-12", 55, 10),
    (6, "BREAD-SD", 85, 6), (6, "CROIS", 38, 12), (6, "BLUEB", 560, 1),
    (7, "MILK-FC", 54, 6), (7, "CREAM", 300, 1), (7, "BUTTER", 500, 1),
]

# Dates in these rows are offsets from SEED_ANCHOR; creator is unspecified in Appendix B.
ORDERS = [
    ("PO-1001", "SYR-CAR", 3, 12, 410, "Confirmed", -2, 1, "Covers caramel syrup"),
    ("PO-1002", "BEANS-ESP", 2, 3, 780, "Placed", -3, 4, "Partial cover for espresso"),
    ("PO-1003", "TEA-ASM", 3, 6, 520, "Cancelled", -5, -2, "Supplier could not fulfil; ignored"),
    ("PO-1004", "MILK-FC", 1, 42, 62, "Delivered", -4, -3, "History"),
    ("PO-1005", "BUTTER", 1, 10, 560, "Delivered", -7, -6, "History"),
    ("PO-1006", "LID-12", 5, 40, 55, "Delivered", -9, -5, "History"),
    ("PO-1007", "CUP-12", 5, 30, 95, "Confirmed", -7, -3, "Overdue"),
    ("PO-1008", "BEANS-FIL", 2, 6, 700, "In Transit", -3, 1, "Item is not low"),
]


def date_at_offset(offset: int) -> str:
    return (SEED_ANCHOR + timedelta(days=offset)).isoformat()


def reset_database(database_path: str | Path) -> Path:
    """Replace the requested demo database with the exact Appendix B seed."""
    target = Path(database_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with sqlite3.connect(target) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
        db.executemany(
            "INSERT INTO inventory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [row[:8] + (date_at_offset(row[8]) if row[8] is not None else None,) for row in ITEMS],
        )
        for supplier_id, name, status, lead, mov, weekdays in SUPPLIERS:
            db.execute("INSERT INTO suppliers VALUES (?, ?, ?, ?, ?)", (supplier_id, name, status, lead, mov))
            db.executemany(
                "INSERT INTO supplier_delivery_days VALUES (?, ?)",
                [(supplier_id, weekday) for weekday in weekdays],
            )
        db.executemany("INSERT INTO supplier_catalog VALUES (?, ?, ?, ?)", CATALOG)
        db.executemany(
            "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (po, sku, supplier, qty, price, status, date_at_offset(created),
                 date_at_offset(expected), None, note)
                for po, sku, supplier, qty, price, status, created, expected, note in ORDERS
            ],
        )
    return target


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("brew_demo.sqlite3")
    print(f"Reset demo database: {reset_database(destination)}")
