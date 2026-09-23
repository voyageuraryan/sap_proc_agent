"""The tool layer: what the model is allowed to do, and how it is described.

Each tool is a LangChain `StructuredTool`, which bundles the three things a
tool always is:

  1. a JSON Schema the MODEL reads   -> args_schema (a Pydantic model)
  2. a Python callable YOUR code runs -> func
  3. a name that links them          -> name, and the registry dict key

Using LangChain's type rather than a home-grown one is what lets any chat
model's `bind_tools` render them in its own provider's wire format, and what
makes every tool call a traced run for any callback handler -- Langfuse
included -- without this module knowing either exists.

The `description` strings are not comments. They are the only instructions the
model gets about when to reach for a tool, so they are prompt engineering and
belong under the same review as prompts.py.

Note what is NOT here: apply_correction. Within one run there is no approval,
so applying could only ever return 409, and leaving it out means the agent's
tool list literally contains no way to change a document. See decisions.md.
"""

from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, ValidationError

from agent.erp_client import ErpClient, ErpError
from agent.schemas import CorrectionPayload, Resolution

#: The tool whose call ends the run. Named once, compared everywhere.
TERMINAL_TOOL = "submit_resolution"


def tool_error_code(tool_name: str, exc: BaseException) -> str:
    """The one name for a tool failure, shared by the tool node and the tracer.

    Defined once so a record in the AgentRun and a span in the trace cannot
    disagree about what went wrong.
    """
    if isinstance(exc, ErpError):
        # The expected failure. A PO_NOT_FOUND is evidence, not a crash.
        return exc.code
    if isinstance(exc, ValidationError):
        return "RESOLUTION_INVALID" if tool_name == TERMINAL_TOOL else "VALIDATION_ERROR"
    return "INTERNAL_ERROR"


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


def _tool(name: str, description: str, args_schema: type[BaseModel], func) -> StructuredTool:
    """One tool. `func` receives the VALIDATED fields as keyword arguments.

    LangChain validates the model's arguments against `args_schema` before
    `func` runs, so a schema violation is a pydantic ValidationError raised by
    `invoke` -- which the graph's tool node turns into a message, never a crash.
    """
    return StructuredTool.from_function(
        func=func,
        name=name,
        description=description,
        args_schema=args_schema,
    )


def build_tools(
    client: ErpClient,
    *,
    scenario_id: str | None = None,
) -> dict[str, BaseTool]:
    """Bind the tool set to one ERP client. A factory, not a class.

    The client is captured in closures, so the tools are already wired to a
    connection and the tool node needs no context beyond the registry.
    """
    tools = [
        _tool(
            "get_invoice",
            (
                "Fetch a supplier invoice: header (BELNR, LIFNR, BLDAT, XBLNR, "
                "block_reason) and its lines (BUZEI, EBELN, EBELP, MENGE, NETPR). "
                "Start here -- the invoice tells you which PO to look at next."
            ),
            GetInvoiceArgs,
            lambda invoice_number: client.get_invoice(invoice_number),
        ),
        _tool(
            "get_purchase_order",
            (
                "Fetch a purchase order: header (EBELN, LIFNR, WERKS, WAERS) and "
                "its lines (EBELP, MATNR, MENGE, NETPR). The response also carries "
                "ToleranceConfig, which holds the price and quantity tolerance "
                "percentages that apply to THIS vendor. That block is the only "
                "authority on tolerance -- do not assume a default."
            ),
            GetPurchaseOrderArgs,
            lambda po_number: client.get_purchase_order(po_number),
        ),
        _tool(
            "get_goods_receipts",
            (
                "Fetch every goods-receipt line posted against a purchase order "
                "(MBLNR, EBELN, EBELP, MENGE, BUDAT). This is what actually "
                "arrived. An empty list means nothing has been received yet -- "
                "that is a fact, not a failure."
            ),
            GetGoodsReceiptsArgs,
            lambda po_number: client.get_goods_receipts(po_number),
        ),
        _tool(
            "get_vendor_history",
            (
                "Fetch aggregates for a supplier: how many invoices, how many "
                "were blocked, average days from receipt to invoice, the "
                "applicable tolerance, and the list of prior invoice references "
                "(BELNR/XBLNR/BLDAT). Use it to check whether an XBLNR has been "
                "billed before, or to judge whether a late invoice is normal for "
                "this supplier."
            ),
            GetVendorHistoryArgs,
            lambda vendor_id: client.get_vendor_history(vendor_id),
        ),
        _tool(
            "propose_correction",
            (
                "Raise a correction for a human to approve. This does NOT change "
                "the invoice: it records a proposal that a person must approve "
                "before anything is applied. Only call it once you have the "
                "figures from the documents. Returns the proposal id and status."
            ),
            ProposeCorrectionArgs,
            lambda payload, agent_reasoning: client.propose_correction(
                invoice_number=payload.invoice_number,
                payload=payload.model_dump(mode="json"),
                agent_reasoning=agent_reasoning,
                scenario_id=scenario_id,
            ),
        ),
        _tool(
            TERMINAL_TOOL,
            (
                "Record your final verdict and END the run. Call this exactly "
                "once, after you have gathered the evidence you need. You must "
                "call it -- a run that stops without it counts as a failure."
            ),
            Resolution,
            # The work is the VALIDATION, which LangChain does against the
            # Resolution schema -- including its coherence validator -- before
            # this runs. Reaching this line means the verdict was accepted.
            lambda **_: "accepted",
        ),
    ]
    return {tool.name: tool for tool in tools}


def to_openai_schemas(tools: dict[str, BaseTool] | list[BaseTool]) -> list[dict]:
    """Render the registry into the OpenAI tool-calling wire format.

    Not what the model necessarily receives -- each chat model's `bind_tools`
    renders for its own provider -- but the provider-neutral form, used by the
    cassette fingerprint and by the tests that check what the model is told.

    Accepts the dict directly: iterating a dict yields its KEYS, and passing
    the registry to something that expects tools is the easy mistake here.
    """
    values = tools.values() if isinstance(tools, dict) else tools
    return [convert_to_openai_tool(tool) for tool in values]
