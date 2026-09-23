"""The claim the whole project rests on:

    no write to the mock ERP happens without explicit human approval.

Three ways of testing it, in increasing strength:

  1. The agent's tool registry contains no way to apply anything.
  2. Running the agent to a PROPOSE_CORRECTION leaves the document unchanged.
  3. The document only changes after a human approval and a matching payload
     hash -- and it changes on the same URL the agent read, so "nothing
     happened" is observable rather than asserted.
"""

import json

from agent.loop import StopReason, run_agent
from agent.schemas import Classification, Decision
from agent.tools import TERMINAL_TOOL, build_tools, to_openai_schemas
from conftest import INVOICE, PO, ScriptedModel, calls
from conftest import call as tc

CORRECTION = {
    "correction_type": "AMEND_INVOICE_QUANTITY",
    "invoice_number": INVOICE,
    "inv_item_number": "0001",
    "from_quantity": "14.000",
    "to_quantity": "13.000",
}

RESOLUTION = {
    "classification": Classification.QUANTITY_EXCEEDS_RECEIPT.value,
    "decision": Decision.PROPOSE_CORRECTION.value,
    "reasoning": "Invoiced 14.000 against receipts of 13.000, outside a 5.0% tolerance.",
    "evidence": ["INV 5100000901 MENGE 14.000", "GR 5000000901 MENGE 13.000"],
    "correction": CORRECTION,
}


def test_the_agent_has_no_tool_that_applies_anything(erp_client):
    """The strongest form of the guarantee: it is absent, not merely refused."""
    names = set(build_tools(erp_client))
    assert names == {
        "get_invoice",
        "get_purchase_order",
        "get_goods_receipts",
        "get_vendor_history",
        "propose_correction",
        TERMINAL_TOOL,
    }
    forbidden = {"apply", "approve", "reject", "post", "delete", "update", "amend"}
    for name in names:
        parts = set(name.split("_"))
        assert not (parts & forbidden), f"tool {name!r} looks like a write"


def test_the_erp_client_exposes_no_apply_method(erp_client):
    public = {n for n in dir(erp_client) if not n.startswith("_")}
    assert "apply_correction" not in public
    assert "approve" not in public
    # propose_correction is the single write, and it writes a proposal row.
    writes = {n for n in public if n.startswith(("post_", "put_", "delete_", "apply_"))}
    assert writes == set()


