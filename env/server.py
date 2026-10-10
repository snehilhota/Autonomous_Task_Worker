"""Minimal Brew & Co. dashboard backed by the local SQLite system of record."""

from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3

from flask import Flask, render_template

from env.clock import get_business_date
from env.domain import InventoryItem, PurchaseOrder, item_snapshot, order_snapshot


DEFAULT_DATABASE = Path(__file__).with_name("db") / "brew_demo.sqlite3"


def create_app(database_path: str | Path = DEFAULT_DATABASE) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(database_path)
    app.add_template_filter(
        lambda value: date.fromisoformat(value).strftime("%d-%m-%Y"), "datetimeformat"
    )

    @app.get("/")
    def dashboard():
        business_date = get_business_date()
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return render_template(
                "dashboard.html", business_date=business_date, setup_error=True,
                low_items=[], expiry_items=[], open_orders=[],
            ), 503

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            items = connection.execute("SELECT * FROM inventory_items ORDER BY sku").fetchall()
            orders = connection.execute("SELECT * FROM purchase_orders ORDER BY po_number").fetchall()

        order_models = [
            PurchaseOrder(row["sku"], row["quantity"], row["status"],
                          date.fromisoformat(row["expected_delivery"]))
            for row in orders
        ]
        low_items = []
        expiry_items = []
        for row in items:
            expiry_date = date.fromisoformat(row["expiry_date"]) if row["expiry_date"] else None
            item = InventoryItem(row["sku"], row["on_hand"], row["threshold"], expiry_date)
            view = {**dict(row), **item_snapshot(item, order_models, business_date)}
            if view["level"] != "OK":
                low_items.append(view)
            if view["expiry"] in {"expired", "critical", "warning"}:
                expiry_items.append(view)

        open_orders = []
        for row in orders:
            expected = date.fromisoformat(row["expected_delivery"])
            state = order_snapshot(
                PurchaseOrder(row["sku"], row["quantity"], row["status"], expected), business_date
            )
            if state["is_open"]:
                open_orders.append({**dict(row), **state})

        return render_template(
            "dashboard.html", business_date=business_date, setup_error=False,
            low_items=low_items, expiry_items=expiry_items, open_orders=open_orders,
        )

    return app


if __name__ == "__main__":
    create_app().run(debug=False)
