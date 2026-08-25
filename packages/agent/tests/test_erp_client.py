"""The ErpClient against the real service.

These are integration tests, not unit tests: they exercise routing, the OData
envelope, and error translation end to end. The only thing missing is the
socket.
"""

import httpx
import pytest
from agent.erp_client import ErpClient, ErpError
from conftest import INVOICE, PO, VENDOR


def test_get_invoice_unwraps_the_d_envelope(erp_client):
    invoice = erp_client.get_invoice(INVOICE)
    # No "d" key: the dialect stops at the client boundary.
    assert "d" not in invoice
    assert invoice["BELNR"] == INVOICE
    assert invoice["block_reason"] == "QUANTITY_VARIANCE"
    assert invoice["items"][0]["EBELN"] == PO


def test_purchase_order_carries_the_vendor_tolerance(erp_client):
    po = erp_client.get_purchase_order(PO)
    tolerance = po["ToleranceConfig"]
    assert tolerance["Source"] == "VENDOR_SPECIFIC"
    # Strings, not floats: money and tolerances stay Decimal-shaped on the wire.
    assert tolerance["PriceVariancePct"] == "10.0"
    assert tolerance["QuantityVariancePct"] == "5.0"


def test_goods_receipts_returns_a_flat_list(erp_client):
    rows = erp_client.get_goods_receipts(PO)
    assert isinstance(rows, list)
    assert [r["MENGE"] for r in rows] == ["13.000"]


def test_vendor_history_aggregates(erp_client):
    history = erp_client.get_vendor_history(VENDOR)
    assert history["Supplier"] == VENDOR
    assert history["TotalInvoices"] > 0
    assert any(ref["BELNR"] == INVOICE for ref in history["InvoiceReferences"])


def test_missing_document_raises_a_typed_error(erp_client):
    with pytest.raises(ErpError) as excinfo:
        erp_client.get_invoice("5199999999")
    error = excinfo.value
    assert error.code == "INVOICE_NOT_FOUND"
    assert error.status == 404
    # The message is what the model will read, so it has to be a sentence.
    assert "5199999999" in error.message


def test_a_non_odata_error_body_still_becomes_an_erp_error(erp_client):
    """FastAPI's own 404 is {"detail": ...}, not the OData envelope.

    Indexing ["error"] blindly would raise KeyError -- the wrong exception
    type, which the tool layer does not catch, which kills the run.
    """
    with pytest.raises(ErpError) as excinfo:
        erp_client._get("/NoSuchEntitySet")
    assert excinfo.value.code == "HTTP_ERROR"
    assert excinfo.value.status == 404


def test_html_error_body_still_becomes_an_erp_error():
    """A crashing gateway returns HTML. json() raises ValueError; we must not."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, html="<html><body>Bad Gateway</body></html>")

    transport = httpx.MockTransport(handler)
    client = ErpClient(
        "http://erp", client=httpx.Client(transport=transport, base_url="http://erp")
    )
    with pytest.raises(ErpError) as excinfo:
        client.get_invoice(INVOICE)
    assert excinfo.value.code == "HTTP_ERROR"
    assert excinfo.value.status == 502


def test_connection_failure_is_distinguished_from_an_http_error():
    """The fix for ERP_UNREACHABLE is 'start the service', so it needs its own code."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    client = ErpClient(
        "http://erp", client=httpx.Client(transport=transport, base_url="http://erp")
    )
    with pytest.raises(ErpError) as excinfo:
        client.get_invoice(INVOICE)
    assert excinfo.value.code == "ERP_UNREACHABLE"
    assert excinfo.value.status == 503


def test_a_response_without_the_d_envelope_is_rejected():
    """Silently returning a bare body would let a dialect change pass unnoticed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"BELNR": "5100000901"})

    transport = httpx.MockTransport(handler)
    client = ErpClient(
        "http://erp", client=httpx.Client(transport=transport, base_url="http://erp")
    )
    with pytest.raises(ErpError) as excinfo:
        client.get_invoice(INVOICE)
    assert excinfo.value.code == "MALFORMED_RESPONSE"


def test_close_does_not_close_an_injected_client(erp_app):
    """The caller owns what the caller opened."""
    client = ErpClient(base_url=str(erp_app.base_url), client=erp_app)
    client.close()
    # Still usable: closing the ErpClient did not close the TestClient.
    assert client.get_invoice(INVOICE)["BELNR"] == INVOICE
