import sqlite3

from env.db.seed import reset_database
from env.server import create_app


def test_order_form_submits_a_placed_po_and_shows_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    database = reset_database(tmp_path / "orders.sqlite3")
    response = create_app(database, tmp_path / "events.jsonl").test_client().post(
        "/new-purchase-order",
        data={"supplier_id": "3", "sku": "TEA-ASM", "quantity": "6",
              "expected_delivery": "2026-10-14"},
    )

    assert response.status_code == 201
    assert b"PO-1009" in response.data and b"Placed" in response.data
    assert "Estimated total: ₹3,120" in response.get_data(as_text=True)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sku, supplier_id, quantity, unit_price_inr, status, created_date, expected_delivery "
            "FROM purchase_orders WHERE po_number = 'PO-1009'"
        ).fetchone()
    assert row == ("TEA-ASM", 3, 6, 520, "Placed", "2026-10-12", "2026-10-14")


def test_order_form_stores_business_choices_for_the_policy_layer(tmp_path):
    database = reset_database(tmp_path / "orders.sqlite3")
    client = create_app(database, tmp_path / "events.jsonl").test_client()
    invalid_pack = client.post("/new-purchase-order", data={
        "supplier_id": "1", "sku": "MILK-FC", "quantity": "7",
        "expected_delivery": "2026-10-14",
    })
    suspended = client.post("/new-purchase-order", data={
        "supplier_id": "7", "sku": "MILK-FC", "quantity": "6",
        "expected_delivery": "2026-10-14",
    })

    assert invalid_pack.status_code == 201 and b"PO-1009" in invalid_pack.data
    assert suspended.status_code == 201 and b"PO-1010" in suspended.data
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT supplier_id, quantity, unit_price_inr FROM purchase_orders "
            "WHERE po_number IN ('PO-1009', 'PO-1010') ORDER BY po_number"
        ).fetchall()
    assert rows == [(1, 7, 62), (7, 6, 54)]


def test_order_form_still_rejects_non_numeric_or_unknown_records(tmp_path):
    database = reset_database(tmp_path / "orders.sqlite3")
    client = create_app(database, tmp_path / "events.jsonl").test_client()
    invalid_number = client.post("/new-purchase-order", data={
        "supplier_id": "1", "sku": "MILK-FC", "quantity": "six",
        "expected_delivery": "2026-10-14",
    })
    unknown_supplier = client.post("/new-purchase-order", data={
        "supplier_id": "999", "sku": "MILK-FC", "quantity": "6",
        "expected_delivery": "2026-10-14",
    })

    assert invalid_number.status_code == 400
    assert unknown_supplier.status_code == 400
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM purchase_orders").fetchone()[0] == 8


def test_order_form_get_shows_stable_browser_fields(tmp_path):
    database = reset_database(tmp_path / "orders.sqlite3")
    response = create_app(database, tmp_path / "events.jsonl").test_client().get("/new-purchase-order")

    assert response.status_code == 200
    assert b'data-testid="purchase-order-form"' in response.data
    assert b'data-testid="submit-order"' in response.data


def test_estimate_uses_supplier_catalogue_price_and_selected_quantity(tmp_path):
    database = reset_database(tmp_path / "orders.sqlite3")
    client = create_app(database, tmp_path / "events.jsonl").test_client()

    response = client.get("/order-estimate", query_string={
        "supplier_id": "3", "sku": "TEA-ASM", "quantity": "6",
    })

    assert response.status_code == 200
    assert response.json == {
        "unit_price_inr": 520, "quantity": 6, "estimated_total_inr": 3120,
        "pack_size": 1, "pack_valid": True,
    }
    suspended_quote = client.get("/order-estimate", query_string={
        "supplier_id": "7", "sku": "MILK-FC", "quantity": "6",
    })
    assert suspended_quote.status_code == 200
    assert suspended_quote.json["estimated_total_inr"] == 324


def test_test_fault_hooks_are_off_without_environment_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("BREW_ENABLE_TEST_FAULTS", raising=False)
    database = reset_database(tmp_path / "orders.sqlite3")
    app = create_app(database, tmp_path / "events.jsonl")
    app.config["NEXT_SUBMIT_FAULT"] = "fail_next_submit"

    response = app.test_client().post("/new-purchase-order", data={
        "supplier_id": "3", "sku": "TEA-ASM", "quantity": "6",
        "expected_delivery": "2026-10-14",
    })

    assert response.status_code == 201
    assert app.config["TEST_FAULTS_ENABLED"] is False
