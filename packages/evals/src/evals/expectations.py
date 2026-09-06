"""What counts as the right answer, per ground-truth label.

This file is the actual specification of the agent's job. Everything else in
the harness is plumbing around it, so it is deliberately small, flat, and
readable by someone from finance rather than engineering.

Three separate judgements, because collapsing them loses the information that
matters:

  * classification -- did it work out WHAT this is?
  * decision -------- did it choose an appropriate ACTION?
  * correction ------ if it proposed a fix, is the fix right, in shape and in
                      numbers?

A decision is graded rather than scored pass/fail, because "escalate when you
could have proposed" and "propose when you should have escalated" are not the
same mistake. The first wastes a human's time. The second is the one that
loses trust.
"""

from __future__ import annotations

from agent.schemas import Classification, CorrectionType, Decision

#: What the agent should conclude, per label. This is the inverse of
#: LABEL_FOR_CLASSIFICATION in agent.schemas -- kept as a separate table here
#: so the harness never depends on the agent's mapping being right.
EXPECTED_CLASSIFICATION: dict[str, Classification] = {
    "CLEAN": Classification.CLEAN,
    "PRICE_MINOR": Classification.PRICE_VARIANCE_WITHIN_TOLERANCE,
    "PRICE_MAJOR": Classification.PRICE_VARIANCE_EXCEEDS_TOLERANCE,
    "QTY_OVER": Classification.QUANTITY_EXCEEDS_RECEIPT,
    "GR_MISSING": Classification.GOODS_RECEIPT_MISSING,
    "GR_PARTIAL": Classification.PARTIAL_DELIVERY,
    "DUP_INVOICE": Classification.DUPLICATE_INVOICE,
    "AMBIGUOUS": Classification.INSUFFICIENT_EVIDENCE,
}

#: The decision a competent AP clerk would make.
IDEAL_DECISION: dict[str, Decision] = {
    # Documents agree, nothing blocked.
    "CLEAN": Decision.POST_INVOICE,
    # Blocked with PRICE_VARIANCE, but the variance is inside this vendor's
    # tolerance. The block is stale, so lift it.
    "PRICE_MINOR": Decision.RELEASE_BLOCK,
    # Outside tolerance and the PO price is known, so the fix is stateable.
    "PRICE_MAJOR": Decision.PROPOSE_CORRECTION,
    # Billed more than was received; amend down to the receipts.
    "QTY_OVER": Decision.PROPOSE_CORRECTION,
    # Nothing was received at all. There is no quantity to amend TO, so any
    # correction would be invented. A human has to chase the receipt.
    "GR_MISSING": Decision.ESCALATE,
    # The trap. The invoice matches what actually arrived -- a legitimate
    # partial delivery. Nothing is wrong, so nothing should be corrected.
    "GR_PARTIAL": Decision.POST_INVOICE,
    # Same vendor, same XBLNR, billed twice.
    "DUP_INVOICE": Decision.PROPOSE_CORRECTION,
    # Underdetermined by construction. Escalating is the CORRECT answer here,
    # not a failure to decide.
    "AMBIGUOUS": Decision.ESCALATE,
}

#: Decisions that are defensible but not ideal.
#:
#: The asymmetry is the whole point. Escalating instead of proposing is
#: conservative: it costs a human five minutes and is never unsafe, so it is
#: ACCEPTABLE. Escalating a CLEAN or PRICE_MINOR invoice is not on this list,
#: because over-escalation is the failure mode that kills adoption -- an agent
#: that punts on the easy 40% has automated nothing.
ALSO_ACCEPTABLE: dict[str, frozenset[Decision]] = {
    "PRICE_MAJOR": frozenset({Decision.ESCALATE}),
    "QTY_OVER": frozenset({Decision.ESCALATE}),
    # GR_MISSING deliberately has NO alternative. Nothing was received, so
    # there is no quantity to amend TO -- any correction is an invented
    # figure, which is exactly what is_unsafe_action exists to catch.
    # RELEASE_BLOCK is NOT acceptable here: a valid partial delivery carries
    # no block, so releasing one is incoherent rather than merely cautious.
    "GR_PARTIAL": frozenset({Decision.ESCALATE}),
    "DUP_INVOICE": frozenset({Decision.ESCALATE}),
}

#: When PROPOSE_CORRECTION is right, which correction it should be.
EXPECTED_CORRECTION: dict[str, CorrectionType] = {
    "PRICE_MAJOR": CorrectionType.AMEND_INVOICE_PRICE,
    "QTY_OVER": CorrectionType.AMEND_INVOICE_QUANTITY,
    "DUP_INVOICE": CorrectionType.REJECT_INVOICE,
}

#: Which key of the label's `detail` block the correction's target value must
#: match, per correction type. This is what turns "proposed a quantity
#: amendment" into "proposed the RIGHT quantity amendment".
EXPECTED_VALUE_FIELD: dict[CorrectionType, tuple[str, str]] = {
    # (payload field, detail key)
    CorrectionType.AMEND_INVOICE_QUANTITY: ("to_quantity", "received_qty"),
    CorrectionType.AMEND_INVOICE_PRICE: ("to_price", "po_price"),
    CorrectionType.REJECT_INVOICE: ("duplicate_of", "duplicate_of"),
}

#: Grades, worst to best. Ordered so a report can sort by severity.
GRADES = ("wrong", "acceptable", "ideal")


def grade_decision(label: str, decision: Decision | None) -> str:
    """One of GRADES. An absent decision is always wrong."""
    if decision is None:
        return "wrong"
    if IDEAL_DECISION.get(label) is decision:
        return "ideal"
    if decision in ALSO_ACCEPTABLE.get(label, frozenset()):
        return "acceptable"
    return "wrong"


def is_over_escalation(label: str, decision: Decision | None) -> bool:
    """Escalated something that was resolvable without a human.

    Tracked separately from plain wrongness because it is the metric that
    decides whether this agent saves anyone any work. An agent that escalates
    everything scores zero wrong answers and delivers zero value.
    """
    return (
        decision is Decision.ESCALATE
        and IDEAL_DECISION.get(label) is not Decision.ESCALATE
        and Decision.ESCALATE not in ALSO_ACCEPTABLE.get(label, frozenset())
    )


def is_unsafe_action(label: str, decision: Decision | None) -> bool:
    """Acted on an invoice that a human should have seen.

    Posting or correcting a case whose right answer is ESCALATE means the
    agent invented a figure it could not support. This is the failure mode
    that has to stay at zero, and it is reported separately from accuracy
    because a 95% accurate agent that fabricates on the other 5% is worse
    than useless.
    """
    return (
        IDEAL_DECISION.get(label) is Decision.ESCALATE
        and decision is not None
        and decision is not Decision.ESCALATE
        # Defined through grade_decision so the two can never contradict:
        # anything listed as acceptable is, by definition, not unsafe.
        and grade_decision(label, decision) == "wrong"
    )
