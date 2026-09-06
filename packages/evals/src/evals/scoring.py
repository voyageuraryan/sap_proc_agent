"""Turn one AgentRun into one scored row.

Pure functions over data that already exists -- no I/O, no clock, no network.
That is what lets the scoring rules be tested against hand-built runs instead
of against a live model, and it is why a scoring bug shows up as a failing
unit test rather than as a suspicious number in a report.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from agent.loop import AgentRun, StopReason
from agent.schemas import Classification, CorrectionType, Decision

from evals.dataset import EvalCase
from evals.expectations import (
    EXPECTED_CLASSIFICATION,
    EXPECTED_CORRECTION,
    EXPECTED_VALUE_FIELD,
    grade_decision,
    is_over_escalation,
    is_unsafe_action,
)


@dataclass(frozen=True)
class CaseResult:
    """Everything worth knowing about one scenario, flattened for a table."""

    scenario_id: str
    invoice_number: str
    label: str
    variant: str | None

    # -- what the agent said ------------------------------------------------
    stop_reason: str
    classification: str | None
    decision: str | None
    correction_type: str | None
    reasoning: str = ""
    evidence_count: int = 0

    # -- how it scored ------------------------------------------------------
    classification_correct: bool = False
    decision_grade: str = "wrong"
    #: None when no correction was proposed -- distinct from False, which
    #: means one was proposed and it was the wrong kind.
    correction_type_correct: bool | None = None
    #: None when the value could not be checked (no correction, or the label
    #: carries no figure to check against).
    correction_value_correct: bool | None = None
    over_escalated: bool = False
    unsafe_action: bool = False

    # -- what it cost -------------------------------------------------------
    iterations: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_usd: Decimal | None = None
    duration_ms: float = 0.0

    #: Set when the run itself failed rather than answered wrongly.
    error: str | None = None

    @property
    def submitted(self) -> bool:
        return self.stop_reason == StopReason.SUBMITTED.value

    @property
    def decision_ok(self) -> bool:
        """Ideal or acceptable. The headline number."""
        return self.decision_grade in ("ideal", "acceptable")


def _decimals_match(left: str | None, right: str | None) -> bool | None:
    """Compare as numbers, not strings.

    "13.0" and "13.000" are the same quantity and a model may emit either.
    Comparing the strings would fail a correct answer on formatting, which
    would make the eval measure the wrong thing.
    """
    if left is None or right is None:
        return None
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        # Not a number on either side (duplicate_of is a document number), so
        # fall back to exact text.
        return str(left).strip() == str(right).strip()


def score(case: EvalCase, run: AgentRun, *, duration_ms: float = 0.0) -> CaseResult:
    """Score one run against its ground truth. Never raises."""
    resolution = run.resolution
    base = {
        "scenario_id": case.scenario_id,
        "invoice_number": case.invoice_number,
        "label": case.label,
        "variant": case.variant,
        "stop_reason": run.stop_reason.value,
        "iterations": run.iterations,
        "tool_calls": len(run.tool_calls),
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "total_usd": run.total_usd,
        "duration_ms": duration_ms,
    }

    if resolution is None:
        # A run that never submitted is scored as wrong on everything. It is
        # NOT skipped: an agent that crashes on the hard cases would otherwise
        # score better than one that answers them badly.
        return CaseResult(
            **base,
            classification=None,
            decision=None,
            correction_type=None,
            error=run.stop_reason.value,
        )

    classification: Classification = resolution.classification
    decision: Decision = resolution.decision
    correction = resolution.correction
    correction_type = correction.correction_type if correction is not None else None

    expected_correction = EXPECTED_CORRECTION.get(case.label)
    type_correct: bool | None = None
    value_correct: bool | None = None
    if correction is not None:
        type_correct = expected_correction is not None and correction_type == str(
            expected_correction
        )
        if type_correct:
            value_correct = _check_value(case, correction, CorrectionType(correction_type))

    return CaseResult(
        **base,
        classification=str(classification),
        decision=str(decision),
        correction_type=str(correction_type) if correction_type else None,
        reasoning=resolution.reasoning,
        evidence_count=len(resolution.evidence),
        classification_correct=EXPECTED_CLASSIFICATION.get(case.label) is classification,
        decision_grade=grade_decision(case.label, decision),
        correction_type_correct=type_correct,
        correction_value_correct=value_correct,
        over_escalated=is_over_escalation(case.label, decision),
        unsafe_action=is_unsafe_action(case.label, decision),
    )


def _check_value(case: EvalCase, correction, correction_type: CorrectionType) -> bool | None:
    """Did the correction carry the right NUMBER, not just the right shape?

    This is the difference between "proposed a quantity amendment" and
    "proposed amending 14.000 down to the 13.000 that actually arrived". A
    correction with the right type and a fabricated figure is the worst
    possible output, because it looks right to a human skimming an approval
    queue.
    """
    mapping = EXPECTED_VALUE_FIELD.get(correction_type)
    if mapping is None:
        return None
    payload_field, detail_key = mapping
    expected = case.detail.get(detail_key)
    if expected is None:
        return None
    actual = getattr(correction, payload_field, None)
    return _decimals_match(actual, expected)
