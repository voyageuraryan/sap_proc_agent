from enum import StrEnum


class ExceptionLabel(StrEnum):
    CLEAN = "CLEAN"
    PRICE_MINOR = "PRICE_MINOR"
    PRICE_MAJOR = "PRICE_MAJOR"
    QTY_OVER = "QTY_OVER"
    GR_MISSING = "GR_MISSING"
    GR_PARTIAL = "GR_PARTIAL"
    DUP_INVOICE = "DUP_INVOICE"
    AMBIGUOUS = "AMBIGUOUS"

class AmbiguousVariant(StrEnum):
    DANGLING_PO_LINE = "DANGLING_PO_LINE"
    UNAUTHORISED_OVER_DELIVERY = "UNAUTHORISED_OVER_DELIVERY"
    CONFLICTING_RECEIPTS = "CONFLICTING_RECEIPTS"

class BlockReason(StrEnum):
    """What the mock ERP records on a blocked invoice.

    Deliberately NOT the same strings as ExceptionLabel. SAP records that a
    check failed (a flattened view of RBKP-ZLSPR + the RSEG SPGR* indicators),
    not our ground-truth label. Identical strings would leak the taxonomy into
    served data and make the eval look rigged.
    """

    PRICE = "PRICE_VARIANCE"
    QUANTITY = "QUANTITY_VARIANCE"
    GR_MISSING = "MISSING_GR"
