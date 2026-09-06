"""Turning a proposal into something a human can check in ten seconds.

Pure functions over dicts. No I/O, no framework, no templates -- so the rules
about what a reviewer is shown can be unit-tested directly, and a bug in the
diff shows up as a failing assertion rather than as a misleading page.

The job of this module is one question: **can the reviewer see enough to
disagree with the agent?** An approval queue that only shows "the agent wants
to change something" trains people to click Approve, which converts a human
gate into a slower rubber stamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

#: Which fields carry the before/after for each correction type, and what to
#: call them on screen. Driven by a table so a new correction type cannot be
#: added without deciding how a human is meant to check it.
DIFF_SPEC: dict[str, tuple[str, str, str]] = {
    # keyed by correction type; value is the on-screen label, then the two
    # payload fields holding the before and after values
    "AMEND_INVOICE_QUANTITY": ("Invoiced quantity", "from_quantity", "to_quantity"),
    "AMEND_INVOICE_PRICE": ("Unit price", "from_price", "to_price"),
}

#: Correction types that change a header field rather than a line figure.
HEADER_SPEC: dict[str, tuple[str, str]] = {
    "RELEASE_INVOICE_BLOCK": ("Payment block", "released_block_reason"),
    "REJECT_INVOICE": ("Duplicate of", "duplicate_of"),
}

STATUS_ORDER = ("PROPOSED", "APPROVED", "APPLIED", "REJECTED")

#: What the reviewer can do from each state. The UI renders buttons from this,
#: so the screen and the state machine cannot drift apart -- a button that the
#: server will refuse is worse than no button.
ACTIONS_FOR_STATUS: dict[str, tuple[str, ...]] = {
    "PROPOSED": ("approve", "reject"),
    "APPROVED": ("apply",),
    "APPLIED": (),
    "REJECTED": (),
}


@dataclass(frozen=True)
class DiffRow:
    label: str
    before: str
    after: str
    #: True when the document no longer holds the value the agent proposed to
    #: change FROM. Rendered as a warning, because approving it would apply a
    #: correction computed against a document that has since moved.
    stale: bool = False


@dataclass
class ProposalView:
    proposal_id: str
    status: str
    invoice_number: str
    correction_type: str
    agent_reasoning: str
    scenario_id: str | None
    proposed_at: str
    payload: dict = field(default_factory=dict)
    payload_hash: str = ""
    approved_by: str | None = None
    approved_at: str | None = None
    rejected_by: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    applied_at: str | None = None
    diff: list[DiffRow] = field(default_factory=list)
    actions: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def is_stale(self) -> bool:
        return any(row.stale for row in self.diff)


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _same(left: object, right: object) -> bool:
    """Compare as numbers when both are numeric, else as text.

    "13.0" and "13.000" are the same quantity. Flagging that as staleness
    would put a scary warning on a perfectly good proposal, and a warning
    that cries wolf is worse than no warning.
    """
    a, b = _decimal(left), _decimal(right)
    if a is not None and b is not None:
        return a == b
    return str(left).strip() == str(right).strip()


def _invoice_line(invoice: dict, inv_item_number: str) -> dict:
    for line in invoice.get("items") or []:
        if line.get("BUZEI") == inv_item_number:
            return line
    return {}


def build_diff(payload: dict, invoice: dict | None) -> list[DiffRow]:
    """What this correction would change, as before/after rows.

    `invoice` is the EFFECTIVE document -- base plus any amendments already
    applied -- so staleness is measured against what the reviewer is actually
    looking at, not against the base on disk.
    """
    correction_type = payload.get("correction_type", "")

    if correction_type in DIFF_SPEC:
        label, from_field, to_field = DIFF_SPEC[correction_type]
        before = payload.get(from_field, "")
        after = payload.get(to_field, "")
        stale = False
        if invoice is not None:
            line = _invoice_line(invoice, payload.get("inv_item_number", ""))
            current = line.get("MENGE" if "quantity" in from_field else "NETPR")
            stale = current is not None and not _same(current, before)
        return [DiffRow(label=label, before=str(before), after=str(after), stale=stale)]

    if correction_type in HEADER_SPEC:
        label, field_name = HEADER_SPEC[correction_type]
        value = str(payload.get(field_name, ""))
        if correction_type == "RELEASE_INVOICE_BLOCK":
            stale = invoice is not None and not _same(invoice.get("block_reason") or "", value)
            return [DiffRow(label=label, before=value, after="(none)", stale=stale)]
        return [DiffRow(label=label, before="(not marked)", after=value)]

    # An unknown correction type must still render something. Showing the raw
    # payload is honest; showing nothing would hide a change from the person
    # whose job is to see it.
    return [
        DiffRow(label=key, before="", after=str(value))
        for key, value in sorted(payload.items())
        if key not in ("correction_type", "invoice_number")
    ]


def build_view(proposal: dict, invoice: dict | None = None) -> ProposalView:
    payload = proposal.get("payload") or {}
    diff = build_diff(payload, invoice)
    status = str(proposal.get("status", ""))

    warnings: list[str] = []
    if any(row.stale for row in diff):
        warnings.append(
            "The invoice no longer holds the value this correction was computed from. "
            "Applying it will be refused by the ERP; the agent should re-propose."
        )
    if invoice is None:
        warnings.append("The current invoice could not be read, so nothing is verified below.")

    return ProposalView(
        proposal_id=str(proposal.get("proposal_id", "")),
        status=status,
        invoice_number=str(proposal.get("invoice_number", "")),
        correction_type=str(payload.get("correction_type", "")),
        agent_reasoning=str(proposal.get("agent_reasoning", "")),
        scenario_id=proposal.get("scenario_id"),
        proposed_at=str(proposal.get("proposed_at", "")),
        payload=payload,
        payload_hash=str(proposal.get("payload_hash", "")),
        approved_by=proposal.get("approved_by"),
        approved_at=proposal.get("approved_at"),
        rejected_by=proposal.get("rejected_by"),
        rejected_at=proposal.get("rejected_at"),
        rejection_reason=proposal.get("rejection_reason"),
        applied_at=proposal.get("applied_at"),
        diff=diff,
        actions=ACTIONS_FOR_STATUS.get(status, ()),
        warnings=warnings,
    )


def queue_counts(proposals: list[dict]) -> dict[str, int]:
    """Counts per status, in a fixed order so the tabs never reshuffle."""
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for proposal in proposals:
        status = str(proposal.get("status", ""))
        if status in counts:
            counts[status] += 1
    return counts


def evidence_rows(
    invoice: dict | None, po: dict | None, receipts: list[dict] | None, inv_item_number: str
) -> dict:
    """The three-way match, laid out for a human to re-derive in one glance.

    Everything the AGENT looked at, shown side by side -- so the reviewer is
    checking the evidence rather than checking the agent's summary of it.
    """
    line = _invoice_line(invoice or {}, inv_item_number)
    ebelp = line.get("EBELP")
    po_line = next(
        (item for item in (po or {}).get("items") or [] if item.get("EBELP") == ebelp), {}
    )
    matching = [gr for gr in (receipts or []) if gr.get("EBELP") == ebelp]
    received = sum((_decimal(gr.get("MENGE")) or Decimal(0) for gr in matching), Decimal(0))

    tolerance = (po or {}).get("ToleranceConfig") or {}
    return {
        # A reviewer approving money movement wants to know who is being paid.
        "supplier": (invoice or {}).get("LIFNR"),
        "vendor_ref": (invoice or {}).get("XBLNR"),
        "invoice_date": (invoice or {}).get("BLDAT"),
        "po_number": (po or {}).get("EBELN"),
        "po_item": ebelp,
        "ordered_qty": po_line.get("MENGE"),
        "po_price": po_line.get("NETPR"),
        "received_qty": str(received) if matching else None,
        "receipts": matching,
        "invoiced_qty": line.get("MENGE"),
        "invoiced_price": line.get("NETPR"),
        "block_reason": (invoice or {}).get("block_reason"),
        "price_tolerance": tolerance.get("PriceVariancePct"),
        "quantity_tolerance": tolerance.get("QuantityVariancePct"),
        "tolerance_source": tolerance.get("Source"),
    }
