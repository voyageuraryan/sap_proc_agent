"""Scoring one run against one case. Pure functions, hand-built inputs.

Every AgentRun here is constructed by hand rather than produced by a model,
because the thing under test is the RULE, and a rule tested through a model is
a rule tested through noise.
"""

from decimal import Decimal

import pytest
from agent.loop import AgentRun, LlmCallRecord, StopReason, ToolCallRecord
from agent.schemas import Classification, Decision, Resolution
from evals.dataset import EvalCase
from evals.scoring import score

QTY_CASE = EvalCase(
    scenario_id="SC-0009",
    invoice_number="5100000901",
    label="QTY_OVER",
    detail={"invoiced_qty": "14.000", "received_qty": "13.000", "po_qty": "14.000"},
    all_invoice_numbers=("5100000901",),
)
CLEAN_CASE = EvalCase(
    scenario_id="SC-0001",
    invoice_number="5100000101",
    label="CLEAN",
    all_invoice_numbers=("5100000101",),
)
AMBIGUOUS_CASE = EvalCase(
    scenario_id="SC-0017",
    invoice_number="5100001701",
    label="AMBIGUOUS",
    variant="DANGLING_PO_LINE",
    all_invoice_numbers=("5100001701",),
)


def _run(resolution: Resolution | None, **kwargs) -> AgentRun:
    defaults = {
        "invoice_number": "5100000901",
        "model": "test/scripted",
        "stop_reason": StopReason.SUBMITTED if resolution else StopReason.MAX_ITERATIONS,
        "iterations": 4,
        "resolution": resolution,
        "tool_calls": [ToolCallRecord(name="get_invoice")],
        "llm_calls": [LlmCallRecord(iteration=1, model="test/scripted")],
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_usd": Decimal("0.01"),
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)


def _resolution(**kwargs) -> Resolution:
    defaults = {
        "classification": Classification.QUANTITY_EXCEEDS_RECEIPT,
        "decision": Decision.PROPOSE_CORRECTION,
        "reasoning": "Invoiced 14.000 against receipts of 13.000.",
        "evidence": ["INV MENGE 14.000", "GR MENGE 13.000"],
        "correction": {
            "correction_type": "AMEND_INVOICE_QUANTITY",
            "invoice_number": "5100000901",
            "inv_item_number": "0001",
            "from_quantity": "14.000",
            "to_quantity": "13.000",
        },
    }
    defaults.update(kwargs)
    return Resolution(**defaults)


def test_a_perfect_answer_scores_perfectly():
    result = score(QTY_CASE, _run(_resolution()))
    assert result.classification_correct
    assert result.decision_grade == "ideal"
    assert result.correction_type_correct is True
    assert result.correction_value_correct is True
    assert not result.over_escalated
    assert not result.unsafe_action
    assert result.submitted


def test_the_right_shape_with_a_fabricated_figure_is_caught():
    """The worst possible output: it looks right in an approval queue.

    Type-correct and value-wrong has to be distinguishable from type-wrong,
    or 'proposed a quantity amendment' passes for 'proposed the right one'.
    """
    correction = dict(_resolution().correction.model_dump(mode="json"), to_quantity="9.000")
    result = score(QTY_CASE, _run(_resolution(correction=correction)))
    assert result.correction_type_correct is True
    assert result.correction_value_correct is False
    # It still counts as an ideal DECISION -- the two are graded separately on
    # purpose, because they have different fixes.
    assert result.decision_grade == "ideal"


def test_the_wrong_correction_type_is_not_value_checked():
    correction = {
        "correction_type": "AMEND_INVOICE_PRICE",
        "invoice_number": "5100000901",
        "inv_item_number": "0001",
        "from_price": "41.90",
        "to_price": "40.00",
    }
    result = score(QTY_CASE, _run(_resolution(correction=correction)))
    assert result.correction_type_correct is False
    assert result.correction_value_correct is None


