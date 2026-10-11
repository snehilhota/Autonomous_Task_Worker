import sqlite3
from datetime import date

import pytest

from env.db.seed import reset_database
from env.drafts import (
    DraftError,
    approval_is_valid,
    create_order_draft,
    get_trusted_submission,
    mark_draft_submitted,
    record_human_decision,
)


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


def test_permitted_draft_is_saved_with_python_computed_policy(database):
    draft = create_order_draft(database, "TEA-ASM", 3, 6, business_date=BUSINESS_DATE)
    assert draft["status"] == "ready"
    assert draft["decision"]["decision"] == "permitted"
    stored = get_trusted_submission(database, draft["draft_id"])
    assert stored.sku == "TEA-ASM"
    assert stored.quantity == 6
    assert stored.expected_delivery == "2026-10-14"
    assert stored.approval_valid is False


def test_approval_is_bound_to_the_exact_draft_hash(database):
    draft = create_order_draft(database, "BEANS-ESP", 2, 17, business_date=BUSINESS_DATE)
    assert draft["status"] == "awaiting_approval"
    decision = record_human_decision(database, draft["draft_id"], "approve")
    assert decision["status"] == "approved"
    assert approval_is_valid(database, draft["draft_id"])
    assert get_trusted_submission(database, draft["draft_id"]).approval_valid

    database.execute("UPDATE order_drafts SET draft_hash = 'changed' WHERE draft_id = ?",
                     (draft["draft_id"],))
    database.commit()
    assert approval_is_valid(database, draft["draft_id"]) is False
    assert get_trusted_submission(database, draft["draft_id"]).approval_valid is False


def test_rejection_and_duplicate_human_decision_are_safe(database):
    draft = create_order_draft(database, "BEANS-ESP", 2, 17, business_date=BUSINESS_DATE)
    record_human_decision(database, draft["draft_id"], "reject")
    assert get_trusted_submission(database, draft["draft_id"]).status == "rejected"
    with pytest.raises(DraftError):
        record_human_decision(database, draft["draft_id"], "approve")


def test_submission_requires_permitted_or_approved_draft_and_is_idempotent(database):
    draft = create_order_draft(database, "TEA-ASM", 3, 6, business_date=BUSINESS_DATE)
    database.execute(
        "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("PO-1009", "TEA-ASM", 3, 6, 520, "Placed", "2026-10-12", "2026-10-14", "worker", None),
    )
    database.commit()
    mark_draft_submitted(database, draft["draft_id"], "PO-1009")
    assert get_trusted_submission(database, draft["draft_id"]).status == "submitted"
    with pytest.raises(DraftError, match="draft_already_submitted"):
        mark_draft_submitted(database, draft["draft_id"], "PO-1010")


def test_submission_binding_rejects_a_purchase_order_that_does_not_match(database):
    draft = create_order_draft(database, "TEA-ASM", 3, 6, business_date=BUSINESS_DATE)
    database.execute(
        "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("PO-1009", "TEA-ASM", 3, 12, 520, "Placed", "2026-10-12", "2026-10-14", "worker", None),
    )
    database.commit()
    with pytest.raises(DraftError, match="does not match"):
        mark_draft_submitted(database, draft["draft_id"], "PO-1009")


def test_approval_cannot_be_recorded_for_permitted_or_blocked_draft(database):
    permitted = create_order_draft(database, "TEA-ASM", 3, 6, business_date=BUSINESS_DATE)
    blocked = create_order_draft(database, "SYR-CAR", 3, 12, business_date=BUSINESS_DATE)
    for draft in (permitted, blocked):
        with pytest.raises(DraftError):
            record_human_decision(database, draft["draft_id"], "approve")
