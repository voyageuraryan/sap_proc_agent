"""Loads data/erp/ into memory once, indexed for the way the agent queries it.

Design notes:

* The store is built from JSON on disk. It does NOT import `generator`. The
  generator is a build-time tool; coupling the service to it would ship the
  test-data code in the container and, worse, give the service a live import
  path toward the label sidecar.

* Read-only data needs no database. In-memory dicts loaded at startup are the
  right answer here -- a database would buy migrations and fixture resets for
  nothing. Step 5 introduces mutable approval state, and that is when the
  storage question gets asked properly.

* Loaded ONCE, at startup. Not per request: 652 records re-parsed per call is
  wasted work, and -- more importantly -- the data must not be able to change
  mid-run, or two identical agent calls in one eval could see different worlds.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from erp_domain.models import (
    GoodsReceipt,
    Invoice,
    Material,
    Plant,
    PurchaseOrder,
    Vendor,
)
from erp_domain.tolerances import ToleranceBook

REQUIRED_FILES = (
    "vendors.json",
    "materials.json",
    "plants.json",
    "purchase_orders.json",
    "goods_receipts.json",
    "invoices.json",
    "tolerances.json",
)


class ErpDataError(RuntimeError):
    """The data on disk is missing or inconsistent. Raised at startup, never later."""


@dataclass(frozen=True)
class ErpStore:
    """Every index the endpoints need, built once.

    A frozen dataclass rather than a Pydantic model on purpose. Pydantic belongs
    at boundaries, where untrusted input arrives. This is internal state
    assembled from objects that were validated one line earlier -- wrapping it
    would re-validate 652 objects for no benefit. `frozen` because nothing may
    mutate the store after startup.
    """

    vendors: dict[str, Vendor]
    materials: dict[str, Material]
    plants: dict[str, Plant]
    purchase_orders: dict[str, PurchaseOrder]
    # Grouped, because goods receipts are flat MSEG-style rows and the agent's
    # core question is "how much arrived against this PO line" -- a group-by
    # done once at startup beats a 188-row scan on every call.
    grs_by_po: dict[str, list[GoodsReceipt]]
    invoices: dict[str, Invoice]
    # Needed for vendor history, and load-bearing for DUP_INVOICE: the only way
    # to spot a duplicate is to see the vendor's other invoice references.
    invoices_by_vendor: dict[str, list[Invoice]]
    tolerances: ToleranceBook


def _read_json(path: Path) -> object:
    if not path.exists():
        raise ErpDataError(
            f"missing {path.name} at {path} -- run `uv run generator` first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _check_references(store: ErpStore) -> None:
    """Document-level referential integrity, checked at startup.

    Deliberately does NOT check po_item_number (EBELP). The AMBIGUOUS
    DANGLING_PO_LINE scenarios bill a line the PO does not have -- that is
    intentional test data, and the whole point of the variant. Document
    references are guaranteed; line references are not.
    """
    problems: list[str] = []

    for po in store.purchase_orders.values():
        if po.vendor_id not in store.vendors:
            problems.append(f"PO {po.po_number} references unknown vendor {po.vendor_id}")
        if po.plant_id not in store.plants:
            problems.append(f"PO {po.po_number} references unknown plant {po.plant_id}")
        for item in po.items:
            if item.material_id not in store.materials:
                problems.append(
                    f"PO {po.po_number}/{item.po_item_number} references "
                    f"unknown material {item.material_id}"
                )

    for grs in store.grs_by_po.values():
        for gr in grs:
            if gr.po_number not in store.purchase_orders:
                problems.append(f"GR {gr.gr_number} references unknown PO {gr.po_number}")

    for invoice in store.invoices.values():
        if invoice.vendor_id not in store.vendors:
            problems.append(
                f"invoice {invoice.invoice_number} references "
                f"unknown vendor {invoice.vendor_id}"
            )
        for item in invoice.items:
            if item.po_number not in store.purchase_orders:
                problems.append(
                    f"invoice {invoice.invoice_number}/{item.inv_item_number} "
                    f"references unknown PO {item.po_number}"
                )

    if problems:
        raise ErpDataError(
            f"{len(problems)} referential integrity problem(s):\n  "
            + "\n  ".join(problems[:10])
        )


def load_store(erp_data_dir: Path) -> ErpStore:
    """Read, validate and index the served ERP documents.

    Validation happens by construction -- every record goes through its Pydantic
    model, so malformed data fails here with a field-level message rather than
    500-ing on the third request.
    """
    vendors = {
        v.vendor_id: v
        for v in (Vendor(**r) for r in _read_json(erp_data_dir / "vendors.json"))
    }
    materials = {
        m.material_id: m
        for m in (Material(**r) for r in _read_json(erp_data_dir / "materials.json"))
    }
    plants = {
        p.plant_id: p
        for p in (Plant(**r) for r in _read_json(erp_data_dir / "plants.json"))
    }
    purchase_orders = {
        po.po_number: po
        for po in (
            PurchaseOrder(**r) for r in _read_json(erp_data_dir / "purchase_orders.json")
        )
    }

    grs_by_po: dict[str, list[GoodsReceipt]] = defaultdict(list)
    for row in _read_json(erp_data_dir / "goods_receipts.json"):
        gr = GoodsReceipt(**row)
        grs_by_po[gr.po_number].append(gr)

    invoices: dict[str, Invoice] = {}
    invoices_by_vendor: dict[str, list[Invoice]] = defaultdict(list)
    for row in _read_json(erp_data_dir / "invoices.json"):
        invoice = Invoice(**row)
        invoices[invoice.invoice_number] = invoice
        invoices_by_vendor[invoice.vendor_id].append(invoice)

    # Sort every list index. Otherwise response ordering follows file order, and
    # an eval that inspects the evidence trail becomes flaky for no reason.
    for rows in grs_by_po.values():
        rows.sort(key=lambda g: (g.gr_number, g.po_item_number))
    for docs in invoices_by_vendor.values():
        docs.sort(key=lambda i: i.invoice_number)

    tolerances = ToleranceBook(**_read_json(erp_data_dir / "tolerances.json"))

    store = ErpStore(
        vendors=vendors,
        materials=materials,
        plants=plants,
        purchase_orders=purchase_orders,
        # Plain dicts, not defaultdicts: a missing PO key must read as "no
        # receipts posted", and a defaultdict would silently invent an empty
        # list for a PO that does not exist at all. The endpoint needs to tell
        # those two apart -- one is GR_MISSING, the other is a 404.
        grs_by_po=dict(grs_by_po),
        invoices=invoices,
        invoices_by_vendor=dict(invoices_by_vendor),
        tolerances=tolerances,
    )
    _check_references(store)
    return store
