"""Minimal Brew & Co. dashboard backed by the local SQLite system of record."""

from contextlib import closing
from datetime import date
import os
from pathlib import Path
import sqlite3

from flask import Flask, jsonify, render_template, request

from env.clock import get_business_date
from env.domain import InventoryItem, PurchaseOrder, item_snapshot, order_snapshot
from agent.trace import EventLog


DEFAULT_DATABASE = Path(__file__).with_name("db") / "brew_demo.sqlite3"
DEFAULT_EVENT_LOG = Path(__file__).resolve().parents[1] / "runs" / "events.jsonl"


def create_app(
    database_path: str | Path = DEFAULT_DATABASE,
    event_log_path: str | Path = DEFAULT_EVENT_LOG,
) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(database_path)
    app.config["EVENT_LOG_PATH"] = str(event_log_path)
    app.config["TEST_FAULTS_ENABLED"] = os.getenv("BREW_ENABLE_TEST_FAULTS") == "1"
    app.config["NEXT_SUBMIT_FAULT"] = None
    app.add_template_filter(
        lambda value: date.fromisoformat(value).strftime("%d-%m-%Y"), "datetimeformat"
    )

    @app.get("/")
    def dashboard():
        business_date = get_business_date()
        events = EventLog(app.config["EVENT_LOG_PATH"]).read_recent()
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return render_template(
                "dashboard.html", business_date=business_date, setup_error=True,
                low_items=[], expiry_items=[], open_orders=[], events=events,
            ), 503

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
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
            low_items=low_items, expiry_items=expiry_items, open_orders=open_orders, events=events,
        )

    def data_page(title: str, test_id: str, query: str):
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return render_template("data_page.html", title=title, test_id=test_id,
                                   rows=[], setup_error=True), 503
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(query).fetchall()]
        return render_template("data_page.html", title=title, test_id=test_id,
                               rows=rows, setup_error=False)

    @app.get("/inventory")
    def inventory_page():
        return data_page("Inventory", "inventory-row", """
            SELECT sku, name, category, unit, on_hand, threshold, target, max_stock,
                   COALESCE(strftime('%d-%m-%Y', expiry_date), '—') AS expiry_date
            FROM inventory_items ORDER BY sku
        """)

    @app.get("/suppliers")
    def suppliers_page():
        return data_page("Supplier catalogue", "supplier-row", """
            SELECT s.id AS supplier_id, s.name AS supplier, s.status,
                   i.sku, i.name AS item, c.unit_price_inr, c.pack_size,
                   s.minimum_order_value_inr AS minimum_order_value,
                   s.lead_days, group_concat(d.weekday, ',') AS delivery_days
            FROM supplier_catalog c
            JOIN suppliers s ON s.id = c.supplier_id
            JOIN inventory_items i ON i.sku = c.sku
            LEFT JOIN supplier_delivery_days d ON d.supplier_id = s.id
            GROUP BY s.id, i.sku ORDER BY s.id, i.sku
        """)

    @app.get("/purchase-orders")
    def purchase_orders_page():
        return data_page("Purchase orders", "purchase-order-row", """
            SELECT p.po_number, p.sku, i.name AS item, s.name AS supplier,
                   p.quantity, p.unit_price_inr, p.status,
                   strftime('%d-%m-%Y', p.created_date) AS created_date,
                   strftime('%d-%m-%Y', p.expected_delivery) AS expected_delivery
            FROM purchase_orders p
            JOIN inventory_items i ON i.sku = p.sku
            JOIN suppliers s ON s.id = p.supplier_id
            ORDER BY p.po_number
        """)

    @app.get("/order-estimate")
    def order_estimate():
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return jsonify(error="Demo database not found."), 503
        try:
            supplier_id = int(request.args["supplier_id"])
            sku = request.args["sku"]
            quantity = int(request.args.get("quantity") or 0)
            if quantity < 0:
                raise ValueError
        except (KeyError, ValueError):
            return jsonify(error="Choose a supplier and item, and enter a valid quantity."), 400
        with closing(sqlite3.connect(db_path)) as connection:
            catalog = connection.execute(
                "SELECT unit_price_inr, pack_size FROM supplier_catalog "
                "WHERE supplier_id = ? AND sku = ?",
                (supplier_id, sku),
            ).fetchone()
        if not catalog:
            return jsonify(error="No catalogue price for this supplier and item."), 404
        unit_price, pack_size = catalog
        return jsonify(
            unit_price_inr=unit_price, quantity=quantity,
            estimated_total_inr=unit_price * quantity if quantity else None,
            pack_size=pack_size, pack_valid=not quantity or quantity % pack_size == 0,
        )

    @app.route("/new-purchase-order", methods=["GET", "POST"])
    def new_purchase_order():
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return render_template("purchase_order_form.html", suppliers=[], items=[], error="Demo database not found."), 503
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            suppliers = connection.execute(
                "SELECT id, name, status FROM suppliers ORDER BY id"
            ).fetchall()
            items = connection.execute("SELECT sku, name FROM inventory_items ORDER BY sku").fetchall()
            if request.method == "GET":
                return render_template("purchase_order_form.html", suppliers=suppliers, items=items, error=None)
            try:
                supplier_id = int(request.form["supplier_id"])
                sku = request.form["sku"]
                quantity = int(request.form["quantity"])
                expected_delivery = date.fromisoformat(request.form["expected_delivery"])
            except (KeyError, ValueError):
                return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                       error="Enter a supplier, item, positive quantity, and valid delivery date."), 400
            catalog = connection.execute(
                "SELECT unit_price_inr FROM supplier_catalog WHERE supplier_id = ? AND sku = ?",
                (supplier_id, sku),
            ).fetchone()
            if not catalog:
                return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                       error="No catalogue price exists for that supplier and item."), 400
            if quantity <= 0:
                return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                       error="Quantity must be a positive whole number."), 400
            latest = connection.execute(
                "SELECT po_number FROM purchase_orders ORDER BY po_number DESC LIMIT 1"
            ).fetchone()
            next_number = int(latest["po_number"].split("-")[1]) + 1 if latest else 1001
            po_number = f"PO-{next_number}"
            test_fault = app.config.get("NEXT_SUBMIT_FAULT") if app.config["TEST_FAULTS_ENABLED"] else None
            if test_fault == "fail_next_submit":
                app.config["NEXT_SUBMIT_FAULT"] = None
                return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                       error="Test-only failure before the order was saved."), 503
            connection.execute(
                "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, 'Placed', ?, ?, 'worker', NULL)",
                (po_number, sku, supplier_id, quantity, catalog["unit_price_inr"], get_business_date().isoformat(),
                 expected_delivery.isoformat()),
            )
            connection.commit()
            if test_fault == "drop_response_after_commit":
                app.config["NEXT_SUBMIT_FAULT"] = None
                return "Test-only response failure after commit.", 504
        return render_template("purchase_order_form.html", receipt=po_number,
                               unit_price=catalog["unit_price_inr"], quantity=quantity,
                               estimated_total=catalog["unit_price_inr"] * quantity), 201

    return app


if __name__ == "__main__":
    create_app().run(debug=False)