def test_proposing_does_not_change_the_invoice(erp_app, erp_client, settings):
    before = erp_client.get_invoice(INVOICE)
    assert before["items"][0]["MENGE"] == "14.000"

    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number=INVOICE)),
        calls(tc("get_goods_receipts", po_number=PO)),
        calls(
            tc(
                "propose_correction",
                payload=CORRECTION,
                agent_reasoning="Receipts total 13.000 against 14.000 invoiced.",
            )
        ),
        calls(tc(TERMINAL_TOOL, **RESOLUTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, chat_model=model)
    assert run.stop_reason is StopReason.SUBMITTED

    proposal_call = next(c for c in run.tool_calls if c.name == "propose_correction")
    assert proposal_call.error is None
    proposal = json.loads(proposal_call.result_summary)
    assert proposal["status"] == "PROPOSED"

    # The whole point: the agent finished, a proposal exists, nothing moved.
    after = erp_client.get_invoice(INVOICE)
    assert after["items"][0]["MENGE"] == "14.000"
    assert after == before


def test_the_full_gate_from_the_agents_own_proposal(erp_app, erp_client, settings):
    """Agent proposes -> still 14.000 -> apply refused -> human approves ->
    still 14.000 -> apply -> 13.000. On the same URL throughout."""
    model = ScriptedModel(
        calls(
            tc(
                "propose_correction",
                payload=CORRECTION,
                agent_reasoning="Receipts total 13.000 against 14.000 invoiced.",
            )
        ),
        calls(tc(TERMINAL_TOOL, **RESOLUTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, chat_model=model, scenario_id="SC-0009")
    proposal_id = json.loads(run.tool_calls[0].result_summary)["proposal_id"]

    assert erp_client.get_invoice(INVOICE)["items"][0]["MENGE"] == "14.000"

    # Applying before approval must fail -- and the agent has no tool for it,
    # so this request has to be made by hand.
    refused = erp_app.post(
        "/ApplyCorrection", json={"proposal_id": proposal_id, "payload": CORRECTION}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ILLEGAL_TRANSITION"
    assert erp_client.get_invoice(INVOICE)["items"][0]["MENGE"] == "14.000"

    # The human step is on a different router entirely.
    approved = erp_app.post(
        "http://erp/approval/proposals/" + proposal_id + "/approve",
        json={"approved_by": "ap.supervisor@example.com"},
    )
    assert approved.status_code == 200
    assert erp_client.get_invoice(INVOICE)["items"][0]["MENGE"] == "14.000"

    # A payload that differs from the approved one is refused by hash.
    tampered = dict(CORRECTION, to_quantity="1.000")
    mismatch = erp_app.post(
        "/ApplyCorrection", json={"proposal_id": proposal_id, "payload": tampered}
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "PAYLOAD_MISMATCH"
    assert erp_client.get_invoice(INVOICE)["items"][0]["MENGE"] == "14.000"

    applied = erp_app.post(
        "/ApplyCorrection", json={"proposal_id": proposal_id, "payload": CORRECTION}
    )
    assert applied.status_code == 200
    # Only now, and visible on the read the agent itself uses.
    assert erp_client.get_invoice(INVOICE)["items"][0]["MENGE"] == "13.000"


def test_the_agents_own_reads_never_contain_a_ground_truth_label(erp_client, settings):
    """Label isolation, checked from the agent's side of the wire."""
    labels = {
        "CLEAN",
        "PRICE_MINOR",
        "PRICE_MAJOR",
        "QTY_OVER",
        "GR_MISSING",
        "GR_PARTIAL",
        "DUP_INVOICE",
        "AMBIGUOUS",
        "DANGLING_PO_LINE",
        "UNAUTHORISED_OVER_DELIVERY",
        "CONFLICTING_RECEIPTS",
    }
    model = ScriptedModel(
        calls(
            tc("get_invoice", invoice_number=INVOICE),
            tc("get_purchase_order", po_number=PO),
            tc("get_goods_receipts", po_number=PO),
            tc("get_vendor_history", vendor_id="1000000010"),
        ),
        calls(tc(TERMINAL_TOOL, **RESOLUTION)),
    )
    run = run_agent(INVOICE, erp_client, settings, chat_model=model)

    served = " ".join(m["content"] for m in run.messages if m.get("role") == "tool")
    leaked = {label for label in labels if label in served}
    assert leaked == set(), f"the ERP served ground-truth labels: {leaked}"


def test_no_prompt_or_tool_description_names_the_taxonomy(erp_client):
    """The agent must not be told the answer key either."""
    from agent.prompts import SYSTEM_PROMPT

    text = SYSTEM_PROMPT + json.dumps(to_openai_schemas(build_tools(erp_client)))
    for label in ("PRICE_MINOR", "PRICE_MAJOR", "QTY_OVER", "GR_PARTIAL", "DUP_INVOICE"):
        assert label not in text


def test_scenario_id_is_not_something_the_model_can_set(erp_client, settings):
    """Eval bookkeeping is the harness's business, not the model's.

    If the model could pass scenario_id it could mislabel its own proposal,
    and the eval would score the wrong row.
    """
    schema = to_openai_schemas(build_tools(erp_client, scenario_id="SC-0009"))
    propose = next(t for t in schema if t["function"]["name"] == "propose_correction")
    params = propose["function"]["parameters"]
    assert set(params["properties"]) == {"payload", "agent_reasoning"}
    # Nested payload schemas are inlined, so search the whole rendering.
    assert "scenario_id" not in json.dumps(params)
