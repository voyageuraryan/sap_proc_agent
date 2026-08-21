"""HTTP-level tests for the mock ERP OData service.

These are the contract the agent will code against in Step 6, so they are worth
more than their line count suggests: every one of them pins a behaviour that,
if wrong, would look like a model failure later rather than a service bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mock_erp.app import create_app
from mock_erp.settings import Settings

BASE = "/sap/opu/odata/sap/ZPROC_SRV"

# Ground truth vocabulary. None of it may appear in a served response.
LABEL_VALUES = (
    "CLEAN", "PRICE_MINOR", "PRICE_MAJOR", "QTY_OVER",
    "GR_MISSING", "GR_PARTIAL", "DUP_INVOICE", "AMBIGUOUS",
    "DANGLING_PO_LINE", "UNAUTHORISED_OVER_DELIVERY", "CONFLICTING_RECEIPTS",
)
# "detail" is deliberately NOT in this list: FastAPI's own error bodies use
# {"detail": "Not Found"}, so asserting on it would false-positive on every
# legitimate 404. label/variant/scenario_id are the discriminating keys.
FORBIDDEN_KEYS = ("label", "variant", "scenario_id")


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "erp" / "purchase_orders.json").exists():
            return parent
    raise RuntimeError("repo root not found -- run `uv run generator` first")


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Build an app pointed explicitly at the repo's data directory.

    This is why create_app takes a Settings argument: the test must control
    where the store loads from, without monkeypatching an lru_cache or setting
    environment variables before import.
    """
    app = create_app(Settings(erp_data_dir=repo_root() / "data" / "erp"))
    with TestClient(app) as c:  # `with` runs the lifespan, so the store loads
        yield c


@pytest.fixture(scope="module")
def raw_data() -> dict:
    d = repo_root() / "data" / "erp"
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in d.glob("*.json")
    }


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


def test_healthz_reports_record_counts(client: TestClient):
    """Counts, not {"status": "ok"}.

    You already know the expected numbers, so returning them turns "is it
    running" into "is it running with the right data" -- which is the question
    that actually matters when Docker mounts the wrong volume in Step 6.
    """
    body = client.get("/healthz").json()
    assert body["vendors"] == 12
    assert body["materials"] == 40
    assert body["plants"] == 4
    assert body["purchase_orders"] == 200
    assert body["goods_receipts"] == 188
    assert body["invoices"] == 212


def test_healthz_is_outside_the_odata_prefix(client: TestClient):
    """It is not part of the SAP surface and must survive a broken OData layer."""
    assert client.get("/healthz").status_code == 200
    assert client.get(f"{BASE}/healthz").status_code == 404


# --------------------------------------------------------------------------
# A_PurchaseOrder
# --------------------------------------------------------------------------


def test_purchase_order_entity_envelope(client: TestClient):
    body = client.get(f"{BASE}/A_PurchaseOrder('4500000001')").json()
    assert set(body) == {"d"}
    assert body["d"]["EBELN"] == "4500000001"
    assert isinstance(body["d"]["items"], list)


def test_purchase_order_unknown_key_returns_odata_error(client: TestClient):
    resp = client.get(f"{BASE}/A_PurchaseOrder('4599999999')")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "PO_NOT_FOUND"
    assert "4599999999" in body["error"]["message"]["value"]


def test_tolerance_is_composed_not_stored(client: TestClient, raw_data: dict):
    """The stored PO has no tolerance field; the endpoint joins it at response time.

    In SAP, tolerance keys are configuration per company code, not a field on
    EKKO. Storing them would be wrong SAP and would mean a tolerance change
    requires regenerating 200 scenarios.
    """
    assert "ToleranceConfig" not in raw_data["purchase_orders"][0]

    granite = next(
        po for po in raw_data["purchase_orders"] if po["LIFNR"] == "1000000006"
    )
    body = client.get(f"{BASE}/A_PurchaseOrder('{granite['EBELN']}')").json()
    tol = body["d"]["ToleranceConfig"]
    assert tol["PriceVariancePct"] == "2.0"
    assert tol["Source"] == "VENDOR_SPECIFIC"


def test_tolerance_falls_back_to_default(client: TestClient, raw_data: dict):
    """A vendor with no override must report DEFAULT, not VENDOR_SPECIFIC.

    for_vendor() always returns a ToleranceSet -- it never returns None -- so
    Source has to be decided by asking whether the vendor is IN by_vendor.
    Checking `is None` would label every default as vendor-specific.
    """
    unlisted = next(
        po for po in raw_data["purchase_orders"] if po["LIFNR"] == "1000000001"
    )
    body = client.get(f"{BASE}/A_PurchaseOrder('{unlisted['EBELN']}')").json()
    tol = body["d"]["ToleranceConfig"]
    assert tol["PriceVariancePct"] == "5.0"
    assert tol["Source"] == "DEFAULT"


