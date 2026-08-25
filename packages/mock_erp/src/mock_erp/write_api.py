from erp_domain.models import InvoiceNumber
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from mock_erp.odata import collection, entity, error_body
from mock_erp.proposals import CorrectionPayload
from mock_erp.repository import ProposalError, ProposalRepository


class ProposeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    invoice_number: InvoiceNumber
    payload: CorrectionPayload
    agent_reasoning: str
    # Optional: only proposals raised while running the eval set carry a
    # scenario_id. One raised from the review UI has nothing to link back
    # to, and requiring it would make those unrepresentable.
    # `| None` describes the TYPE; `= None` is what makes it OPTIONAL.
    scenario_id: str | None = None


class ApproveRequest(BaseModel):
    approved_by: str


class RejectRequest(BaseModel):
    rejected_by: str
    reason: str


class ApplyRequest(BaseModel):
    proposal_id: str
    payload: CorrectionPayload


def get_repository(request: Request) -> ProposalRepository:
    return request.app.state.repository


agent_router = APIRouter(prefix="/sap/opu/odata/sap/ZPROC_SRV")


@agent_router.post("/ProposeCorrection")
async def propose_correction(
    request: ProposeRequest, repo: ProposalRepository = Depends(get_repository)
):
    proposal = repo.create(
        invoice_number=request.invoice_number,
        payload=request.payload,
        agent_reasoning=request.agent_reasoning,
        scenario_id=request.scenario_id,
    )

    return entity(
        {
            "proposal_id": proposal.proposal_id,
            "status": "PROPOSED",
            # The stored hash, not a recomputed one: if hashing ever changes
            # on one side, this must diverge loudly rather than agree by luck.
            "payload_hash": proposal.payload_hash,
        }
    )


@agent_router.post("/ApplyCorrection")
async def apply_correction(
    request: ApplyRequest, repo: ProposalRepository = Depends(get_repository)
):
    res = repo.apply(request.proposal_id, request.payload)
    return entity(res.model_dump(mode="json"))


human_router = APIRouter(prefix="/approval")


@human_router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str, request: ApproveRequest, repo: ProposalRepository = Depends(get_repository)
):
    proposal = repo.approve(proposal_id, request.approved_by)
    return entity(proposal.model_dump(mode="json"))


@human_router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str, request: RejectRequest, repo: ProposalRepository = Depends(get_repository)
):
    proposal = repo.reject(proposal_id, request.rejected_by, request.reason)
    return entity(proposal.model_dump(mode="json"))


@human_router.get("/proposals")
async def list_proposals(
    status: str | None = None,
    repo: ProposalRepository = Depends(get_repository),
) -> dict:
    proposals = repo.list_proposals(status)
    return collection([p.model_dump(mode="json") for p in proposals])


async def proposal_exception_handler(
    request: Request,
    exc: ProposalError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=error_body(exc.code, exc.message),
    )
