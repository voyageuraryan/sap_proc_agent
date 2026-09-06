"""The expectation tables are the specification. These check them for coherence.

A scoring table with a contradiction in it produces confident, wrong numbers
and nothing fails -- which is the same class of bug as the duplicated enum
value in Step 6, and the reason these are asserted rather than reviewed.
"""

import pytest
from agent.schemas import Classification, Decision
from evals.expectations import (
    ALSO_ACCEPTABLE,
    EXPECTED_CLASSIFICATION,
    EXPECTED_CORRECTION,
    EXPECTED_VALUE_FIELD,
    GRADES,
    IDEAL_DECISION,
    grade_decision,
    is_over_escalation,
    is_unsafe_action,
)

LABELS = tuple(IDEAL_DECISION)


def test_every_label_has_an_ideal_decision_and_a_classification():
    assert set(IDEAL_DECISION) == set(EXPECTED_CLASSIFICATION)
    assert len(LABELS) == 8


def test_the_expected_classifications_are_distinct():
    """Two labels mapping to one classification would make them unscoreable apart."""
    values = list(EXPECTED_CLASSIFICATION.values())
    assert len(set(values)) == len(values)
    assert set(values) <= set(Classification)


def test_the_expectation_table_is_the_inverse_of_the_agents_own_mapping():
    """Held separately on purpose, so the harness does not inherit an agent bug.

    They must still agree -- if they ever diverge, one of them is wrong and
    this says so rather than letting the eval score against the agent's own
    opinion of what it meant.
    """
    from agent.schemas import LABEL_FOR_CLASSIFICATION

    for label, classification in EXPECTED_CLASSIFICATION.items():
        assert LABEL_FOR_CLASSIFICATION[classification] == label


def test_no_acceptable_decision_is_also_the_ideal_one():
    """Overlap would make 'ideal' and 'acceptable' counts double-count."""
    for label, alternatives in ALSO_ACCEPTABLE.items():
        assert IDEAL_DECISION[label] not in alternatives, label


def test_every_acceptable_set_names_a_real_label():
    assert set(ALSO_ACCEPTABLE) <= set(LABELS)


@pytest.mark.parametrize("label", LABELS)
def test_the_ideal_decision_grades_as_ideal(label):
    assert grade_decision(label, IDEAL_DECISION[label]) == "ideal"


@pytest.mark.parametrize("label", LABELS)
def test_every_decision_gets_exactly_one_grade(label):
    for decision in Decision:
        assert grade_decision(label, decision) in GRADES


def test_no_answer_at_all_is_wrong():
    """A crashed run must not score better than a wrong one."""
    for label in LABELS:
        assert grade_decision(label, None) == "wrong"


@pytest.mark.parametrize("label", LABELS)
def test_unsafe_and_acceptable_can_never_both_be_true(label):
    """The contradiction this table actually had: GR_MISSING was listed as
    accepting PROPOSE_CORRECTION while also flagging it as unsafe."""
    for decision in Decision:
        if is_unsafe_action(label, decision):
            assert grade_decision(label, decision) == "wrong", (label, decision)


@pytest.mark.parametrize("label", LABELS)
def test_over_escalation_is_only_flagged_where_escalating_is_wrong(label):
    if is_over_escalation(label, Decision.ESCALATE):
        assert grade_decision(label, Decision.ESCALATE) == "wrong"
        assert IDEAL_DECISION[label] is not Decision.ESCALATE


def test_escalating_is_never_unsafe():
    """Escalating costs a human five minutes. It is never the dangerous choice."""
    for label in LABELS:
        assert not is_unsafe_action(label, Decision.ESCALATE)


def test_the_two_labels_that_require_a_human_are_the_ones_with_nothing_to_act_on():
    """GR_MISSING has no received quantity; AMBIGUOUS is underdetermined by
    construction. Anything else the agent should be able to resolve."""
    require_human = {lbl for lbl in LABELS if IDEAL_DECISION[lbl] is Decision.ESCALATE}
    assert require_human == {"GR_MISSING", "AMBIGUOUS"}
    for label in require_human:
        assert label not in ALSO_ACCEPTABLE


def test_the_easy_majority_has_no_escalation_escape_hatch():
    """CLEAN and PRICE_MINOR are 50% of the queue. An agent allowed to punt on
    them scores no wrong answers and automates nothing."""
    for label in ("CLEAN", "PRICE_MINOR"):
        assert Decision.ESCALATE not in ALSO_ACCEPTABLE.get(label, frozenset())
        assert is_over_escalation(label, Decision.ESCALATE)


def test_gr_partial_must_not_be_corrected():
    """The trap the whole eval exists to measure: a valid partial delivery
    looks like an over-invoice and is not one."""
    assert IDEAL_DECISION["GR_PARTIAL"] is Decision.POST_INVOICE
    assert grade_decision("GR_PARTIAL", Decision.PROPOSE_CORRECTION) == "wrong"
    assert grade_decision("QTY_OVER", Decision.PROPOSE_CORRECTION) == "ideal"


def test_every_proposable_label_names_its_correction_and_a_value_to_check():
    proposable = {lbl for lbl in LABELS if IDEAL_DECISION[lbl] is Decision.PROPOSE_CORRECTION}
    assert set(EXPECTED_CORRECTION) == proposable
    for correction in EXPECTED_CORRECTION.values():
        assert correction in EXPECTED_VALUE_FIELD, correction


def test_release_block_is_only_ideal_where_the_invoice_is_actually_blocked():
    """PRICE_MINOR is the one label whose invoices carry a stale block."""
    release = {lbl for lbl in LABELS if IDEAL_DECISION[lbl] is Decision.RELEASE_BLOCK}
    assert release == {"PRICE_MINOR"}
