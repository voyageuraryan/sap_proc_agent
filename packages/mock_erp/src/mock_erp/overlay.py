"""Effective invoice state = the committed base document + applied amendments.

Why this is its own module and not a method on the repository or a helper in
api.py: BOTH need it, and for different reasons.

  * api.py       needs it to SERVE the current state of an invoice
  * repository   needs it to compute the CURRENT value before checking whether
                 a proposal has gone stale

Two copies of this rule would eventually disagree, and when they did the read
endpoint would show one number while the staleness check believed another --
the kind of bug where both halves look correct in isolation.

It imports only erp_domain.models and mock_erp.proposals, so neither api.py
nor repository.py creates an import cycle by using it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from erp_domain.models import Invoice, InvoiceItem

from mock_erp.proposals import CorrectionPayload, CorrectionType

# What block_reason reads once an invoice is rejected as a duplicate.
REJECTED_BLOCK_REASON = "REJECTED_DUPLICATE"


def _find_item(invoice: Invoice, inv_item_number: str) -> InvoiceItem | None:
    """Address a line by BUZEI.

    The payload already carries inv_item_number, so the overlay never has to
    guess which line it means -- exactly as an invoice item addresses a PO line
    through po_item_number, one level down.
    """
    for item in invoice.items:
        if item.inv_item_number == inv_item_number:
            return item
    return None


def _to_decimal(raw: str) -> Decimal | None:
    """Payload amounts are strings; comparisons and writes need Decimal."""
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return None


def apply_amendments(
    invoice: Invoice, payloads: Sequence[CorrectionPayload]
) -> Invoice:
    """Return a NEW Invoice with every payload applied, in the order given.

    Callers must pass payloads already ordered by applied_at -- order matters
    when two amendments touch the same line.

    THE DEEP COPY IS A CORRECTNESS REQUIREMENT, not tidiness. ErpStore is a
    frozen dataclass, but `frozen` only stops rebinding its fields; it does
    nothing to stop mutation of the Pydantic objects inside. Writing to
    invoice.items[0].quantity would corrupt the shared in-memory store for
    every subsequent request, survive deleting approvals.sqlite3, and vanish on
    restart -- an order-dependent bug that looks like the gate working.
    """
    working = invoice.model_copy(deep=True)

    for payload in payloads:
        kind = payload.correction_type

        if kind == CorrectionType.AMEND_INVOICE_QUANTITY:
            item = _find_item(working, payload.inv_item_number)
            if item is None:
                # Ignore rather than raise: this is the READ path, and a stored
                # amendment pointing at a line that no longer exists must not
                # make the invoice unservable. apply() is where a bad reference
                # gets rejected, before it can ever be stored.
                continue
            value = _to_decimal(payload.to_quantity)
            if value is not None:
                item.quantity = value

        elif kind == CorrectionType.AMEND_INVOICE_PRICE:
            item = _find_item(working, payload.inv_item_number)
            if item is None:
                continue
            value = _to_decimal(payload.to_price)
            if value is not None:
                item.unit_price = value

        elif kind == CorrectionType.RELEASE_INVOICE_BLOCK:
            # Header-level: no item addressing. The block is cleared entirely.
            working.block_reason = None

        elif kind == CorrectionType.REJECT_INVOICE:
            # Header-level. Not payable; the duplicate reference lives in the
            # proposal's payload, not on the served document.
            working.block_reason = REJECTED_BLOCK_REASON

    return working


def find_stale_conflict(
    effective_invoice: Invoice, payload: CorrectionPayload
) -> tuple[str, str] | None:
    """Is this payload still valid against the CURRENT state of the invoice?

    Returns None when the payload is applicable, otherwise (code, message) for
    the caller to raise. Returning rather than raising keeps this module free of
    any dependency on repository.py's exception type -- which is what avoids an
    import cycle, since repository.py imports this module.

    Pass the EFFECTIVE invoice (base + already-applied amendments), never the
    base. Two proposals against the same line, each written when the line read
    20.000:

        PR-000001  from 20.000 -> to 18.000   applied
        PR-000002  from 20.000 -> to 19.000   approved, applying now

    Checked against the base, PR-000002 sees 20.000, matches, and applies --
    silently undoing a separately approved human decision. Checked against the
    effective value it sees 18.000, does not match, and is refused. Correct:
    that proposal was written against facts that have since changed.

    Comparison is by Decimal VALUE, not by string. "20.0" and "20.000" are the
    same quantity and must not be a false stale. Note the deliberate contrast
    with payload_hash, which compares exact BYTES so that reformatting cannot
    evade the integrity check -- two comparisons, two different purposes. Do not
    "tidy" either one into the other.
    """
    kind = payload.correction_type

    if kind == CorrectionType.REJECT_INVOICE:
        # Nothing to compare: rejecting a duplicate asserts nothing about the
        # document's current values.
        return None

    if kind == CorrectionType.RELEASE_INVOICE_BLOCK:
        if effective_invoice.block_reason != payload.released_block_reason:
            return (
                "STALE_PROPOSAL",
                f"invoice {payload.invoice_number} block reason is now "
                f"{effective_invoice.block_reason!r}, proposal was written "
                f"against {payload.released_block_reason!r}",
            )
        return None

    item = _find_item(effective_invoice, payload.inv_item_number)
    if item is None:
        # A malformed request, not a stale one -- different code, different
        # status. A client can act on the difference.
        return (
            "UNKNOWN_INVOICE_ITEM",
            f"invoice {payload.invoice_number} has no item "
            f"{payload.inv_item_number}",
        )

    if kind == CorrectionType.AMEND_INVOICE_QUANTITY:
        expected, current, field = payload.from_quantity, item.quantity, "quantity"
    else:
        expected, current, field = payload.from_price, item.unit_price, "unit price"

    expected_value = _to_decimal(expected)
    if expected_value is None:
        return (
            "UNKNOWN_INVOICE_ITEM",
            f"payload from_{field.replace(' ', '_')} {expected!r} is not a number",
        )

    if expected_value != current:
        return (
            "STALE_PROPOSAL",
            f"invoice {payload.invoice_number} item "
            f"{payload.inv_item_number} {field} is now {current}, proposal was "
            f"written against {expected_value}",
        )

    return None
