"""The view layer: what a reviewer is shown, as pure functions.

These matter more than they look. An approval queue that only says "the agent
wants to change something" trains people to click Approve, which converts a
human gate into a slower rubber stamp. The tests below are about whether the
reviewer can see enough to DISAGREE.
"""

import pytest
from review_ui.views import (
    ACTIONS_FOR_STATUS,
    DIFF_SPEC,
    HEADER_SPEC,
    STATUS_ORDER,
    build_diff,
    build_view,
    evidence_rows,
    queue_counts,
)

INVOICE = {
    "BELNR": "5100000901",
    "LIFNR": "1000000010",
    "XBLNR": "INV-00009",
    "BLDAT": "2025-03-25",
    "block_reason": "QUANTITY_VARIANCE",
    "items": [
        {
            "BUZEI": "0001",
            "EBELN": "4500000009",
            "EBELP": "00010",
            "MENGE": "14.000",
            "NETPR": "41.90",
        }
    ],
}
PO = {
    "EBELN": "4500000009",
    "items": [{"EBELP": "00010", "MENGE": "14.000", "NETPR": "41.90"}],
    "ToleranceConfig": {
        "PriceVariancePct": "10.0",
        "QuantityVariancePct": "5.0",
        "Source": "VENDOR_SPECIFIC",
    },
}
RECEIPTS = [{"MBLNR": "5000000901", "EBELP": "00010", "MENGE": "13.000", "BUDAT": "2025-03-23"}]

QUANTITY = {
    "correction_type": "AMEND_INVOICE_QUANTITY",
    "invoice_number": "5100000901",
    "inv_item_number": "0001",
    "from_quantity": "14.000",
    "to_quantity": "13.000",
}


def test_every_correction_type_knows_how_to_show_itself():
    """A new correction type must not be addable without deciding how a human
    is meant to check it."""
    from agent.schemas import CorrectionType

    covered = set(DIFF_SPEC) | set(HEADER_SPEC)
    assert {c.value for c in CorrectionType} == covered


def test_a_quantity_amendment_reads_as_before_and_after():
    rows = build_diff(QUANTITY, INVOICE)
    assert len(rows) == 1
    assert (rows[0].label, rows[0].before, rows[0].after) == (
        "Invoiced quantity",
        "14.000",
        "13.000",
    )
    assert not rows[0].stale


def test_a_price_amendment_reads_the_price_field():
    payload = {
        "correction_type": "AMEND_INVOICE_PRICE",
        "invoice_number": "5100000901",
        "inv_item_number": "0001",
        "from_price": "46.10",
        "to_price": "41.90",
    }
    rows = build_diff(payload, INVOICE)
    assert rows[0].label == "Unit price"
    # The invoice says 41.90, the payload claims it was 46.10 -> stale.
    assert rows[0].stale


def test_staleness_is_detected_when_the_document_has_moved():
    """Approving a correction computed against a document that has since
    changed would be refused by the ERP anyway -- but a reviewer should be
    told BEFORE clicking, not after."""
    moved = {**INVOICE, "items": [{**INVOICE["items"][0], "MENGE": "13.000"}]}
    assert build_diff(QUANTITY, moved)[0].stale


def test_formatting_differences_are_not_staleness():
    """13.0 and 13.000 are the same quantity. A warning that cries wolf is
    worse than no warning."""
    same = {**INVOICE, "items": [{**INVOICE["items"][0], "MENGE": "14.0"}]}
    assert not build_diff(QUANTITY, same)[0].stale


def test_a_release_block_shows_the_block_going_away():
    payload = {
        "correction_type": "RELEASE_INVOICE_BLOCK",
        "invoice_number": "5100000901",
        "released_block_reason": "QUANTITY_VARIANCE",
    }
    rows = build_diff(payload, INVOICE)
    assert (rows[0].before, rows[0].after) == ("QUANTITY_VARIANCE", "(none)")
    assert not rows[0].stale


def test_releasing_a_block_that_is_no_longer_there_is_stale():
    payload = {
        "correction_type": "RELEASE_INVOICE_BLOCK",
        "invoice_number": "5100000901",
        "released_block_reason": "PRICE_VARIANCE",
    }
    assert build_diff(payload, INVOICE)[0].stale


