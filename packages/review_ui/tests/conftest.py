"""Fixtures for the review UI.

Both sides are real: the review app is served by its own TestClient, and its
ReviewClient is wired to the mock ERP's TestClient. Two FastAPI apps, real
routing on both, real serialisation between them, no socket. A stubbed ERP
here would test our idea of the approval contract instead of the contract.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mock_erp.app import create_app as create_erp_app
from mock_erp.settings import Settings as ErpSettings
from review_ui.app import create_app as create_review_app
from review_ui.client import ReviewClient
from review_ui.settings import ODATA_PREFIX, ReviewSettings

# The SC-0009 anchor, used throughout:
#   PO  4500000009  vendor 1000000010  line 00010  14.000 EA @ 41.90
#   GR  5000000901  13.000 received
#   INV 5100000901  14.000 @ 41.90, blocked QUANTITY_VARIANCE
INVOICE = "5100000901"
PO = "4500000009"

QUANTITY_PAYLOAD = {
    "correction_type": "AMEND_INVOICE_QUANTITY",
    "invoice_number": INVOICE,
    "inv_item_number": "0001",
    "from_quantity": "14.000",
    "to_quantity": "13.000",
}


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "erp" / "invoices.json").exists():
            return parent
    raise RuntimeError("could not locate the repo root")


@pytest.fixture
def erp():
    settings = ErpSettings(
        erp_data_dir=_repo_root() / "data" / "erp",
        db_path=Path(tempfile.mkdtemp()) / "approvals.sqlite3",
    )
    with TestClient(create_erp_app(settings), base_url="http://erp") as client:
        yield client


@pytest.fixture
def review_settings() -> ReviewSettings:
    return ReviewSettings(erp_base_url="http://erp", default_reviewer="ap.supervisor@example.com")


@pytest.fixture
def review_client(erp) -> ReviewClient:
    return ReviewClient("http://erp", client=erp)


@pytest.fixture
def ui(review_settings, review_client):
    return TestClient(create_review_app(review_settings, client=review_client))


@pytest.fixture
def propose(erp):
    """Raise a proposal the way the agent does, and hand back its id."""

    def _propose(payload=None, reasoning="Receipts total 13.000 against 14.000 invoiced.", **kw):
        body = {
            "invoice_number": (payload or QUANTITY_PAYLOAD)["invoice_number"],
            "payload": payload or QUANTITY_PAYLOAD,
            "agent_reasoning": reasoning,
            **kw,
        }
        response = erp.post(f"{ODATA_PREFIX}/ProposeCorrection", json=body)
        assert response.status_code == 200, response.text
        return response.json()["d"]["proposal_id"]

    return _propose


@pytest.fixture
def invoice_quantity(erp):
    """Read the served quantity. The gate is proved by watching this."""

    def _read(invoice_number=INVOICE, item="0001"):
        body = erp.get(f"{ODATA_PREFIX}/A_SupplierInvoice('{invoice_number}')").json()["d"]
        return next(line["MENGE"] for line in body["items"] if line["BUZEI"] == item)

    return _read


def form_hash(html: str) -> str:
    """The payload hash the page rendered into its action forms."""
    import re

    match = re.search(r'name="payload_hash" value="([0-9a-f]+)"', html)
    assert match, "no payload_hash rendered into the page"
    return match.group(1)
