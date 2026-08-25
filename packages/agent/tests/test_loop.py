"""The tool-calling loop, driven by a scripted model.

What is under test is the HARNESS, not the reasoning: that every failure mode
becomes a message rather than an exception, that the transcript stays
well-formed, and that the run always terminates with a stop reason.
"""

import json

from agent.loop import StopReason, run_agent
from agent.schemas import Classification, Decision
from agent.tools import TERMINAL_TOOL
from conftest import INVOICE, PO, ScriptedModel, calls, raw_call, says
from conftest import call as tc

SUBMIT_QTY_CORRECTION = {
    "classification": Classification.QUANTITY_EXCEEDS_RECEIPT.value,
    "decision": Decision.PROPOSE_CORRECTION.value,
    "reasoning": "Invoiced 14.000 against receipts totalling 13.000; 7.7% over a 5.0% tolerance.",
    "evidence": [
        "INV 5100000901 line 0001 MENGE 14.000",
        "GR 5000000901 MENGE 13.000 for PO line 00010",
        "PO 4500000009 ToleranceConfig QuantityVariancePct 5.0",
    ],
    "correction": {
        "correction_type": "AMEND_INVOICE_QUANTITY",
        "invoice_number": INVOICE,
        "inv_item_number": "0001",
        "from_quantity": "14.000",
        "to_quantity": "13.000",
    },
}


def _tool_messages(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("role") == "tool"]


def _assistant_tool_call_ids(messages: list[dict]) -> list[str]:
    ids = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            ids.append(call["id"] if isinstance(call, dict) else call.id)
    return ids


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_a_full_run_reaches_a_resolution(erp_client, settings):
    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number=INVOICE)),
        calls(tc("get_purchase_order", po_number=PO)),
        calls(tc("get_goods_receipts", po_number=PO)),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)

    assert run.stop_reason is StopReason.SUBMITTED
    assert run.iterations == 4
    assert run.resolution is not None
    assert run.resolution.classification is Classification.QUANTITY_EXCEEDS_RECEIPT
    assert run.resolution.correction.to_quantity == "13.000"
    # Real ERP data reached the model, not a stub.
    assert '"MENGE": "13.000"' in _tool_messages(run.messages)[2]["content"]
    assert run.prompt_tokens == 400
    assert run.completion_tokens == 80


def test_every_tool_call_is_answered_exactly_once(erp_client, settings):
    """The protocol invariant. Violating it is a 400 or an infinite loop."""
    model = ScriptedModel(
        calls(
            tc("get_invoice", invoice_number=INVOICE),
            tc("get_purchase_order", po_number=PO),
        ),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)

    ids = _assistant_tool_call_ids(run.messages)
    answered = [m["tool_call_id"] for m in _tool_messages(run.messages)]
    assert sorted(ids) == sorted(answered)
    assert len(answered) == len(set(answered))


def test_parallel_tool_calls_in_one_turn_are_all_executed(erp_client, settings):
    model = ScriptedModel(
        calls(
            tc("get_invoice", invoice_number=INVOICE),
            tc("get_purchase_order", po_number=PO),
            tc("get_goods_receipts", po_number=PO),
        ),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert [c.name for c in run.tool_calls][:3] == [
        "get_invoice",
        "get_purchase_order",
        "get_goods_receipts",
    ]
    assert run.stop_reason is StopReason.SUBMITTED


def test_the_model_is_offered_every_tool_including_the_terminal_one(erp_client, settings):
    model = ScriptedModel(calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)))
    run_agent(INVOICE, erp_client, settings, completion_fn=model)

    names = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert names == {
        "get_invoice",
        "get_purchase_order",
        "get_goods_receipts",
        "get_vendor_history",
        "propose_correction",
        TERMINAL_TOOL,
    }
    # A terminal tool that is not offered can never be called, and the run
    # could then only ever end in MAX_ITERATIONS.
    assert TERMINAL_TOOL in names
    assert model.calls[0]["temperature"] == 0.0


def test_the_first_two_messages_are_system_then_user(erp_client, settings):
    model = ScriptedModel(calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)))
    run_agent(INVOICE, erp_client, settings, completion_fn=model)
    sent = model.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[1]["role"] == "user"
    assert INVOICE in sent[1]["content"]


# ---------------------------------------------------------------------------
# failure becomes content
# ---------------------------------------------------------------------------


def test_an_erp_404_becomes_a_message_not_an_exception(erp_client, settings):
    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number="5199999999")),
        calls(
            tc(
                TERMINAL_TOOL,
                classification=Classification.INSUFFICIENT_EVIDENCE.value,
                decision=Decision.ESCALATE.value,
                reasoning="The invoice does not exist in the system.",
                evidence=["get_invoice 5199999999 returned INVOICE_NOT_FOUND"],
                escalate_to="AP_SUPERVISOR",
                escalation_reason="Invoice 5199999999 is not present; nothing to match against.",
            )
        ),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)

    assert run.stop_reason is StopReason.SUBMITTED
    assert run.tool_calls[0].error == "INVOICE_NOT_FOUND"
    assert "INVOICE_NOT_FOUND" in _tool_messages(run.messages)[0]["content"]
    # And the model got to see it, which is the point.
    assert "INVOICE_NOT_FOUND" in json.dumps(model.calls[1]["messages"])