def test_a_rejection_names_the_original():
    payload = {
        "correction_type": "REJECT_INVOICE",
        "invoice_number": "5100000602",
        "duplicate_of": "5100000601",
    }
    rows = build_diff(payload, None)
    assert rows[0].after == "5100000601"


def test_an_unknown_correction_type_still_renders_its_fields():
    """Showing the raw payload is honest; showing nothing would hide a change
    from the person whose job is to see it."""
    rows = build_diff({"correction_type": "SOMETHING_NEW", "amount": "10"}, None)
    assert any(row.after == "10" for row in rows)


def test_a_missing_invoice_is_flagged_rather_than_silently_unverified():
    view = build_view({"status": "PROPOSED", "payload": QUANTITY}, None)
    assert any("could not be read" in w for w in view.warnings)


def test_a_stale_proposal_carries_a_warning_and_a_flag():
    moved = {**INVOICE, "items": [{**INVOICE["items"][0], "MENGE": "13.000"}]}
    view = build_view({"status": "PROPOSED", "payload": QUANTITY}, moved)
    assert view.is_stale
    assert any("no longer holds" in w for w in view.warnings)


@pytest.mark.parametrize(("status", "expected"), list(ACTIONS_FOR_STATUS.items()))
def test_buttons_are_rendered_from_the_state_machine(status, expected):
    """A button the server will refuse is worse than no button."""
    view = build_view({"status": status, "payload": QUANTITY}, INVOICE)
    assert view.actions == expected


def test_the_terminal_states_offer_nothing():
    assert ACTIONS_FOR_STATUS["APPLIED"] == ()
    assert ACTIONS_FOR_STATUS["REJECTED"] == ()


def test_the_action_table_matches_the_erps_allowed_transitions():
    """The screen and the state machine cannot be allowed to drift apart."""
    from mock_erp.proposals import ALLOWED_TRANSITIONS

    for (status, action), _ in ALLOWED_TRANSITIONS.items():
        assert action.value.lower() in ACTIONS_FOR_STATUS[status.value], (status, action)
    offered = {(s, a) for s, actions in ACTIONS_FOR_STATUS.items() for a in actions}
    allowed = {(s.value, a.value.lower()) for s, a in ALLOWED_TRANSITIONS}
    assert offered == allowed


def test_queue_counts_are_stable_and_complete():
    counts = queue_counts([{"status": "PROPOSED"}, {"status": "APPLIED"}, {"status": "PROPOSED"}])
    assert list(counts) == list(STATUS_ORDER)
    assert counts["PROPOSED"] == 2
    assert counts["REJECTED"] == 0


def test_the_evidence_panel_reconstructs_the_three_way_match():
    rows = evidence_rows(INVOICE, PO, RECEIPTS, "0001")
    assert rows["ordered_qty"] == "14.000"
    assert rows["received_qty"] == "13.000"
    assert rows["invoiced_qty"] == "14.000"
    assert rows["quantity_tolerance"] == "5.0"
    assert rows["tolerance_source"] == "VENDOR_SPECIFIC"
    assert rows["supplier"] == "1000000010"
    assert rows["block_reason"] == "QUANTITY_VARIANCE"


def test_the_evidence_panel_sums_multiple_receipts():
    two = [
        *RECEIPTS,
        {"MBLNR": "5000000902", "EBELP": "00010", "MENGE": "1.000", "BUDAT": "2025-03-24"},
    ]
    assert evidence_rows(INVOICE, PO, two, "0001")["received_qty"] == "14.000"


def test_no_receipts_reads_as_nothing_received_not_as_zero():
    """'nothing was received' and 'zero was received' are the same number and
    very different facts."""
    assert evidence_rows(INVOICE, PO, [], "0001")["received_qty"] is None


def test_the_evidence_panel_survives_missing_documents():
    rows = evidence_rows(None, None, None, "0001")
    assert rows["ordered_qty"] is None
    assert rows["received_qty"] is None