# --------------------------------------------------------------------------
# A_MaterialDocumentItem
# --------------------------------------------------------------------------


def test_goods_receipts_for_a_po(client: TestClient, raw_data: dict):
    gr = raw_data["goods_receipts"][0]
    resp = client.get(
        f"{BASE}/A_MaterialDocumentItem",
        params={"$filter": f"PurchaseOrder eq '{gr['EBELN']}'"},
    )
    assert resp.status_code == 200
    rows = resp.json()["d"]["results"]
    assert rows and all(r["EBELN"] == gr["EBELN"] for r in rows)


def test_goods_receipts_empty_collection_when_none_posted(
    client: TestClient, raw_data: dict
):
    """GR_MISSING: the PO exists, nothing was received. 200 with results: [].

    This must NOT be a 404 and must NOT raise. A 404 here would make "no goods
    receipt was posted" indistinguishable from "no such PO", and those demand
    opposite agent behaviour -- escalate to the requisitioner versus report a
    bad reference.
    """
    with_grs = {g["EBELN"] for g in raw_data["goods_receipts"]}
    without = next(
        po["EBELN"] for po in raw_data["purchase_orders"] if po["EBELN"] not in with_grs
    )
    resp = client.get(
        f"{BASE}/A_MaterialDocumentItem",
        params={"$filter": f"PurchaseOrder eq '{without}'"},
    )
    assert resp.status_code == 200
    assert resp.json()["d"]["results"] == []


