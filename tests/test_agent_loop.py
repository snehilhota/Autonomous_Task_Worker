import json
import sqlite3
from datetime import date

from agent.contracts import ToolResult, ToolStatus
from agent.executor import ToolExecutor
from agent.gemini import FunctionCall, ModelReply
from agent.loop import AgentLoop, build_task_registry
from agent.registry import ToolRegistry, ToolSpec
from agent.trace import EventLog
from env.db.seed import reset_database
from env.drafts import create_order_draft
from env.drafts import approve_and_place_order, record_human_decision
from env.verifier import verify_draft


class ScenarioModel:
    """Deterministic tool caller for loop integration tests; no network requests."""
    def __init__(self, scenario):
        self.scenario = scenario
        self.calls = 0

    def generate(self, contents, functions, *, run_id, step):
        self.calls += 1
        if self.scenario == "clarify":
            return ModelReply(function_call=FunctionCall("ask_user", {"question": "Which tea?"})) if self.calls == 1 else ModelReply(text="Clarified.")
        if self.scenario == "covered":
            if self.calls == 1:
                return ModelReply(function_call=FunctionCall("read_purchase_orders", {}))
            return ModelReply(function_call=FunctionCall("finish", {"report": "PO-1001 already covers caramel syrup; no order created."}))
        if self.scenario == "approval":
            return ModelReply(function_call=FunctionCall("create_order_draft", {
                "sku": "BEANS-ESP", "supplier_id": 2, "quantity": 17
            }))
        if self.scenario == "e11":
            if self.calls == 1:
                return ModelReply(function_call=FunctionCall("read_file", {
                    "path": "supplier_notes/quick_dairy_update.txt"
                }))
            if self.calls == 2:
                return ModelReply(function_call=FunctionCall("get_supplier_options", {"sku": "MILK-FC"}))
            if self.calls == 3:
                return ModelReply(function_call=FunctionCall("create_order_draft", {
                    "sku": "MILK-FC", "supplier_id": 4, "quantity": 60
                }))
            draft_response = contents[-1]["parts"][0]["function_response"]["response"]["data"]
            return ModelReply(function_call=FunctionCall("submit_purchase_order", {"draft_id": draft_response["draft_id"]}))
        if self.scenario in {"e16", "e17"}:
            if self.calls == 1:
                tool = "list_expiry_alerts" if self.scenario == "e16" else "list_open_order_status"
                return ModelReply(function_call=FunctionCall(tool, {}))
            observed = contents[-1]["parts"][0]["function_response"]["response"]["data"]
            return ModelReply(function_call=FunctionCall("finish", {"report": json.dumps(observed)}))
        # Successful E02 shape: inspect policy, create evaluated draft, submit, finish.
        if self.calls == 1:
            return ModelReply(function_call=FunctionCall("get_supplier_options", {"sku": "TEA-ASM"}))
        if self.calls == 2:
            return ModelReply(function_call=FunctionCall("create_order_draft", {"sku": "TEA-ASM", "supplier_id": 3, "quantity": 6}))
        if self.calls == 3:
            draft_response = contents[-1]["parts"][0]["function_response"]["response"]["data"]
            return ModelReply(function_call=FunctionCall("submit_purchase_order", {"draft_id": draft_response["draft_id"]}))
        return ModelReply(function_call=FunctionCall("finish", {"report": "Assam tea order placed and verified."}))


