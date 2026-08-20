"""One defect, injected into a known-good CLEAN scenario.
Every injector has the same shape: (scenario, rng, ctx) -> Scenario. It deep
copies first, mutates the copy, sets label/variant/detail, and returns it.
Why a registry of functions rather than branches inside build_scenario:
  * one code path builds valid documents, so a bug there is caught by 100% of
    scenarios instead of 12% of them
  * each injector is testable on its own
  * the dict key IS the label, so the two cannot drift apart
v1 has exactly one PO item, so these index [0] directly. Multi-line is
deliberately out of scope -- see decisions.md.
"""

from datetime import timedelta
from decimal import Decimal

from erp_domain.tolerances import ToleranceBook
from pydantic import BaseModel

from generator.labels import AmbiguousVariant, BlockReason, ExceptionLabel
from generator.scenarios import Scenario

MONEY = Decimal("0.01")
QTY = Decimal("0.001")


class InjectorContext(BaseModel):
    tolerances: ToleranceBook
    variant: AmbiguousVariant | None = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _price_variance(sc: Scenario, rng, ctx: InjectorContext, lo: int, hi: int, *, exceed: bool):
    """Raise the invoiced price by a fraction/multiple of the vendor's tolerance.
    Derived FROM the tolerance, never hardcoded. A flat +3% would be a minor
    variance for a 5% vendor and a major one for a 1% vendor -- the label would
    then disagree with the data and the eval set would be quietly poisoned.
    """
    tol = ctx.tolerances.for_vendor(sc.po.vendor_id).price_pct
    po_price = sc.po.items[0].net_price

    target_pct = tol * Decimal(rng.randint(lo, hi)) / Decimal(100)
    new_price = (po_price * (1 + target_pct / 100)).quantize(MONEY)

    # Rounding to two decimals moves the variance slightly. On a cheap line one
    # cent can be a large percentage, so recompute the EFFECTIVE variance and
    # nudge if it landed on the wrong side of the boundary.
    def effective(price: Decimal) -> Decimal:
        return ((price - po_price) / po_price * 100).quantize(MONEY)

    eff = effective(new_price)
    while exceed and eff <= tol:
        new_price += MONEY
        eff = effective(new_price)
    while not exceed and eff > tol:
        new_price -= MONEY
        eff = effective(new_price)

    sc.invoices[0].items[0].unit_price = new_price
    sc.invoices[0].block_reason = BlockReason.PRICE
    sc.detail = {
        "tolerance_pct": str(tol),
        "variance_pct": str(eff),
        "po_price": str(po_price),
        "invoiced_price": str(new_price),
    }


def _short_receipt(sc: Scenario, rng) -> Decimal:
    """Shrink the goods receipt below the ordered quantity. Returns what arrived.
    Shared by QTY_OVER and GR_PARTIAL on purpose: called with the same scenario
    index they consume the same draws, so the two labels produce identical POs
    and identical receipts, differing only in the invoiced quantity. That gives
    you a matched pair for the demo.
    """
    ordered = sc.po.items[0].quantity
    if ordered > 1:
        received = Decimal(rng.randint(1, int(ordered) - 1)).quantize(QTY)
    else:
        received = Decimal("0.500")  # rare: a single-unit line, part-delivered
    sc.goods_receipts[0].quantity = received
    return received


# --------------------------------------------------------------------------
# injectors
# --------------------------------------------------------------------------