def test_goods_receipts_unknown_po_is_404(client: TestClient):
    """A deliberate deviation from strict OData collection semantics.

    Strict OData would return []. We 404 instead, because an agent that
    hallucinates a PO number would otherwise read [] as "no goods receipt was
    posted" and escalate GR_MISSING for an invoice that does not exist -- a
    wrong action taken with full confidence. Documented in decisions.md.
    """
    resp = client.get(
        f"{BASE}/A_MaterialDocumentItem",
        params={"$filter": "PurchaseOrder eq '4599999999'"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_NOT_FOUND"


def test_goods_receipts_requires_a_filter(client: TestClient):
    resp = client.get(f"{BASE}/A_MaterialDocumentItem")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "FILTER_REQUIRED"


def test_goods_receipts_rejects_unsupported_filter(client: TestClient):
    resp = client.get(
        f"{BASE}/A_MaterialDocumentItem", params={"$filter": "Supplier eq '1000000001'"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNSUPPORTED_FILTER"


def test_goods_receipts_rejects_unimplemented_option(client: TestClient):
    resp = client.get(
        f"{BASE}/A_MaterialDocumentItem",
        params={"$filter": "PurchaseOrder eq '4500000001'", "$top": "5"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNSUPPORTED_OPTION"


# --------------------------------------------------------------------------
# A_SupplierInvoice
# --------------------------------------------------------------------------


def test_supplier_invoice_entity(client: TestClient, raw_data: dict):
    belnr = raw_data["invoices"][0]["BELNR"]
    body = client.get(f"{BASE}/A_SupplierInvoice('{belnr}')").json()
    assert body["d"]["BELNR"] == belnr
    assert "XBLNR" in body["d"]


def test_supplier_invoice_unknown_key_is_404_not_a_crash(client: TestClient):
    """Must be a clean 404. Indexing the dict before the membership check would
    raise KeyError and surface as a 500."""
    resp = client.get(f"{BASE}/A_SupplierInvoice('5199999999')")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "INVOICE_NOT_FOUND"


def test_invoice_queue_is_a_sorted_collection(client: TestClient):
    body = client.get(f"{BASE}/A_SupplierInvoice").json()
    rows = body["d"]["results"]
    assert len(rows) == 212  # 200 + 12 DUP_INVOICE duplicates
    numbers = [r["BELNR"] for r in rows]
    assert numbers == sorted(numbers)


def test_both_duplicate_invoices_are_served_and_unmarked(
    client: TestClient, raw_data: dict
):
    """A duplicate must look perfect in isolation. Nothing flags it."""
    seen: dict[tuple[str, str], list[str]] = {}
    for inv in raw_data["invoices"]:
        seen.setdefault((inv["LIFNR"], inv["XBLNR"]), []).append(inv["BELNR"])
    pair = next(v for v in seen.values() if len(v) == 2)

    for belnr in pair:
        body = client.get(f"{BASE}/A_SupplierInvoice('{belnr}')").json()["d"]
        assert body["BELNR"] == belnr
        assert not any(
            "dup" in str(k).lower() or "duplicate" in str(k).lower() for k in body
        )


# --------------------------------------------------------------------------
# VendorHistory
# --------------------------------------------------------------------------


def test_vendor_history_derived_fields(client: TestClient, raw_data: dict):
    resp = client.get(f"{BASE}/VendorHistory", params={"VendorID": "'1000000006'"})
    assert resp.status_code == 200
    hist = resp.json()["d"]

    expected = [i for i in raw_data["invoices"] if i["LIFNR"] == "1000000006"]
    blocked = [i for i in expected if i["block_reason"] is not None]

    assert hist["Supplier"] == "1000000006"
    assert hist["SupplierName"] == "GRANITE DATA SOLUTIONS"
    assert hist["TotalInvoices"] == len(expected)
    assert hist["BlockedInvoices"] == len(blocked)
    assert hist["ApplicableTolerance"]["PriceVariancePct"] == "2.0"


def test_vendor_history_exposes_invoice_references(client: TestClient):
    """The ONLY route by which DUP_INVOICE is solvable.

    A duplicate is invisible from the invoice in front of you. The agent has to
    fetch the vendor's other references and notice a repeated XBLNR. Without
    this field, 12 of 200 scenarios are unsolvable by any agent -- and it would
    read as a model failure when it is a tool-design failure.
    """
    hist = client.get(
        f"{BASE}/VendorHistory", params={"VendorID": "'1000000006'"}
    ).json()["d"]
    refs = hist["InvoiceReferences"]
    assert refs and {"BELNR", "XBLNR", "BLDAT"} <= set(refs[0])
    assert [r["BELNR"] for r in refs] == sorted(r["BELNR"] for r in refs)


def test_a_duplicate_is_discoverable_through_vendor_history(
    client: TestClient, raw_data: dict
):
    counts: dict[tuple[str, str], int] = {}
    for inv in raw_data["invoices"]:
        key = (inv["LIFNR"], inv["XBLNR"])
        counts[key] = counts.get(key, 0) + 1
    vendor_id, vendor_ref = next(k for k, n in counts.items() if n > 1)

    hist = client.get(
        f"{BASE}/VendorHistory", params={"VendorID": f"'{vendor_id}'"}
    ).json()["d"]
    matching = [r for r in hist["InvoiceReferences"] if r["XBLNR"] == vendor_ref]
    assert len(matching) == 2


def test_vendor_history_unknown_vendor_is_404(client: TestClient):
    """The ODataError must be RAISED, not merely constructed."""
    resp = client.get(f"{BASE}/VendorHistory", params={"VendorID": "'9999999999'"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VENDOR_NOT_FOUND"


# --------------------------------------------------------------------------
# the test that must never be deleted
# --------------------------------------------------------------------------


def test_no_ground_truth_leaks_over_http(client: TestClient, raw_data: dict):
    """Walk every endpoint for every document and scan the raw response text.

    If a label reaches the tool layer, every eval silently becomes trivial, the
    scores mean nothing, and nothing looks broken. This is the test that checks
    the measurement apparatus itself for self-deception.
    """
    urls: list[str] = ["/healthz", f"{BASE}/A_SupplierInvoice"]
    urls += [f"{BASE}/A_PurchaseOrder('{po['EBELN']}')" for po in raw_data["purchase_orders"]]
    urls += [f"{BASE}/A_SupplierInvoice('{i['BELNR']}')" for i in raw_data["invoices"]]
    urls += [f"{BASE}/VendorHistory?VendorID='{v['LIFNR']}'" for v in raw_data["vendors"]]
    urls += [
        f"{BASE}/A_MaterialDocumentItem?$filter=PurchaseOrder eq '{po['EBELN']}'"
        for po in raw_data["purchase_orders"]
    ]

    for url in urls:
        resp = client.get(url)
        # Doubles as a coverage check: every URL here must actually be served.
        assert resp.status_code == 200, f"{url} returned {resp.status_code}"
        text = resp.text
        for label in LABEL_VALUES:
            assert f'"{label}"' not in text, f"label {label!r} leaked from {url}"
        for key in FORBIDDEN_KEYS:
            assert f'"{key}"' not in text, f"key {key!r} served from {url}"