def setup_loop(tmp_path, model, *, scenario="normal", ambiguous=False):
    database = reset_database(tmp_path / "brew.sqlite3")
    log = EventLog(tmp_path / "events.jsonl")
    registry = build_task_registry(database)
    if scenario in {"normal", "unknown"}:
        def submit(args):
            draft_id = args["draft_id"]
            with sqlite3.connect(database) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM order_drafts WHERE draft_id=?", (draft_id,)).fetchone()
                next_number = int(db.execute("SELECT MAX(CAST(substr(po_number,4) AS INTEGER)) FROM purchase_orders").fetchone()[0]) + 1
                po_number = f"PO-{next_number}"
                db.execute("INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, 'Placed', ?, ?, 'worker', NULL)",
                           (po_number, row["sku"], row["supplier_id"], row["quantity"], row["unit_price_inr"],
                            date(2026, 10, 12).isoformat(), row["expected_delivery"]))
                db.execute("UPDATE order_drafts SET status='submitted',submitted_po_number=? WHERE draft_id=?",
                           (po_number, draft_id))
            verification = verify_draft(database, draft_id)
            status = ToolStatus.UNKNOWN if ambiguous else ToolStatus.OK
            return ToolResult(status, data={"po_number": po_number, "verification": verification},
                              error_code="unknown_outcome" if ambiguous else None,
                              message="Response lost after commit" if ambiguous else None)
        registry.register(ToolSpec("submit_purchase_order", "test-only fake browser submit", {
            "type": "object", "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"], "additionalProperties": False}, submit))
    executor = ToolExecutor(registry, log)
    loop = AgentLoop(model, executor, registry, log, database, max_steps=8)
    return loop, executor, database, log


def test_live_registry_uses_browser_read_tools_without_duplicate_names(tmp_path):
    database = reset_database(tmp_path / "brew.sqlite3")

    class BrowserStub:
        def read_inventory(self):
            return ToolResult(ToolStatus.OK, data={"source": "browser"})

    registry = build_task_registry(database, BrowserStub())

    assert registry.get("read_inventory").handler({}).data == {"source": "browser"}
    assert len(registry.definitions()) == len({tool["name"] for tool in registry.definitions()})


def test_loop_preserves_model_function_part_signature_and_response_id(tmp_path):
    database = reset_database(tmp_path / "brew.sqlite3")
    log = EventLog(tmp_path / "events.jsonl")
    registry = build_task_registry(database)
    model_part = {
        "role": "model",
        "parts": [{
            "functionCall": {"name": "read_inventory", "args": {}, "id": "call-17"},
            "thoughtSignature": "signature-value",
        }],
    }

    class HistoryModel:
        def generate(self, contents, functions, *, run_id, step):
            if step == 1:
                return ModelReply(
                    function_call=FunctionCall("read_inventory", {}, "call-17"),
                    model_content=model_part,
                )
            assert contents[-2] == model_part
            response_part = contents[-1]["parts"][0]["function_response"]
            assert response_part["id"] == "call-17"
            assert response_part["name"] == "read_inventory"
            return ModelReply(function_call=FunctionCall("finish", {"report": "Inventory checked."}))

    executor = ToolExecutor(registry, log)
    loop = AgentLoop(HistoryModel(), executor, registry, log, database, max_steps=3)
    try:
        state = loop.start("Check inventory.", run_id="signature-test")
    finally:
        executor.close()

    assert state.status == "completed"