def inject_clean(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    sc.label = ExceptionLabel.CLEAN
    sc.detail = {}
    return sc


def inject_price_minor(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    # 20-80% of tolerance: comfortably inside, no boundary arguments.
    _price_variance(sc, rng, ctx, 20, 80, exceed=False)
    sc.label = ExceptionLabel.PRICE_MINOR
    return sc


def inject_price_major(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    # 1.6x-5x tolerance. Starts at 1.6 rather than 1.01 because a variance one
    # hair over the line is a rounding dispute, and grading it would be unfair.
    _price_variance(sc, rng, ctx, 160, 500, exceed=True)
    sc.label = ExceptionLabel.PRICE_MAJOR
    return sc


def inject_qty_over(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    """Vendor billed the full PO quantity; only part of it arrived.
    The invoice quantity stays equal to the PO quantity, so invoice-vs-PO
    agrees perfectly. The only way to catch this is invoice-vs-receipts. If the
    invoice exceeded the PO too, an agent comparing against the PO would get it
    right by accident and this label would test nothing.
    """
    ordered = sc.po.items[0].quantity
    received = _short_receipt(sc, rng)
    sc.invoices[0].items[0].quantity = ordered
    sc.invoices[0].block_reason = BlockReason.QUANTITY
    sc.label = ExceptionLabel.QTY_OVER
    sc.detail = {
        "po_qty": str(ordered),
        "received_qty": str(received),
        "invoiced_qty": str(ordered),
        "over_by": str(ordered - received),
    }
    return sc


def inject_gr_missing(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    """No receipt at all. No resolution is available -- nobody knows if it came."""
    sc.goods_receipts = []
    sc.invoices[0].block_reason = BlockReason.GR_MISSING
    sc.label = ExceptionLabel.GR_MISSING
    sc.detail = {
        "po_qty": str(sc.po.items[0].quantity),
        "invoiced_qty": str(sc.invoices[0].items[0].quantity),
    }
    return sc


def inject_gr_partial(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    """A valid partial delivery. Nothing is wrong and nothing is blocked.
    The trap: GR sum < PO qty, so anything comparing the invoice to the PO sees
    a mismatch and raises a false positive.
    """
    ordered = sc.po.items[0].quantity
    received = _short_receipt(sc, rng)
    sc.invoices[0].items[0].quantity = received
    sc.invoices[0].block_reason = None
    sc.label = ExceptionLabel.GR_PARTIAL
    sc.detail = {
        "po_qty": str(ordered),
        "received_qty": str(received),
        "invoiced_qty": str(received),
    }
    return sc


def inject_dup_invoice(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    """Same vendor, same XBLNR, billed twice.
    The nastiest trap in the set: in isolation each invoice is perfect. The
    only signal is that the other one exists.
    """
    original = sc.invoices[0]
    duplicate = original.model_copy(deep=True)
    duplicate.invoice_number = original.invoice_number[:-2] + "02"
    duplicate.invoice_date = original.invoice_date + timedelta(days=rng.randint(1, 20))
    duplicate.block_reason = None
    sc.invoices.append(duplicate)
    sc.label = ExceptionLabel.DUP_INVOICE
    sc.detail = {
        "duplicate_of": original.invoice_number,
        "duplicate": duplicate.invoice_number,
        "vendor_ref": original.vendor_ref,
    }
    return sc


def inject_ambiguous(sc: Scenario, rng, ctx: InjectorContext) -> Scenario:
    """Evidence is underdetermined. Correct behaviour is to escalate, not resolve.
    Graded on "did it admit it couldn't tell", never on naming the variant.
    """
    sc.label = ExceptionLabel.AMBIGUOUS
    sc.variant = ctx.variant

    if ctx.variant is AmbiguousVariant.DANGLING_PO_LINE:
        # Bills a line the PO does not have. Typo? Wrong PO? Wrong line?
        sc.invoices[0].items[0].po_item_number = "00030"
        sc.invoices[0].block_reason = None
        sc.detail = {
            "referenced_item": "00030",
            "po_items": ",".join(i.po_item_number for i in sc.po.items),
        }

    elif ctx.variant is AmbiguousVariant.UNAUTHORISED_OVER_DELIVERY:
        # More arrived than was ordered. Authorised off-system, or an error?
        ordered = sc.po.items[0].quantity
        extra = Decimal(rng.randint(1, max(1, int(ordered * Decimal("0.3"))))).quantize(QTY)
        received = ordered + extra
        sc.goods_receipts[0].quantity = received
        sc.invoices[0].items[0].quantity = received
        sc.invoices[0].block_reason = BlockReason.QUANTITY
        sc.detail = {
            "po_qty": str(ordered),
            "received_qty": str(received),
            "invoiced_qty": str(received),
        }

    else:  # CONFLICTING_RECEIPTS
        # Two receipts of the full quantity. Received twice, or posted twice?
        first = sc.goods_receipts[0]
        second = first.model_copy(deep=True)
        second.gr_number = first.gr_number[:-2] + "02"
        second.posting_date = first.posting_date + timedelta(days=rng.randint(1, 7))
        sc.goods_receipts.append(second)
        sc.invoices[0].block_reason = None
        sc.detail = {
            "po_qty": str(sc.po.items[0].quantity),
            "gr_quantities": ",".join(str(g.quantity) for g in sc.goods_receipts),
            "invoiced_qty": str(sc.invoices[0].items[0].quantity),
        }

    return sc


INJECTORS = {
    ExceptionLabel.CLEAN: inject_clean,
    ExceptionLabel.PRICE_MINOR: inject_price_minor,
    ExceptionLabel.PRICE_MAJOR: inject_price_major,
    ExceptionLabel.QTY_OVER: inject_qty_over,
    ExceptionLabel.GR_MISSING: inject_gr_missing,
    ExceptionLabel.GR_PARTIAL: inject_gr_partial,
    ExceptionLabel.DUP_INVOICE: inject_dup_invoice,
    ExceptionLabel.AMBIGUOUS: inject_ambiguous,
}


def apply_injection(
    scenario: Scenario, label: ExceptionLabel, rng, ctx: InjectorContext
) -> Scenario:
    """Deep copy, then hand off to the injector for the planned label.
    The copy happens here rather than in each injector so it cannot be
    forgotten -- without it, mutating a nested list corrupts the base and you
    get "some of my CLEAN scenarios aren't clean".
    """
    return INJECTORS[label](scenario.model_copy(deep=True), rng, ctx)
