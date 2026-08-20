"""Procurement domain models, shared by the generator and the mock ERP service.

Field names are readable Python; JSON serialisation uses SAP MM field names via
`serialization_alias` (EBELN, LIFNR, MENGE, ...). Dump with `by_alias=True` to
get the SAP-shaped payload.

Structure follows SAP MM: EKKO/EKPO (purchase order header/items),
MKPF/MSEG (material document = goods receipt), RBKP/RSEG (invoice header/items).
Deliberate v1 simplifications are recorded in decisions.md.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# ---------------------------------------------------------------------------
# SAP-typed field aliases
#
# SAP zero-pads its keys, so these are strings with fixed widths -- never ints.
# An int would strip leading zeros and skip the pattern check entirely.
# ---------------------------------------------------------------------------

PONumber = Annotated[
    str, StringConstraints(pattern=r"^45\d{8}$"), Field(serialization_alias="EBELN")
]
POItemNumber = Annotated[
    str, StringConstraints(pattern=r"^\d{5}$"), Field(serialization_alias="EBELP")
]
GRNumber = Annotated[
    str, StringConstraints(pattern=r"^50\d{8}$"), Field(serialization_alias="MBLNR")
]
InvoiceNumber = Annotated[
    str, StringConstraints(pattern=r"^51\d{8}$"), Field(serialization_alias="BELNR")
]
InvoiceItemNumber = Annotated[
    str, StringConstraints(pattern=r"^\d{4}$"), Field(serialization_alias="BUZEI")
]
VendorID = Annotated[
    str, StringConstraints(pattern=r"^\d{10}$"), Field(serialization_alias="LIFNR")
]
MaterialID = Annotated[
    str, StringConstraints(pattern=r"^\d{18}$"), Field(serialization_alias="MATNR")
]
PlantID = Annotated[
    str, StringConstraints(pattern=r"^\d{4}$"), Field(serialization_alias="WERKS")
]
Currency = Annotated[
    str, StringConstraints(pattern=r"^[A-Z]{3}$"), Field(serialization_alias="WAERS")
]
VendorRef = Annotated[str, Field(serialization_alias="XBLNR")]

# Decimal, never float: this system exists to decide whether two amounts match,
# so binary rounding drift is not acceptable. Pydantic serialises Decimal to a
# JSON string, which is also what SAP OData V2 does with Edm.Decimal.
Quantity = Annotated[Decimal, Field(serialization_alias="MENGE")]
NetPrice = Annotated[Decimal, Field(serialization_alias="NETPR")]
BasePrice = Annotated[Decimal, Field(serialization_alias="STPRS")]

PODate = Annotated[date, Field(serialization_alias="AEDAT")]
PostingDate = Annotated[date, Field(serialization_alias="BUDAT")]
InvoiceDate = Annotated[date, Field(serialization_alias="BLDAT")]


# ---------------------------------------------------------------------------
# Master data -- fixed reference data, referenced by ID from every transaction
# ---------------------------------------------------------------------------


class Vendor(BaseModel):
    vendor_id: VendorID
    name: str
    country: str


class Material(BaseModel):
    material_id: MaterialID
    description: str
    unit_of_measure: str
    # SAP's material master carries a valuation price (MBEW-STPRS). PO prices
    # are derived from it, so a laptop cannot be priced like a box of gloves.
    base_price: BasePrice


class Plant(BaseModel):
    plant_id: PlantID
    name: str


# ---------------------------------------------------------------------------
# Purchase order -- EKKO header + EKPO items
# ---------------------------------------------------------------------------


class PurchaseOrderItem(BaseModel):
    po_item_number: POItemNumber
    material_id: MaterialID
    quantity: Quantity
    net_price: NetPrice


class PurchaseOrder(BaseModel):
    po_number: PONumber
    vendor_id: VendorID
    plant_id: PlantID
    po_date: PODate
    # Currency is header-level in SAP (EKKO-WAERS), not per item.
    currency: Currency = "USD"
    # v1 always emits exactly one item, but the array shape matches OData's
    # PurchaseOrder -> PurchaseOrderItems and makes multi-line a generator
    # change rather than a schema migration. See decisions.md.
    items: list[PurchaseOrderItem]


# ---------------------------------------------------------------------------
# Goods receipt -- a flat MSEG item row, carrying its document keys.
#
# Deliberately NOT header+items: this mirrors SAP's own OData shape (the
# material-document item entity carries the header keys on every row) and
# matches the query the agent actually runs -- "sum what was received against
# this PO line". See decisions.md.
# ---------------------------------------------------------------------------


class GoodsReceipt(BaseModel):
    gr_number: GRNumber
    po_number: PONumber
    po_item_number: POItemNumber
    quantity: Quantity
    posting_date: PostingDate


# ---------------------------------------------------------------------------
# Invoice -- RBKP header + RSEG items
# ---------------------------------------------------------------------------


class InvoiceItem(BaseModel):
    # BUZEI is this line's position within the invoice. EBELP is the PO line it
    # bills. They are unrelated -- the vendor lays out their invoice however
    # they like, and may bill lines out of order or across several POs.
    inv_item_number: InvoiceItemNumber
    po_number: PONumber
    po_item_number: POItemNumber
    quantity: Quantity
    # Real RSEG carries WRBTR (line amount), not a unit price. Modelled as a
    # unit price so the comparison against EKPO-NETPR is field-to-field.
    unit_price: NetPrice


class Invoice(BaseModel):
    invoice_number: InvoiceNumber
    vendor_id: VendorID
    invoice_date: InvoiceDate
    # The vendor's own document number. Duplicate detection is vendor + this.
    vendor_ref: VendorRef
    items: list[InvoiceItem]
    # Flattened representation of RBKP-ZLSPR (payment block key) plus the RSEG
    # SPGR* blocking indicators. Deliberately unaliased -- it is neither field.
    # None means the three-way match passed and nothing is blocked.
    block_reason: str | None = None