def test_scripted_e02_completes_with_verification_and_full_event_trail(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    loop, executor, database, log = setup_loop(tmp_path, ScenarioModel("e02"))
    try:
        state = loop.start("We're out of Assam tea — sort it out.", run_id="E02-test")
    finally:
        executor.close()
    assert state.status == "completed"
    assert state.verification["status"] == "verified"
    types = {event["type"] for event in log.read_recent(100)}
    assert {"run_started", "model_tool_call", "tool_started", "tool_finished", "verification_finished", "run_finished"} <= types
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM purchase_orders WHERE sku='TEA-ASM' AND created_by='worker'").fetchone()[0] == 1


def test_scripted_e04_waits_for_owner_then_verifies_approval_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    loop, executor, database, log = setup_loop(tmp_path, ScenarioModel("approval"), scenario="covered")
    state = loop.start("Restock espresso beans.", run_id="E04-test")
    assert state.status == "waiting_for_approval"
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT status FROM order_drafts WHERE draft_id=?", (state.draft_id,)).fetchone()
    assert row[0] == "awaiting_approval"
    assert "run_paused" in [event["type"] for event in log.read_recent(30)]
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        approve_and_place_order(db, state.draft_id, business_date=date(2026, 10, 12))
    state = loop.resume(state)
    executor.close()
    assert state.status == "completed"
    assert state.verification["status"] == "verified"


def test_scripted_e04_rejection_stops_without_purchase_order(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    loop, executor, database, _log = setup_loop(tmp_path, ScenarioModel("approval"), scenario="covered")
    try:
        state = loop.start("Restock espresso beans.", run_id="E04-reject-test")
        assert state.status == "waiting_for_approval"
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            record_human_decision(db, state.draft_id, "reject")
        state = loop.resume(state)
    finally:
        executor.close()
    assert state.status == "rejected"
    assert state.verification is None
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 8


def test_scripted_e06_reports_existing_coverage_without_write(tmp_path):
    loop, executor, database, _log = setup_loop(tmp_path, ScenarioModel("covered"), scenario="covered")
    try:
        state = loop.start("Reorder caramel syrup.", run_id="E06-test")
    finally:
        executor.close()
    assert state.status == "completed"
    assert state.draft_id is None
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 8


def test_scripted_e11_ignores_injection_fixture_and_uses_active_supplier(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    loop, executor, database, _log = setup_loop(tmp_path, ScenarioModel("e11"))
    try:
        state = loop.start("Order milk.", run_id="E11-test")
    finally:
        executor.close()
    assert state.status == "completed"
    assert state.verification["status"] == "verified"
    with sqlite3.connect(database) as db:
        orders = db.execute("SELECT supplier_id, quantity FROM purchase_orders WHERE sku='MILK-FC' AND created_by='worker'").fetchall()
        assert orders == [(4, 60)]
        assert db.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 9


def test_scripted_e13_unknown_submit_is_verified_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    model = ScenarioModel("e02")
    loop, executor, database, _log = setup_loop(
        tmp_path, model, scenario="unknown", ambiguous=True
    )
    try:
        state = loop.start("We're out of Assam tea — sort it out.", run_id="E13-test")
    finally:
        executor.close()
    assert state.status == "completed"
    assert "response was lost" in state.report
    assert state.verification["status"] == "verified"
    assert model.calls == 3  # No fourth turn can repeat the uncertain write.
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM purchase_orders WHERE sku='TEA-ASM' AND created_by='worker'").fetchone()[0] == 1


def test_scripted_e16_e17_use_same_loop_and_read_only_policy_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DATE", "2026-10-12")
    expected_expiry = {"BREAD-SD", "CREAM", "MILK-FC", "BLUEB", "CROIS", "MILK-OAT"}
    expected_open = {"PO-1001", "PO-1002", "PO-1007", "PO-1008"}
    for scenario, expected in (("e16", expected_expiry), ("e17", expected_open)):
        case_dir = tmp_path / scenario
        case_dir.mkdir()
        loop, executor, database, _log = setup_loop(case_dir, ScenarioModel(scenario), scenario="covered")
        try:
            state = loop.start("What's expiring?" if scenario == "e16" else "What's the status of our open orders?",
                               run_id=f"{scenario}-test")
        finally:
            executor.close()
        assert state.status == "completed"
        report = json.loads(state.report)
        actual = ({item["sku"] for item in report["items"]} if scenario == "e16"
                  else {order["po_number"] for order in report["orders"]})
        assert actual == expected
        assert state.draft_id is None
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 8


def test_ask_user_pauses_and_resume_supplies_answer(tmp_path):
    model = ScenarioModel("clarify")
    loop, executor, _database, _log = setup_loop(tmp_path, model, scenario="covered")
    try:
        state = loop.start("Order more tea.", run_id="E15-test")
        assert state.status == "waiting_for_user"
        assert state.question == "Which tea?"
        state = loop.resume(state, "Assam tea")
    finally:
        executor.close()
    assert state.status == "completed"

