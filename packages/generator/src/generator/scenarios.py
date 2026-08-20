"""Builds labelled PO / goods-receipt / invoice triples.

v1 emits CLEAN scenarios only: every document agrees, nothing is blocked.
Step 3 adds the exception taxonomy by injecting one defect into this known-good
base, so the label always describes exactly one deliberate change.

Determinism rules for this module:
  * the only source of randomness is the `rng` passed in
  * every identifier derives from the scenario index, never from a draw
  * every date derives from cfg.epoch_date, never from the clock
"""

from datetime import timedelta
from decimal import Decimal

from erp_domain.models import (
    GoodsReceipt,
    Invoice,
    InvoiceItem,
    Material,
    Plant,
    PurchaseOrder,
    PurchaseOrderItem,
    Vendor,
)
from pydantic import BaseModel

from generator.config import GeneratorConfig
from generator.labels import AmbiguousVariant, ExceptionLabel
from generator.splits import Splits
from generator.tolerances import ToleranceBook

# Plausible order sizes per unit of measure. You buy gloves by the box in tens
# and laptops in ones -- without this the data reads as obviously fake.
QUANTITY_BANDS: dict[str, tuple[int, int]] = {
    "EA": (1, 25),
    "PR": (1, 20),
    "PAK": (2, 40),
    "BOX": (2, 30),
    "CS": (2, 40),
    "PAI": (1, 12),
    "RO": (2, 12),
    "L": (20, 200),
    "GAL": (5, 60),
}
DEFAULT_QUANTITY_BAND = (1, 20)

QTY_PRECISION = Decimal("0.001")  # SAP quantities carry 3 decimals
MONEY_PRECISION = Decimal("0.01")


class Scenario(BaseModel):
    """One test case: the documents plus the ground-truth label.

    `label` lives here, but nothing ever serialises a Scenario. cli.py writes
    po / goods_receipts / invoice into data/erp/ and the label into
    data/labels/, so there is no code path from the label to the served data.
    """

    scenario_id: str
    po: PurchaseOrder
    goods_receipts: list[GoodsReceipt]
    # A list because DUP_INVOICE puts two invoices in one scenario. Same
    # cardinality lesson as goods_receipts.
    invoices: list[Invoice]
    label: ExceptionLabel
    variant: AmbiguousVariant | None = None
    # Injected magnitudes, for the label sidecar. Strings so the JSON stays
    # byte-stable -- float formatting is not deterministic across platforms.
    detail: dict[str, str] = {}


class Dataset(BaseModel):
    """Everything one generator run produces."""

    vendors: list[Vendor]
    materials: list[Material]
    plants: list[Plant]
    scenarios: list[Scenario]
    tolerances: ToleranceBook
    splits: Splits


def _draw_quantity(rng, material: Material) -> Decimal:
    low, high = QUANTITY_BANDS.get(material.unit_of_measure, DEFAULT_QUANTITY_BAND)
    return Decimal(rng.randint(low, high)).quantize(QTY_PRECISION)


def _draw_price(rng, material: Material) -> Decimal:
    """Material valuation price, wobbled +/-5%.

    Integer arithmetic on Decimals only -- multiplying by a float would put
    binary rounding error back into money we are about to compare exactly.
    """
    wobble = Decimal(rng.randint(95, 105)) / Decimal(100)
    return (material.base_price * wobble).quantize(MONEY_PRECISION)


def build_scenario(
    index: int,
    rng,
    vendors: list[Vendor],
    materials: list[Material],
    plants: list[Plant],
    cfg: GeneratorConfig,
) -> Scenario:
    """Build one CLEAN triple.

    Document numbers derive from `index`, not from rng, so inserting a new
    random draw in Step 3 does not renumber every document in the dataset.
    SAP assigns them sequentially from number ranges anyway.
    """
    # 1-based: SAP number ranges do not start at zero, and "4500000000" reads
    # as a placeholder rather than a document.
    doc_seq = index + 1

    po_items: list[PurchaseOrderItem] = []
    for i in range(cfg.items):
        material = rng.choice(materials)
        po_items.append(
            PurchaseOrderItem(
                # EBELP is 5 digits, stepping by 10: 00010, 00020, ...
                po_item_number=f"{(i + 1) * 10:05d}",
                material_id=material.material_id,
                quantity=_draw_quantity(rng, material),
                net_price=_draw_price(rng, material),
            )
        )

    po = PurchaseOrder(
        po_number=f"45{doc_seq:08d}",
        vendor_id=rng.choice(vendors).vendor_id,
        plant_id=rng.choice(plants).plant_id,
        po_date=cfg.epoch_date + timedelta(days=rng.randint(0, 364)),
        currency="USD",
        items=po_items,
    )

    # PO date < GR date < invoice date. An invoice dated before its PO is the
    # fastest way for a reviewer to spot that nobody looked at the data.
    posting_date = po.po_date + timedelta(days=rng.randint(*cfg.lead_time_days))

    goods_receipts: list[GoodsReceipt] = []
    for item in po_items:
        goods_receipts.append(
            GoodsReceipt(
                gr_number=f"50{doc_seq:06d}01",
                po_number=po.po_number,
                po_item_number=item.po_item_number,
                # CLEAN: received exactly what was ordered.
                quantity=item.quantity,
                posting_date=posting_date,
            )
        )

    invoice_items: list[InvoiceItem] = []
    for i, item in enumerate(po_items):
        invoice_items.append(
            InvoiceItem(
                # BUZEI is 4 digits stepping by 1, and is NOT the PO item number.
                inv_item_number=f"{i + 1:04d}",
                po_number=po.po_number,
                po_item_number=item.po_item_number,
                # CLEAN: billed exactly what was received, at the PO price.
                quantity=item.quantity,
                unit_price=item.net_price,
            )
        )

    invoice = Invoice(
        invoice_number=f"51{doc_seq:06d}01",
        vendor_id=po.vendor_id,
        invoice_date=posting_date + timedelta(days=rng.randint(*cfg.invoice_lag_days)),
        vendor_ref=f"INV-{doc_seq:05d}",
        items=invoice_items,
        # None, not "None": a string is truthy, so every clean invoice would
        # read as blocked.
        block_reason=None,
    )

    return Scenario(
        # Obviously not an SAP identifier -- that is the point. The label file
        # is keyed on this, never on a document number.
        scenario_id=f"SC-{doc_seq:04d}",
        po=po,
        goods_receipts=goods_receipts,
        invoices=[invoice],
        # Every scenario is born CLEAN; the injector overwrites this.
        label=ExceptionLabel.CLEAN,
    )
