"""HTTP client for the review UI.

Deliberately a SECOND client, not a reuse of agent.erp_client.ErpClient. The
agent's client has no approve, reject or apply method -- that absence is the
guarantee the whole project rests on, and importing it here and bolting the
human capabilities onto it would erase exactly the distinction being
demonstrated.

The asymmetry is asserted in tests: this client can approve and the agent's
cannot, and neither can grow the other's methods by accident.
"""

from __future__ import annotations

import httpx

from review_ui.settings import APPROVAL_PREFIX, ODATA_PREFIX


class ReviewError(Exception):
    """A failed call to the ERP, normalised so a template can render it."""

    def __init__(self, code: str, message: str, status: int):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code} ({status}): {message}")


class ReviewClient:
    """Reads documents and drives the approval state machine.

    `client` is injectable so tests can wire this to the mock ERP's own
    TestClient -- real routing, real serialisation, no socket.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _translate(response: httpx.Response) -> ReviewError:
        """Any error response becomes one exception type.

        The OData envelope is the happy path; FastAPI's own {"detail": ...}
        and an HTML 500 are not, and indexing ["error"] blindly would raise
        the wrong type -- which a route handler would then fail to catch and
        turn into a 500 page instead of a readable message.
        """
        try:
            error = response.json()["error"]
            return ReviewError(
                code=str(error["code"]),
                message=str(error["message"]["value"]),
                status=response.status_code,
            )
        except (ValueError, KeyError, TypeError):
            return ReviewError("HTTP_ERROR", response.text[:400], response.status_code)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.RequestError as exc:
            raise ReviewError("ERP_UNREACHABLE", str(exc), 503) from exc
        if response.is_error:
            raise self._translate(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise ReviewError("MALFORMED_RESPONSE", response.text[:400], 502) from exc
        if "d" not in body:
            raise ReviewError("MALFORMED_RESPONSE", "response has no 'd' envelope", 502)
        return body["d"]

    # -- the queue --------------------------------------------------------

    def list_proposals(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else None
        return self._request("GET", f"{APPROVAL_PREFIX}/proposals", params=params)["results"]

    def get_proposal(self, proposal_id: str) -> dict:
        return self._request("GET", f"{APPROVAL_PREFIX}/proposals/{proposal_id}")

    # -- the three human actions -----------------------------------------

    def approve(self, proposal_id: str, approved_by: str) -> dict:
        return self._request(
            "POST",
            f"{APPROVAL_PREFIX}/proposals/{proposal_id}/approve",
            json={"approved_by": approved_by},
        )

    def reject(self, proposal_id: str, rejected_by: str, reason: str) -> dict:
        return self._request(
            "POST",
            f"{APPROVAL_PREFIX}/proposals/{proposal_id}/reject",
            json={"rejected_by": rejected_by, "reason": reason},
        )

    def apply(self, proposal_id: str, payload: dict) -> dict:
        """Send the payload back so the ERP can hash-check it against the
        approved one. The UI holds no privileged path: it goes through the
        same endpoint, and the same check, as anything else would."""
        return self._request(
            "POST",
            f"{ODATA_PREFIX}/ApplyCorrection",
            json={"proposal_id": proposal_id, "payload": payload},
        )

    # -- the evidence a reviewer needs -----------------------------------

    def get_invoice(self, invoice_number: str) -> dict:
        return self._request("GET", f"{ODATA_PREFIX}/A_SupplierInvoice('{invoice_number}')")

    def get_purchase_order(self, po_number: str) -> dict:
        return self._request("GET", f"{ODATA_PREFIX}/A_PurchaseOrder('{po_number}')")

    def get_goods_receipts(self, po_number: str) -> list[dict]:
        data = self._request(
            "GET",
            f"{ODATA_PREFIX}/A_MaterialDocumentItem",
            params={"$filter": f"PurchaseOrder eq '{po_number}'"},
        )
        return data["results"]

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
