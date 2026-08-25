import json
import sqlite3
from datetime import UTC, datetime

from erp_domain.models import InvoiceNumber
from pydantic import BaseModel, ConfigDict

from mock_erp.overlay import apply_amendments, find_stale_conflict
from mock_erp.proposals import (
    ALLOWED_TRANSITIONS,
    CorrectionPayload,
    CorrectionType,
    Proposal,
    ProposalAction,
    ProposalStatus,
    canonical_json,
    payload_hash,
)
from mock_erp.store import ErpStore


class ProposalError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code = code
        self.message = message
        self.status = status


class AppliedResult(BaseModel):
    proposal_id: str
    status: ProposalStatus
    applied_at: datetime
    already_applied: bool = False


class AppliedAmendment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    proposal_id: str
    invoice_number: InvoiceNumber
    correction_type: CorrectionType
    payload_json: CorrectionPayload
    applied_at: datetime


class ProposalRepository:
    def __init__(self, conn: sqlite3.Connection, store: ErpStore):
        self.conn = conn
        self.store = store

    def create(
        self,
        invoice_number: InvoiceNumber,
        payload: CorrectionPayload,
        agent_reasoning: str,
        scenario_id: str | None,
    ) -> Proposal:
        store = self.store
        if invoice_number not in store.invoices:
            raise ProposalError(
                code="INVOICE_UNKNOWN",
                message=f"The invoice number {invoice_number} is unknown.",
                status=400,
            )
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO proposals (
                    proposal_id,
                    invoice_number,
                    status,
                    payload_hash,
                    agent_reasoning,
                    scenario_id,
                    proposed_at,
                    payload_json,
                    proposed_by
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "PENDING",
                    invoice_number,
                    ProposalStatus.PROPOSED.value,
                    payload_hash(payload),
                    agent_reasoning,
                    scenario_id,
                    datetime.now(UTC).isoformat(),
                    payload.model_dump_json(),
                    "agent",
                ),
            )

            sequence_id = cursor.lastrowid

            proposal_id = f"PR-{sequence_id:06d}"
            self.conn.execute(
                """
                UPDATE proposals
                    SET 
                    proposal_id = ?
                    WHERE sequence_id = ?
                """,
                (proposal_id, sequence_id),
            )
        return Proposal(
            status=ProposalStatus.PROPOSED,
            invoice_number=invoice_number,
            proposal_id=proposal_id,
            proposed_by="agent",
            payload=payload,
            payload_hash=payload_hash(payload),
            proposed_at=datetime.now(UTC),
            agent_reasoning=agent_reasoning,
            scenario_id=scenario_id,
        )

    def get(self, proposal_id) -> Proposal:
        cursor = self.conn.execute(
            """
                    SELECT * 
                    FROM proposals 
                    WHERE proposal_id = ?
                    """,
            (proposal_id,),
        )
        row = cursor.fetchone()

        if row is None:
            raise ProposalError(
                code="PROPOSAL_NOT_FOUND", message="The proposal request is not found", status=404
            )
        return Proposal(
            proposal_id=row["proposal_id"],
            status=row["status"],
            scenario_id=row["scenario_id"],
            invoice_number=row["invoice_number"],
            payload=json.loads(row["payload_json"]),
            payload_hash=row["payload_hash"],
            agent_reasoning=row["agent_reasoning"],
            proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            approved_hash=row["approved_hash"],
            rejected_by=row["rejected_by"],
            rejected_at=row["rejected_at"],
            rejection_reason=row["rejection_reason"],
            applied_at=row["applied_at"],
        )

    def _transition(self, proposal: Proposal, action: ProposalAction) -> ProposalStatus:
        if (proposal.status, action) in ALLOWED_TRANSITIONS:
            return ALLOWED_TRANSITIONS[(proposal.status, action)]
        raise ProposalError(
            code="ILLEGAL_TRANSITION",
            message=f"The combination of proposal{proposal.status} and {action} is not permitted",
            status=409,
        )

    def approve(self, proposal_id, approved_by) -> Proposal:
        proposal = self.get(proposal_id)
        proposal.status = new_status = self._transition(proposal, ProposalAction.APPROVE)
        proposal.approved_by = approved_by
        approved_at = proposal.approved_at = datetime.now(UTC)
        approved_hash = proposal.approved_hash = proposal.payload_hash

        with self.conn:
            self.conn.execute(
                """
                UPDATE proposals 
                SET
                approved_by = ?,
                approved_at = ?,
                approved_hash = ?,
                status = ?
                WHERE proposal_id = ?;
                """,
                (approved_by, approved_at, approved_hash, new_status, proposal_id),
            )

        return proposal

    def reject(self, proposal_id, rejected_by, reason) -> Proposal:
        proposal = self.get(proposal_id)
        new_status = proposal.status = self._transition(proposal, ProposalAction.REJECT)
        proposal.rejected_by = rejected_by
        rejected_at = proposal.rejected_at = datetime.now(UTC)
        proposal.rejection_reason = reason
        with self.conn:
            self.conn.execute(
                """
                UPDATE proposals 
                SET
                rejected_by = ?,
                rejected_at = ?,
                rejection_reason = ?,
                status = ?
                WHERE proposal_id = ?;
                """,
                (rejected_by, rejected_at, reason, new_status, proposal_id),
            )

        return proposal

    def apply(self, proposal_id, payload: CorrectionPayload) -> AppliedResult:
        proposal = self.get(proposal_id)
        if (
            proposal.status == ProposalStatus.APPLIED
            and payload_hash(payload) == proposal.approved_hash
        ):
            return AppliedResult(
                proposal_id=proposal_id,
                status=proposal.status,
                applied_at=proposal.applied_at,
                already_applied=True,
            )
        new_status = self._transition(proposal, ProposalAction.APPLY)
        if payload_hash(payload) != proposal.approved_hash:
            raise ProposalError(
                code="PAYLOAD_MISMATCH",
                message=(
                    "payload does not match the payload that was approved"
                ),
                status=409,
            )

        effective = apply_amendments(
            self.store.invoices[proposal.invoice_number],
            [a.payload_json for a in self.amendments_for_invoice(proposal.invoice_number)],
        )

        problem = find_stale_conflict(effective, payload)
        if problem:
            raise ProposalError(code=problem[0], message=problem[1], status=409)
        applied_at = datetime.now(UTC)

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO amendments(
                    proposal_id,
                    invoice_number,
                    correction_type,
                    payload_json,
                    applied_at
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.invoice_number,
                    payload.correction_type,
                    canonical_json(payload).decode("utf-8"),
                    applied_at.isoformat(),
                ),
            )

            self.conn.execute(
                """
                UPDATE proposals
                SET
                status = ?,
                applied_at = ?
                WHERE proposal_id = ?;
                """,
                (new_status, applied_at.isoformat(), proposal_id),
            )
        return AppliedResult(
            proposal_id=proposal_id, status=new_status, applied_at=applied_at, already_applied=False
        )

    def amendments_for_invoice(self, invoice_number) -> list[AppliedAmendment]:

        cursor = self.conn.execute(
            """
            SELECT
                proposal_id,
                invoice_number,
                correction_type,
                payload_json,
                applied_at
            FROM amendments
            WHERE invoice_number = ?
            ORDER BY applied_at
            """,
            (invoice_number,),
        )

        rows = cursor.fetchall()
        return [
            AppliedAmendment(
                proposal_id=row["proposal_id"],
                invoice_number=row["invoice_number"],
                correction_type=row["correction_type"],
                payload_json=json.loads(row["payload_json"]),
                applied_at=row["applied_at"],
            )
            for row in rows
        ]

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        #     )
        if status is None:
            cursor = self.conn.execute("SELECT * FROM proposals ORDER BY sequence_id")
        else:
            cursor = self.conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY sequence_id",
                (status,),
            )

        rows = cursor.fetchall()
        return [
            Proposal(
                proposal_id=row["proposal_id"],
                status=row["status"],
                scenario_id=row["scenario_id"],
                invoice_number=row["invoice_number"],
                payload=json.loads(row["payload_json"]),
                payload_hash=row["payload_hash"],
                agent_reasoning=row["agent_reasoning"],
                proposed_by=row["proposed_by"],
                proposed_at=row["proposed_at"],
                approved_by=row["approved_by"],
                approved_at=row["approved_at"],
                approved_hash=row["approved_hash"],
                rejected_by=row["rejected_by"],
                rejected_at=row["rejected_at"],
                rejection_reason=row["rejection_reason"],
                applied_at=row["applied_at"],
            )
            for row in rows
        ]