def test_quantities_are_compared_as_numbers_not_strings():
    """13.0 and 13.000 are the same quantity; failing that would measure
    formatting rather than correctness."""
    correction = dict(_resolution().correction.model_dump(mode="json"), to_quantity="13.0")
    assert score(QTY_CASE, _run(_resolution(correction=correction))).correction_value_correct


def test_no_correction_leaves_the_correction_checks_unset():
    """None means 'nothing was proposed'; False would mean 'the wrong thing was'."""
    result = score(
        QTY_CASE,
        _run(
            _resolution(
                decision=Decision.ESCALATE,
                correction=None,
                escalate_to="AP_SUPERVISOR",
                escalation_reason="unsure",
            )
        ),
    )
    assert result.correction_type_correct is None
    assert result.correction_value_correct is None
    assert result.decision_grade == "acceptable"


def test_a_run_that_never_submitted_is_scored_wrong_not_skipped():
    """An agent that crashes on the hard cases must not outscore one that
    answers them badly."""
    result = score(QTY_CASE, _run(None))
    assert not result.submitted
    assert not result.classification_correct
    assert result.decision_grade == "wrong"
    assert result.decision is None
    assert result.error == "MAX_ITERATIONS"
    # Cost is still counted: a failed run is not a free run.
    assert result.prompt_tokens == 1000


def test_escalating_a_clean_invoice_is_over_escalation_not_safety():
    """Note the classification: the agent's own Resolution validator refuses
    CLEAN + ESCALATE outright, so a punt on an easy case necessarily arrives
    mislabelled. The schema already blocks the incoherent version."""
    result = score(
        CLEAN_CASE,
        _run(
            _resolution(
                classification=Classification.INSUFFICIENT_EVIDENCE,
                decision=Decision.ESCALATE,
                correction=None,
                escalate_to="AP_SUPERVISOR",
                escalation_reason="not sure",
            )
        ),
    )
    assert result.over_escalated
    assert not result.unsafe_action
    assert result.decision_grade == "wrong"


def test_acting_on_an_ambiguous_case_is_flagged_unsafe():
    result = score(
        AMBIGUOUS_CASE,
        _run(
            _resolution(
                classification=Classification.QUANTITY_EXCEEDS_RECEIPT,
                decision=Decision.PROPOSE_CORRECTION,
            )
        ),
    )
    assert result.unsafe_action
    assert result.decision_grade == "wrong"


def test_classification_and_decision_are_scored_independently():
    """Right action, wrong name: worth knowing, and not the same failure."""
    result = score(
        QTY_CASE,
        _run(_resolution(classification=Classification.PARTIAL_DELIVERY)),
    )
    assert not result.classification_correct
    assert result.decision_grade == "ideal"


def test_the_duplicate_target_is_checked_against_the_original():
    case = EvalCase(
        scenario_id="SC-0006",
        invoice_number="5100000602",
        label="DUP_INVOICE",
        detail={"duplicate_of": "5100000601", "duplicate": "5100000602"},
        all_invoice_numbers=("5100000601", "5100000602"),
    )
    good = _resolution(
        classification=Classification.DUPLICATE_INVOICE,
        correction={
            "correction_type": "REJECT_INVOICE",
            "invoice_number": "5100000602",
            "duplicate_of": "5100000601",
        },
    )
    assert score(case, _run(good)).correction_value_correct is True

    bad = _resolution(
        classification=Classification.DUPLICATE_INVOICE,
        correction={
            "correction_type": "REJECT_INVOICE",
            "invoice_number": "5100000602",
            "duplicate_of": "5100009999",
        },
    )
    assert score(case, _run(bad)).correction_value_correct is False


@pytest.mark.parametrize("field", ["iterations", "prompt_tokens", "completion_tokens"])
def test_cost_and_effort_are_carried_through(field):
    result = score(QTY_CASE, _run(_resolution()))
    assert getattr(result, field) > 0
    assert result.total_usd == Decimal("0.01")
