"""Minimal Brew & Co. dashboard backed by the local SQLite system of record."""

from contextlib import closing
from datetime import date
import os
import json
from pathlib import Path
import sqlite3

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from env.clock import get_business_date
from env.domain import InventoryItem, PurchaseOrder, item_snapshot, order_snapshot
from env.drafts import DraftError, approve_and_place_order, approval_is_valid, record_human_decision
from agent.trace import EventLog
from agent.loop import TaskState, create_live_agent_loop, resume_approved_task


DEFAULT_DATABASE = Path(__file__).with_name("db") / "brew_demo.sqlite3"
DEFAULT_EVENT_LOG = Path(__file__).resolve().parents[1] / "runs" / "events.jsonl"


def create_app(
    database_path: str | Path = DEFAULT_DATABASE,
    event_log_path: str | Path = DEFAULT_EVENT_LOG,
    agent_factory=None,
) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(database_path)
    app.config["EVENT_LOG_PATH"] = str(event_log_path)
    app.config["TEST_FAULTS_ENABLED"] = os.getenv("BREW_ENABLE_TEST_FAULTS") == "1"
    app.config["NEXT_SUBMIT_FAULT"] = None
    app.config["AGENT_FACTORY"] = agent_factory or create_live_agent_loop
    app.config["EVIDENCE_DIR"] = str(Path(__file__).resolve().parents[1] / "runs" / "browser_evidence")
    app.add_template_filter(
        lambda value: date.fromisoformat(value).strftime("%d-%m-%Y"), "datetimeformat"
    )

    def ensure_task_store(db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_runs (
                run_id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                state_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_updated ON task_runs(updated_at DESC)")

    def save_task_state(state: TaskState) -> None:
        with closing(sqlite3.connect(app.config["DATABASE_PATH"])) as db:
            db.execute("""INSERT INTO task_runs(run_id, task, status, state_json)
                VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET task=excluded.task,
                status=excluded.status, state_json=excluded.state_json, updated_at=CURRENT_TIMESTAMP""",
                (state.run_id, state.task, state.status, json.dumps(state.to_dict(), ensure_ascii=False)))
            db.commit()

    def resume_saved_task(draft_id: str, answer: str | None = None) -> None:
        db_path = Path(app.config["DATABASE_PATH"])
        ensure_task_store(db_path)
        with closing(sqlite3.connect(db_path)) as db:
            rows = db.execute("SELECT state_json FROM task_runs WHERE status IN ('waiting_for_approval','waiting_for_user')").fetchall()
        saved = None
        for row in rows:
            try:
                candidate = json.loads(row[0])
            except json.JSONDecodeError:
                continue
            matches = (candidate.get("draft_id") == draft_id if answer is None
                       else candidate.get("run_id") == draft_id)
            if matches:
                saved = candidate
                break
        if saved is None:
            return
        state = TaskState.from_dict(saved)
        loop = executor = None
        try:
            if answer is None:
                updated = resume_approved_task(
                    state, db_path, EventLog(app.config["EVENT_LOG_PATH"])
                )
            else:
                loop, executor = app.config["AGENT_FACTORY"](
                    db_path, app.config["EVENT_LOG_PATH"]
                )
                updated = loop.resume(state, answer)
            save_task_state(updated)
        except Exception as exc:
            app.config["LAST_TASK_ERROR"] = str(exc)
        finally:
            if executor is not None:
                executor.close()

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

        ensure_task_store(db_path)

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            items = connection.execute("SELECT * FROM inventory_items ORDER BY sku").fetchall()
            orders = connection.execute("SELECT * FROM purchase_orders ORDER BY po_number").fetchall()
            pending_drafts = []
            try:
                pending = connection.execute(
                    """SELECT d.*, i.name AS item_name, i.unit, s.name AS supplier_name
                       FROM order_drafts d JOIN inventory_items i ON i.sku = d.sku
                       JOIN suppliers s ON s.id = d.supplier_id
                       WHERE d.status = 'awaiting_approval' ORDER BY d.created_at"""
                ).fetchall()
                for draft in pending:
                    view = dict(draft)
                    view["reason_codes"] = json.loads(view["reason_codes_json"])
                    view["total_inr"] = view["quantity"] * view["unit_price_inr"]
                    pending_drafts.append(view)
            except sqlite3.OperationalError:
                # Allows the dashboard to explain setup before an older demo DB is reset.
                pass

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

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            tasks = [dict(row) for row in connection.execute(
                "SELECT run_id, task, status, state_json, created_at, updated_at "
                "FROM task_runs ORDER BY updated_at DESC LIMIT 12"
            ).fetchall()]
        for task in tasks:
            try:
                task["state"] = json.loads(task.pop("state_json"))
            except json.JSONDecodeError:
                task["state"] = {}

        visible_events = []
        for event in events:
            item = dict(event)
            result = item.get("result") or {}
            evidence = result.get("evidence") if isinstance(result, dict) else None
            screenshot = evidence.get("screenshot") if isinstance(evidence, dict) else None
            if screenshot:
                root = Path(app.config["EVIDENCE_DIR"]).resolve()
                path = Path(screenshot).resolve()
                if path.is_relative_to(root) and path.is_file():
                    item["screenshot_url"] = url_for("evidence_file", filename=path.name)
            visible_events.append(item)

        return render_template(
            "dashboard.html", business_date=business_date, setup_error=False,
            low_items=low_items, expiry_items=expiry_items, open_orders=open_orders,
            pending_drafts=pending_drafts, events=visible_events, tasks=tasks,
            task_error=app.config.pop("LAST_TASK_ERROR", None),
        )

    @app.post("/tasks")
    def start_task():
        task = request.form.get("task", "").strip()
        if not task:
            app.config["LAST_TASK_ERROR"] = "Enter a task before starting the worker."
            return redirect(url_for("dashboard"), code=303)
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return "Demo database not found.", 503
        ensure_task_store(db_path)
        loop = executor = None
        try:
            loop, executor = app.config["AGENT_FACTORY"](db_path, app.config["EVENT_LOG_PATH"])
            state = loop.start(task)
            save_task_state(state)
        except Exception as exc:
            app.config["LAST_TASK_ERROR"] = str(exc)
        finally:
            if executor is not None:
                executor.close()
        return redirect(url_for("dashboard"), code=303)

    @app.post("/tasks/<run_id>/answer")
    def answer_task(run_id: str):
        answer = request.form.get("answer", "").strip()
        if not answer:
            app.config["LAST_TASK_ERROR"] = "Enter an answer before resuming the task."
        else:
            resume_saved_task(run_id, answer)
        return redirect(url_for("dashboard"), code=303)

    @app.get("/evidence/<path:filename>")
    def evidence_file(filename: str):
        root = Path(app.config["EVIDENCE_DIR"]).resolve()
        target = (root / filename).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix.casefold() != ".png":
            abort(404)
        return send_file(target, mimetype="image/png")

    @app.post("/drafts/<draft_id>/decision")
    def decide_draft(draft_id: str):
        db_path = Path(app.config["DATABASE_PATH"])
        if not db_path.exists():
            return "Demo database not found.", 503
        try:
            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                action = request.form.get("action", "")
                if action == "approve":
                    approve_and_place_order(connection, draft_id, actor="owner")
                else:
                    record_human_decision(connection, draft_id, action, actor="owner")
        except DraftError as exc:
            return str(exc), 400
        resume_saved_task(draft_id)
        return redirect(url_for("dashboard"), code=303)

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
                draft_id = request.args.get("draft_id", "").strip()
                draft = None
                if draft_id:
                    draft = connection.execute(
                        "SELECT * FROM order_drafts WHERE draft_id = ?", (draft_id,)
                    ).fetchone()
                    if draft is None or draft["status"] not in {"ready", "approved"}:
                        return "This draft is not ready to submit.", 409
                    if draft["policy_decision"] == "needs_approval" and not approval_is_valid(connection, draft_id):
                        return "A valid owner approval is required before submission.", 403
                return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                       error=None, draft=draft, draft_id=draft_id)
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
            draft_id = request.form.get("draft_id", "").strip() or None
            if draft_id:
                try:
                    draft = connection.execute(
                        "SELECT * FROM order_drafts WHERE draft_id = ?", (draft_id,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    draft = None
                if draft is None:
                    return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                           error="The submitted draft could not be found."), 400
                if draft["status"] not in {"ready", "approved"}:
                    return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                           error="This draft is not ready to submit."), 409
                if draft["policy_decision"] == "needs_approval" and not approval_is_valid(connection, draft_id):
                    return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                           error="A valid owner approval is required before submission."), 403
                if (draft["sku"], draft["supplier_id"], draft["quantity"], draft["unit_price_inr"],
                        draft["expected_delivery"]) != (
                        sku, supplier_id, quantity, catalog["unit_price_inr"], expected_delivery.isoformat()):
                    return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                           error="Submitted values do not match the trusted draft."), 400
                if draft["status"] == "submitted" or draft["submitted_po_number"]:
                    return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                           error="This draft already created a purchase order."), 409
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
            if draft_id:
                connection.execute(
                    "UPDATE order_drafts SET status = 'submitted', submitted_po_number = ? "
                    "WHERE draft_id = ? AND submitted_po_number IS NULL",
                    (po_number, draft_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    connection.rollback()
                    return render_template("purchase_order_form.html", suppliers=suppliers, items=items,
                                           error="This draft already created a purchase order."), 409
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
