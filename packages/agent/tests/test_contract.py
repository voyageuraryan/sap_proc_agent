"""The agent's view of the wire contract vs the ERP's.

The agent deliberately does NOT import erp_domain or mock_erp.proposals -- it
is an HTTP client, and sharing the models would make "it integrates" a fact
about a Python import rather than about the interface.

The cost of that decision is drift: two definitions of the same payload can
diverge silently. These tests are what pays that cost. They are the only place
in the agent package that imports from mock_erp, and they do it to compare, not
to reuse.
"""

import json

import pytest
from agent import schemas as agent_schemas
from agent.loop import run_agent
from agent.schemas import LABEL_FOR_CLASSIFICATION, Classification, Resolution
from agent.tools import TERMINAL_TOOL, build_tools, to_openai_schemas
from conftest import INVOICE, ScriptedModel, calls
from conftest import call as tc
from mock_erp import proposals as erp_proposals
from pydantic import TypeAdapter, ValidationError

PAYLOAD_PAIRS = [
    (agent_schemas.AmendQuantityPayload, erp_proposals.AmendQuantityPayload),
    (agent_schemas.AmendPricePayload, erp_proposals.AmendPricePayload),
    (agent_schemas.ReleaseBlockPayload, erp_proposals.ReleaseBlockPayload),
    (agent_schemas.RejectInvoicePayload, erp_proposals.RejectInvoicePayload),
]


@pytest.mark.parametrize("mine,theirs", PAYLOAD_PAIRS, ids=lambda m: getattr(m, "__name__", ""))
def test_payload_field_names_match_the_service(mine, theirs):
    assert set(mine.model_fields) == set(theirs.model_fields)


def test_the_correction_types_match_the_service():
    assert {c.value for c in agent_schemas.CorrectionType} == {
        c.value for c in erp_proposals.CorrectionType
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "correction_type": "AMEND_INVOICE_QUANTITY",
            "invoice_number": INVOICE,
            "inv_item_number": "0001",
            "from_quantity": "14.000",
            "to_quantity": "13.000",
        },
        {
            "correction_type": "AMEND_INVOICE_PRICE",
            "invoice_number": INVOICE,
            "inv_item_number": "0001",
            "from_price": "46.10",
            "to_price": "41.90",
        },
        {
            "correction_type": "RELEASE_INVOICE_BLOCK",
            "invoice_number": INVOICE,
            "released_block_reason": "QUANTITY_VARIANCE",
        },
        {
            "correction_type": "REJECT_INVOICE",
            "invoice_number": INVOICE,
            "duplicate_of": "5100004901",
        },
    ],
    ids=["quantity", "price", "release", "reject"],
)
def test_every_payload_the_agent_can_emit_is_accepted_by_the_service(payload, erp_app):
    """Round-trip: build it with the agent's model, post it, get a PROPOSED row.

    This is the test that would have caught the SAP-alias leak in Step 5 --
    the service wanting BELNR where the agent sends invoice_number.
    """
    built = TypeAdapter(agent_schemas.CorrectionPayload).validate_python(payload)
    response = erp_app.post(
        "/ProposeCorrection",
        json={
            "invoice_number": INVOICE,
            "payload": built.model_dump(mode="json"),
            "agent_reasoning": "contract test",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["d"]["status"] == "PROPOSED"


def test_the_service_rejects_a_payload_the_agent_could_not_have_built():
    """The union is closed on both sides, so a made-up correction_type fails."""
    with pytest.raises(ValidationError):
        TypeAdapter(agent_schemas.CorrectionPayload).validate_python(
            {"correction_type": "DELETE_INVOICE", "invoice_number": INVOICE}
        )


# ---------------------------------------------------------------------------
# eval support
# ---------------------------------------------------------------------------


def test_every_classification_maps_to_a_ground_truth_label():
    """A new classification cannot be added without deciding what it scores as."""
    assert set(LABEL_FOR_CLASSIFICATION) == set(Classification)


def test_the_label_mapping_covers_the_generator_taxonomy():
    from generator.labels import ExceptionLabel

    mapped = set(LABEL_FOR_CLASSIFICATION.values())
    assert mapped == {label.value for label in ExceptionLabel}


def test_the_mapping_is_one_to_one():
    """Two classifications scoring as the same label would make the eval unreadable."""
    values = list(LABEL_FOR_CLASSIFICATION.values())
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# the tool schemas as the model sees them
# ---------------------------------------------------------------------------


def test_every_tool_schema_is_json_serialisable_and_described(erp_client):
    """A tool with no description is a tool the model will misuse."""
    for tool in to_openai_schemas(build_tools(erp_client)):
        json.dumps(tool)  # must not raise
        function = tool["function"]
        assert function["description"].strip(), function["name"]
        assert function["parameters"]["type"] == "object"


def test_the_terminal_tool_schema_is_the_resolution_model(erp_client):
    """Structured output is enforced by the tool schema, not by parsing prose."""
    tools = to_openai_schemas(build_tools(erp_client))
    submit = next(t for t in tools if t["function"]["name"] == TERMINAL_TOOL)
    assert submit["function"]["parameters"] == Resolution.model_json_schema()
    required = set(submit["function"]["parameters"]["required"])
    assert {"classification", "decision", "reasoning", "evidence"} <= required


def test_the_enums_reach_the_model_as_closed_lists(erp_client):
    """If the values were free-form strings the eval could never score them."""
    tools = to_openai_schemas(build_tools(erp_client))
    submit = next(t for t in tools if t["function"]["name"] == TERMINAL_TOOL)
    defs = submit["function"]["parameters"]["$defs"]
    assert set(defs["Classification"]["enum"]) == {c.value for c in Classification}
    assert len(defs["Decision"]["enum"]) == 4


def test_no_classification_value_is_an_accidental_alias():
    """A duplicated enum value becomes an ALIAS: the member silently disappears
    from the schema, the model can never emit it, and the eval scores the wrong
    class without anything failing. See decisions.md."""
    assert len(list(Classification)) == len(Classification.__members__)
    assert len({c.value for c in Classification}) == len(list(Classification))


def test_the_resolution_the_loop_returns_is_the_one_the_model_sent(erp_client, settings):
    """No lossy re-parse between the tool call and the AgentRun."""
    payload = {
        "classification": Classification.CLEAN.value,
        "decision": "POST_INVOICE",
        "reasoning": "Invoiced quantity and price both equal the receipt and the PO.",
        "evidence": ["INV MENGE 14.000", "GR MENGE 14.000", "PO NETPR 41.90"],
    }
    model = ScriptedModel(calls(tc(TERMINAL_TOOL, **payload)))
    run = run_agent(INVOICE, erp_client, settings, completion_fn=model)
    assert run.resolution.model_dump(mode="json", exclude_none=True) == payload
