from erp_domain.models import GoodsReceipt, Invoice, PurchaseOrder
from erp_domain.tolerances import ToleranceBook
from fastapi import APIRouter, Depends, Request

from mock_erp.odata import (
    ODataError,
    collection,
    entity,
    parse_po_filter,
    reject_unsupported_options,
)
from mock_erp.overlay import apply_amendments
from mock_erp.repository import ProposalRepository
from mock_erp.store import ErpStore

router = APIRouter(prefix="/sap/opu/odata/sap/ZPROC_SRV", tags=["odata"])


def get_store(request: Request) -> ErpStore:
    return request.app.state.store


def get_repository(request: Request) -> ProposalRepository:
    return request.app.state.repository


def _effective_invoice(invoice: Invoice, repo: ProposalRepository) -> Invoice:
    """The document as it stands now: committed base + applied amendments.

    Reads go through this so an APPROVED-then-APPLIED correction is
    observable on the same URL that served the original. That observability
    is what makes "no write happened without approval" a testable claim
    rather than a promise -- there has to be a write to look for.

    Amendments are returned ordered by applied_at, which matters when two
    corrections touch the same line.
    """
    amendments = repo.amendments_for_invoice(invoice.invoice_number)
    if not amendments:
        return invoice
    return apply_amendments(invoice, [a.payload_json for a in amendments])


def _tolerance_block(tolerances: ToleranceBook, vendor_id: str) -> dict:
    if vendor_id in tolerances.by_vendor:
        tolerance = tolerances.by_vendor[vendor_id]
        source = "VENDOR_SPECIFIC"
    else:
        tolerance = tolerances.default
        source = "DEFAULT"

    return {
        "PriceVariancePct": tolerance.price_pct,
        "QuantityVariancePct": tolerance.quantity_pct,
        "Source": source,
    }


def _po_payload(po: PurchaseOrder, tolerances: ToleranceBook) -> dict:
    payload = po.model_dump(by_alias=True, mode="json")
    payload["ToleranceConfig"] = _tolerance_block(tolerances, po.vendor_id)
    return payload


def _gr_payload(rows: list[GoodsReceipt]) -> list[dict]:
    payload = []
    for gr in rows:
        payload.append(gr.model_dump(by_alias=True, mode="json"))
    return payload


def _invoice_payload(invoice: Invoice) -> dict:
    return invoice.model_dump(by_alias=True, mode="json")


def _vendor_history_payload(store: ErpStore, vendor_id: str) -> dict:
    hist = {}
    hist["Supplier"] = vendor_id
    hist["SupplierName"] = store.vendors[vendor_id].name
    hist["TotalInvoices"] = len(store.invoices_by_vendor[vendor_id])
    hist["BlockedInvoices"] = sum(
        inv.block_reason is not None for inv in store.invoices_by_vendor[vendor_id]
    )
    hist["BlockRate"] = (
        f"{hist['BlockedInvoices'] / hist['TotalInvoices']:.2f}"
        if hist["TotalInvoices"]
        else "0.00"
    )
    res = []
    for inv in store.invoices_by_vendor[vendor_id]:
        grs = store.grs_by_po.get(inv.items[0].po_number, [])
        if not grs:
            continue
        res.append(
            (inv.invoice_date - ((max(grs, key=lambda gr: gr.posting_date)).posting_date)).days
        )
    hist["AvgDaysGoodsReceiptToInvoice"] = str(sum(res) / len(res)) if res else None
    hist["ApplicableTolerance"] = _tolerance_block(store.tolerances, vendor_id)
    res = []
    for inv in store.invoices_by_vendor[vendor_id]:
        res.append(
            {
                "BELNR": inv.invoice_number,
                "XBLNR": inv.vendor_ref,
                "BLDAT": inv.invoice_date.isoformat(),
            }
        )
    hist["InvoiceReferences"] = sorted(res, key=lambda x: x["BELNR"])
    return hist


@router.get("/A_PurchaseOrder('{po_number}')")
async def get_purchase_order(po_number: str, store: ErpStore = Depends(get_store)) -> dict:
    if po_number not in store.purchase_orders:
        raise ODataError(
            code="PO_NOT_FOUND",
            message=f"The Purchase ORder for {po_number} is not found",
            status=404,
        )

    return entity(_po_payload(store.purchase_orders[po_number], store.tolerances))


@router.get("/A_MaterialDocumentItem")
async def get_goods_receipt_items(request: Request, store: ErpStore = Depends(get_store)) -> dict:
    reject_unsupported_options(request.query_params, {"$filter", "$format"})
    po_number = parse_po_filter(request.query_params.get("$filter"))
    if po_number not in store.purchase_orders:
        raise ODataError(
            code="PO_NOT_FOUND",
            message=f"The Purchase ORder for {po_number} is not found",
            status=404,
        )
    rows = store.grs_by_po.get(po_number, [])
    return collection(_gr_payload(rows))


@router.get("/A_SupplierInvoice('{invoice_number}')")
async def get_supplier_invoice(
    invoice_number: str,
    store=Depends(get_store),
    repo: ProposalRepository = Depends(get_repository),
) -> dict:
    if invoice_number not in store.invoices:
        raise ODataError(
            code="INVOICE_NOT_FOUND",
            message=f"For invoice number {invoice_number} the invoice is not available",
            status=404,
        )
    invoice = _effective_invoice(store.invoices[invoice_number], repo)
    return entity(_invoice_payload(invoice))


@router.get("/A_SupplierInvoice")
async def list_supplier_invoices(
    request: Request,
    store=Depends(get_store),
    repo: ProposalRepository = Depends(get_repository),
) -> dict:
    rows = (_invoice_payload(_effective_invoice(inv, repo)) for inv in store.invoices.values())
    return collection(sorted(rows, key=lambda x: x["BELNR"]))


@router.get("/VendorHistory")
async def get_vendor_history(request: Request, store=Depends(get_store)) -> dict:
    vendor_param = request.query_params.get("VendorID")
    # vendor_id = re.search(r"^\d{10}", vendor_param)
    vendor_id = vendor_param.strip("'")
    if vendor_id not in store.vendors:
        raise ODataError(
            code="VENDOR_NOT_FOUND", message=f"Vendor with ID {vendor_id} i snot found", status=404
        )
    return entity(_vendor_history_payload(store, vendor_id))
