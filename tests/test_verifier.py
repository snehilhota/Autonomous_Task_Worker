import sqlite3
from datetime import date

import pytest

from env.db.seed import reset_database
from env.drafts import create_order_draft
from env.verifier import verify_draft


BUSINESS_DATE = date(2026, 10, 12)


@pytest.fixture
def database(tmp_path):
    path = reset_database(tmp_path / "verify.sqlite3")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield path, connection
    finally:
        connection.close()


def make_draft(connection):
    return create_order_draft(connection, "TEA-ASM", 3, 6, business_date=BUSINESS_DATE)


def commit_po(connection, draft, *, po_number="PO-1009", quantity=6, unit_price=520):
    connection.execute(
        "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, 'Placed', ?, ?, 'worker', NULL)",
        (po_number, draft["sku"], draft["supplier_id"], quantity, unit_price,
         "2026-10-12", draft["expected_delivery"]),
    )
    connection.execute(
        "UPDATE order_drafts SET status = 'submitted', submitted_po_number = ? WHERE draft_id = ?",
        (po_number, draft["draft_id"]),
    )
    connection.commit()


def test_verifier_confirms_one_matching_order_and_unchanged_baseline(database, tmp_path):
    path, connection = database
    draft = make_draft(connection)
    commit_po(connection, draft)

    result = verify_draft(path, draft["draft_id"], tmp_path / "events.jsonl")

    assert result["status"] == "verified"
    assert all(check["passed"] for check in result["checks"].values())
    assert result["checks"]["exactly_one_new_order"]["detail"] == ["PO-1009"]


def test_verifier_catches_mismatched_order_and_unrelated_new_order(database):
    path, connection = database
    draft = make_draft(connection)
    commit_po(connection, draft, quantity=12)
    result = verify_draft(path, draft["draft_id"])
    assert result["status"] == "failed"
    assert result["checks"]["matches_draft"]["passed"] is False

    connection.execute(
        "INSERT INTO purchase_orders VALUES ('PO-1010', 'TEA-GRN', 3, 4, 640, 'Placed', "
        "'2026-10-12', '2026-10-14', 'worker', NULL)"
    )
    connection.commit()
    result = verify_draft(path, draft["draft_id"])
    assert result["checks"]["exactly_one_new_order"]["passed"] is False


def test_verifier_catches_changed_preexisting_order_and_unsubmitted_draft(database):
    path, connection = database
    draft = make_draft(connection)
    connection.execute("UPDATE purchase_orders SET quantity = 11 WHERE po_number = 'PO-1001'")
    connection.commit()
    result = verify_draft(path, draft["draft_id"])
    assert result["status"] == "failed"
    assert result["checks"]["baseline_unchanged"]["passed"] is False

    another = make_draft(connection)
    no_write = verify_draft(path, another["draft_id"])
    assert no_write["checks"]["po_exists"]["passed"] is False