def test_an_unknown_tool_name_becomes_a_message(erp_client, settings):
    model = ScriptedModel(
        calls(tc("delete_invoice", invoice_number=INVOICE)),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.tool_calls[0].error == "UNKNOWN_TOOL"
    assert "no tool named 'delete_invoice'" in _tool_messages(run.messages)[0]["content"]
    assert run.stop_reason is StopReason.SUBMITTED


def test_malformed_json_arguments_become_a_message(erp_client, settings):
    model = ScriptedModel(
        calls(raw_call("get_invoice", '{"invoice_number": ')),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.tool_calls[0].error == "BAD_JSON"
    assert run.stop_reason is StopReason.SUBMITTED


def test_arguments_that_miss_the_schema_become_a_message(erp_client, settings):
    model = ScriptedModel(
        calls(tc("get_invoice", wrong_field="x")),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.tool_calls[0].error == "VALIDATION_ERROR"
    assert "invoice_number" in _tool_messages(run.messages)[0]["content"]


def test_an_incoherent_resolution_is_rejected_and_can_be_retried(erp_client, settings):
    """PROPOSE_CORRECTION with no correction payload must not be accepted.

    The rejection goes back as a tool message, so the schema teaches rather
    than merely failing.
    """
    broken = dict(SUBMIT_QTY_CORRECTION)
    broken.pop("correction")

    model = ScriptedModel(
        calls(tc(TERMINAL_TOOL, **broken)),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)

    assert run.tool_calls[0].error == "RESOLUTION_INVALID"
    assert "requires a `correction` payload" in _tool_messages(run.messages)[0]["content"]
    assert run.stop_reason is StopReason.SUBMITTED
    assert run.resolution.correction is not None


def test_escalate_without_a_reason_is_rejected(erp_client, settings):
    model = ScriptedModel(
        calls(
            tc(
                TERMINAL_TOOL,
                classification=Classification.INSUFFICIENT_EVIDENCE.value,
                decision=Decision.ESCALATE.value,
                reasoning="Something is off.",
                evidence=["INV 5100000901 exists"],
            )
        ),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.tool_calls[0].error == "RESOLUTION_INVALID"
    assert "escalate_to" in _tool_messages(run.messages)[0]["content"]


def test_a_clean_classification_cannot_ask_for_a_correction(erp_client, settings):
    incoherent = dict(SUBMIT_QTY_CORRECTION, classification=Classification.CLEAN.value)
    model = ScriptedModel(
        calls(tc(TERMINAL_TOOL, **incoherent)),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.tool_calls[0].error == "RESOLUTION_INVALID"


def test_an_invented_extra_field_is_rejected(erp_client, settings):
    """extra='forbid': a hallucinated "confidence" is an error, not a silent drop."""
    model = ScriptedModel(
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION, confidence=0.9)),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.tool_calls[0].error == "RESOLUTION_INVALID"
    assert "confidence" in _tool_messages(run.messages)[0]["content"]


# ---------------------------------------------------------------------------
# termination
# ---------------------------------------------------------------------------


def test_prose_gets_one_nudge_then_stops(erp_client, settings):
    model = ScriptedModel(says("Let me think about this."), says("Still thinking."))
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)

    assert run.stop_reason is StopReason.NO_TOOL_CALL
    assert run.resolution is None
    nudges = [
        m for m in run.messages if m.get("role") == "user" and "did not call a tool" in m["content"]
    ]
    assert len(nudges) == 1


def test_prose_followed_by_a_tool_call_recovers(erp_client, settings):
    model = ScriptedModel(
        says("Let me think about this."),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.stop_reason is StopReason.SUBMITTED


def test_a_model_that_never_submits_stops_at_the_iteration_cap(erp_client, settings):
    """The cost ceiling. Without it, a looping model bills until someone notices."""
    model = ScriptedModel(*[calls(tc("get_invoice", invoice_number=INVOICE)) for _ in range(4)])
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)

    assert run.stop_reason is StopReason.MAX_ITERATIONS
    assert run.iterations == settings.max_iterations
    assert run.resolution is None
    assert len(run.tool_calls) == 4
    assert model.turns == []


def test_the_run_serialises_to_json(erp_client, settings):
    """The eval harness and the review UI both consume this, so it must round-trip."""
    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number=INVOICE)),
        calls(tc(TERMINAL_TOOL, **SUBMIT_QTY_CORRECTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    reloaded = json.loads(run.model_dump_json())
    assert reloaded["stop_reason"] == "SUBMITTED"
    assert reloaded["resolution"]["correction"]["to_quantity"] == "13.000"
    assert all(isinstance(m, dict) for m in reloaded["messages"])
