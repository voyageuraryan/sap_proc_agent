"""The agent's output contract, and its view of the correction wire format.

Two things live here:

1. `Resolution` -- the terminal tool's argument model. Registering a Pydantic
   model as a tool's schema is how you force structured output: the model
   cannot "finish" except by filling this in, and a field it fails to justify
   is a validation error the loop feeds back to it.

2. The four correction payload shapes. These are DELIBERATELY redefined here
   rather than imported from mock_erp.proposals: the agent is an HTTP client,
   so it holds its own view of the wire contract. test_contract.py asserts the
   two views still agree, which is a real integration test -- a shared import
   would have made it a tautology.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Classification(StrEnum):
    """What the agent believes it is looking at.

    Deliberately the agent's own vocabulary, not the generator's label names.
    The eval owns the mapping (see LABEL_FOR_CLASSIFICATION) so that scoring is
    an explicit, reviewable table rather than an accident of string equality.
    """

    CLEAN = "CLEAN"
    PRICE_VARIANCE_WITHIN_TOLERANCE = "PRICE_VARIANCE_WITHIN_TOLERANCE"
    PRICE_VARIANCE_EXCEEDS_TOLERANCE = "PRICE_VARIANCE_EXCEEDS_TOLERANCE"
    QUANTITY_EXCEEDS_RECEIPT = "QUANTITY_EXCEEDS_RECEIPT"
    GOODS_RECEIPT_MISSING = "GOODS_RECEIPT_MISSING"
    PARTIAL_DELIVERY = "PARTIAL_DELIVERY"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


#: Ground-truth label (generator taxonomy) for each classification.
#: Used by the eval harness in Step 8. Kept here so a new classification
#: cannot be added without deciding what it scores against.
LABEL_FOR_CLASSIFICATION: dict[Classification, str] = {
    Classification.CLEAN: "CLEAN",
    Classification.PRICE_VARIANCE_WITHIN_TOLERANCE: "PRICE_MINOR",
    Classification.PRICE_VARIANCE_EXCEEDS_TOLERANCE: "PRICE_MAJOR",
    Classification.QUANTITY_EXCEEDS_RECEIPT: "QTY_OVER",
    Classification.GOODS_RECEIPT_MISSING: "GR_MISSING",
    Classification.PARTIAL_DELIVERY: "GR_PARTIAL",
    Classification.DUPLICATE_INVOICE: "DUP_INVOICE",
    Classification.INSUFFICIENT_EVIDENCE: "AMBIGUOUS",
}


class Decision(StrEnum):
    """What the agent wants done. Four values, because code has to branch on it.

    ESCALATE is a first-class value rather than prose in `reasoning`: code
    cannot route on a paragraph, an eval cannot score one, and a required
    enum is *pressure* -- without it a model asked for a correction on a
    GR_MISSING invoice will invent a to_quantity that has no basis.
    """

    POST_INVOICE = "POST_INVOICE"
    RELEASE_BLOCK = "RELEASE_BLOCK"
    PROPOSE_CORRECTION = "PROPOSE_CORRECTION"
    ESCALATE = "ESCALATE"


class CorrectionType(StrEnum):
    AMEND_INVOICE_QUANTITY = "AMEND_INVOICE_QUANTITY"
    AMEND_INVOICE_PRICE = "AMEND_INVOICE_PRICE"
    RELEASE_INVOICE_BLOCK = "RELEASE_INVOICE_BLOCK"
    REJECT_INVOICE = "REJECT_INVOICE"


class AmendQuantityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_type: Literal["AMEND_INVOICE_QUANTITY"]
    invoice_number: str = Field(description="Invoice being corrected, e.g. 5100000901")
    inv_item_number: str = Field(description="The invoice line, 4 digits, e.g. 0001")
    from_quantity: str = Field(description="Quantity currently on the invoice, e.g. 14.000")
    to_quantity: str = Field(description="Quantity it should become, e.g. 13.000")


class AmendPricePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_type: Literal["AMEND_INVOICE_PRICE"]
    invoice_number: str
    inv_item_number: str = Field(description="The invoice line, 4 digits, e.g. 0001")
    from_price: str = Field(description="Unit price currently on the invoice, e.g. 46.10")
    to_price: str = Field(description="Unit price it should become, e.g. 41.90")


class ReleaseBlockPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_type: Literal["RELEASE_INVOICE_BLOCK"]
    invoice_number: str
    released_block_reason: str = Field(
        description="The block_reason currently on the invoice that is being lifted"
    )


class RejectInvoicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correction_type: Literal["REJECT_INVOICE"]
    invoice_number: str
    duplicate_of: str = Field(description="The earlier invoice this one duplicates")


#: Tagged union. `correction_type` selects the shape, so the model gets four
#: concrete option schemas rather than one bag of optional fields.
CorrectionPayload = Annotated[
    AmendQuantityPayload | AmendPricePayload | ReleaseBlockPayload | RejectInvoicePayload,
    Field(discriminator="correction_type"),
]


class Resolution(BaseModel):
    """The agent's final answer. Also the schema of the terminal tool.

    extra="forbid" so an invented field (a "confidence" the model felt like
    adding) is a validation error the model is told about, not a silent drop.
    """

    model_config = ConfigDict(extra="forbid")

    classification: Classification = Field(
        description="What you determined this invoice is, from the fixed list."
    )
    decision: Decision = Field(description="What should happen to the invoice.")
    reasoning: str = Field(
        min_length=1,
        description="Two or three sentences citing the numbers you compared.",
    )
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "The concrete facts you relied on, one per entry, each naming the "
            "document and field it came from. Example: "
            "'GR 5000000901 MENGE 13.000 (sum of receipts for PO line 00010)'."
        ),
    )

    # Only for PROPOSE_CORRECTION. `= None` makes them optional; the validator
    # below makes them conditionally required, which is the part a JSON Schema
    # cannot express on its own.
    correction: CorrectionPayload | None = Field(
        default=None,
        description="Required when decision is PROPOSE_CORRECTION. Omit otherwise.",
    )

    # Only for ESCALATE.
    escalate_to: str | None = Field(
        default=None, description="Required when decision is ESCALATE, e.g. 'AP_SUPERVISOR'."
    )
    escalation_reason: str | None = Field(
        default=None,
        description=(
            "Required when decision is ESCALATE. State precisely which fact is "
            "missing or which two facts conflict."
        ),
    )

    @model_validator(mode="after")
    def _decision_and_fields_agree(self) -> "Resolution":
        """Coherence rules a JSON Schema cannot state.

        These fire as ValidationError inside the tool call, and the loop hands
        the message straight back to the model -- so the schema teaches rather
        than just rejecting.
        """
        if self.decision is Decision.PROPOSE_CORRECTION:
            if self.correction is None:
                raise ValueError("decision=PROPOSE_CORRECTION requires a `correction` payload")
            if self.correction.invoice_number.strip() == "":
                raise ValueError("`correction.invoice_number` must not be empty")
        elif self.correction is not None:
            raise ValueError(
                f"`correction` is only valid with decision=PROPOSE_CORRECTION, "
                f"not {self.decision.value}"
            )

        if self.decision is Decision.ESCALATE:
            if not self.escalate_to:
                raise ValueError("decision=ESCALATE requires `escalate_to`")
            if not self.escalation_reason:
                raise ValueError("decision=ESCALATE requires `escalation_reason`")
        elif self.escalate_to or self.escalation_reason:
            raise ValueError(
                f"escalation fields are only valid with decision=ESCALATE, "
                f"not {self.decision.value}"
            )

        # A clean invoice that the agent also wants corrected is incoherent.
        if self.classification is Classification.CLEAN and self.decision not in (
            Decision.POST_INVOICE,
            Decision.RELEASE_BLOCK,
        ):
            raise ValueError(
                "classification=CLEAN is only compatible with POST_INVOICE or RELEASE_BLOCK"
            )
        if (
            self.classification is Classification.INSUFFICIENT_EVIDENCE
            and self.decision is not Decision.ESCALATE
        ):
            raise ValueError("classification=INSUFFICIENT_EVIDENCE requires decision=ESCALATE")

        return self
