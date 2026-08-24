import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from erp_domain.models import InvoiceItemNumber, InvoiceNumber
from pydantic import BaseModel, ConfigDict, Field


class PayloadBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

class ProposalStatus(StrEnum):
    PROPOSED: str = "PROPOSED"
    APPROVED: str = "APPROVED"
    REJECTED: str = "REJECTED"
    APPLIED: str = "APPLIED"
    
class ProposalAction(StrEnum):
    APPROVE: str = "APPROVE"
    REJECT: str = "REJECT"
    APPLY: str = "APPLY"

class CorrectionType(StrEnum):
    AMEND_INVOICE_QUANTITY: str = "AMEND_INVOICE_QUANTITY"
    AMEND_INVOICE_PRICE: str = "AMEND_INVOICE_PRICE"
    RELEASE_INVOICE_BLOCK: str = "RELEASE_INVOICE_BLOCK"
    REJECT_INVOICE: str = "REJECT_INVOICE"
    
class AmendQuantityPayload(PayloadBase):
    correction_type: Literal["AMEND_INVOICE_QUANTITY"]
    invoice_number: InvoiceNumber
    inv_item_number: InvoiceItemNumber
    from_quantity: str
    to_quantity: str

class AmendPricePayload(PayloadBase):
    correction_type: Literal["AMEND_INVOICE_PRICE"]
    invoice_number: InvoiceNumber
    inv_item_number: InvoiceItemNumber
    from_price: str
    to_price: str    
    
class ReleaseBlockPayload(PayloadBase):
    correction_type: Literal["RELEASE_INVOICE_BLOCK"]
    invoice_number: InvoiceNumber
    released_block_reason: str
    
class RejectInvoicePayload(PayloadBase):
    correction_type: Literal["REJECT_INVOICE"]
    invoice_number: InvoiceNumber
    duplicate_of: InvoiceNumber
    
CorrectionPayload = Annotated[
    AmendQuantityPayload | AmendPricePayload | ReleaseBlockPayload | RejectInvoicePayload, 
    Field(discriminator="correction_type")
]

class Proposal(PayloadBase):
    proposal_id: str
    status: ProposalStatus
    scenario_id: str | None = None
    invoice_number: InvoiceNumber
    payload: CorrectionPayload
    payload_hash: str
    agent_reasoning: str
    proposed_by: str = "agent"
    proposed_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    approved_hash: str | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    applied_at: datetime | None = None
    
ALLOWED_TRANSITIONS = {
    (ProposalStatus.PROPOSED, ProposalAction.APPROVE): ProposalStatus.APPROVED,
    (ProposalStatus.PROPOSED, ProposalAction.REJECT): ProposalStatus.REJECTED,
    (ProposalStatus.APPROVED, ProposalAction.APPLY): ProposalStatus.APPLIED
}

def canonical_json(payload: CorrectionPayload) -> bytes:
    payload_data = payload.model_dump(mode='json')
    payload_json = json.dumps(
        payload_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return payload_json.encode("utf-8")

def payload_hash(payload: CorrectionPayload) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()

