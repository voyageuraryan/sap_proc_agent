"""The tool layer: what the model is allowed to do, and how it is described.

A tool is three separate things, and keeping them separate is the whole point
of this module:

  1. a JSON Schema the MODEL reads   -> args_model.model_json_schema()
  2. a Python callable YOUR code runs -> fn
  3. a name that links them          -> the registry dict

The `description` strings are not comments. They are the only instructions the
model gets about when to reach for a tool, so they are prompt engineering and
belong under the same review as prompts.py.

Note what is NOT here: apply_correction. Within one run there is no approval,
so applying could only ever return 409, and leaving it out means the agent's
tool list literally contains no way to change a document. See decisions.md.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agent.erp_client import ErpClient
from agent.schemas import CorrectionPayload, Resolution

#: The tool whose call ends the run. Named once, compared everywhere.
TERMINAL_TOOL = "submit_resolution"


# ---------------------------------------------------------------------------
# Argument models. One per tool. Every `description` reaches the model.
# ---------------------------------------------------------------------------


class GetInvoiceArgs(BaseModel):
    invoice_number: str = Field(
        description="The supplier invoice number, 10 digits starting 51, e.g. 5100000901."
    )


class GetPurchaseOrderArgs(BaseModel):
    po_number: str = Field(
        description=(
            "The purchase order number, 10 digits starting 45, e.g. 4500000009. "
            "Take it from an invoice line's EBELN field."
        )
    )


class GetGoodsReceiptsArgs(BaseModel):
    po_number: str = Field(
        description=(
            "The purchase order number whose receipts you want, e.g. 4500000009. "
            "Returns one flat row per receipt line; a PO with no receipts returns "
            "an empty list."
        )
    )


class GetVendorHistoryArgs(BaseModel):
    vendor_id: str = Field(
        description=(
            "The supplier number, 10 digits, e.g. 1000000010. Take it from the "
            "invoice or purchase order LIFNR field."
        )
    )


class ProposeCorrectionArgs(BaseModel):
    """Args for raising a correction for human approval.

    scenario_id is absent on purpose: it is eval bookkeeping the model has no
    way to know, so build_tools() closes over it instead of asking for it.
    """

    payload: CorrectionPayload = Field(
        description=(
            "The correction to raise. Pick the shape that matches "
            "correction_type; every field must come from a document you read."
        )
    )
    agent_reasoning: str = Field(
        min_length=1,
        description=(
            "Why this correction is right, citing the figures compared. A human "
            "reads this before approving, so it must stand on its own."
        ),
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One tool, in all three of its aspects.

    `fn` always takes the validated args model and nothing else. Uniform
    signatures are what let the executor be a single generic function instead
    of a switch statement.
    """

    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[BaseModel], object]


def build_tools(
    client: ErpClient,
    *,
    scenario_id: str | None = None,
) -> dict[str, ToolSpec]:
    """Bind the tool set to one ERP client. A factory, not a class.

    The client is captured in closures, so the tools are already wired to a
    connection and the executor needs no context beyond the registry.
    """
    specs = [
        ToolSpec(
            name="get_invoice",
            description=(
                "Fetch a supplier invoice: header (BELNR, LIFNR, BLDAT, XBLNR, "
                "block_reason) and its lines (BUZEI, EBELN, EBELP, MENGE, NETPR). "
                "Start here -- the invoice tells you which PO to look at next."
            ),
            args_model=GetInvoiceArgs,
            fn=lambda a: client.get_invoice(a.invoice_number),
        ),
        ToolSpec(
            name="get_purchase_order",
            description=(
                "Fetch a purchase order: header (EBELN, LIFNR, WERKS, WAERS) and "
                "its lines (EBELP, MATNR, MENGE, NETPR). The response also carries "
                "ToleranceConfig, which holds the price and quantity tolerance "
                "percentages that apply to THIS vendor. That block is the only "
                "authority on tolerance -- do not assume a default."
            ),
            args_model=GetPurchaseOrderArgs,
            fn=lambda a: client.get_purchase_order(a.po_number),
        ),
        ToolSpec(
            name="get_goods_receipts",
            description=(
                "Fetch every goods-receipt line posted against a purchase order "
                "(MBLNR, EBELN, EBELP, MENGE, BUDAT). This is what actually "
                "arrived. An empty list means nothing has been received yet -- "
                "that is a fact, not a failure."
            ),
            args_model=GetGoodsReceiptsArgs,
            fn=lambda a: client.get_goods_receipts(a.po_number),
        ),
        ToolSpec(
            name="get_vendor_history",
            description=(
                "Fetch aggregates for a supplier: how many invoices, how many "
                "were blocked, average days from receipt to invoice, the "
                "applicable tolerance, and the list of prior invoice references "
                "(BELNR/XBLNR/BLDAT). Use it to check whether an XBLNR has been "
                "billed before, or to judge whether a late invoice is normal for "
                "this supplier."
            ),
            args_model=GetVendorHistoryArgs,
            fn=lambda a: client.get_vendor_history(a.vendor_id),
        ),
        ToolSpec(
            name="propose_correction",
            description=(
                "Raise a correction for a human to approve. This does NOT change "
                "the invoice: it records a proposal that a person must approve "
                "before anything is applied. Only call it once you have the "
                "figures from the documents. Returns the proposal id and status."
            ),
            args_model=ProposeCorrectionArgs,
            fn=lambda a: client.propose_correction(
                invoice_number=a.payload.invoice_number,
                payload=a.payload.model_dump(mode="json"),
                agent_reasoning=a.agent_reasoning,
                scenario_id=scenario_id,
            ),
        ),
        ToolSpec(
            name=TERMINAL_TOOL,
            description=(
                "Record your final verdict and END the run. Call this exactly "
                "once, after you have gathered the evidence you need. You must "
                "call it -- a run that stops without it counts as a failure."
            ),
            args_model=Resolution,
            # Intercepted by name in the loop, so this is never invoked. Kept
            # non-None so every ToolSpec is uniform and the registry needs no
            # special case.
            fn=lambda a: a,
        ),
    ]
    return {spec.name: spec for spec in specs}


def to_openai_schemas(specs: dict[str, ToolSpec] | list[ToolSpec]) -> list[dict]:
    """Render the registry into the tool-calling wire format.

    Accepts the dict directly: iterating a dict yields its KEYS, and passing
    the registry to something that expects ToolSpecs is the easy mistake here.
    """
    values = specs.values() if isinstance(specs, dict) else specs
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args_model.model_json_schema(),
            },
        }
        for spec in values
    ]
