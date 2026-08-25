"""HTTP client for the mock SAP OData service.

This is the agent's entire view of the ERP: five methods over httpx. It does
NOT import erp_domain -- the agent is as decoupled from the ERP's internals as
a third-party integration would be, and "it works against the OData contract"
means something because nothing else is shared. See decisions.md.

Every method either returns plain Python data or raises ErpError. Nothing
else escapes: the tool layer above turns an ErpError into a message the model
can read and react to, and it can only do that if there is exactly one
exception type to catch.
"""

from __future__ import annotations

import httpx


class ErpError(Exception):
    """A failed ERP call, normalised.

    `code` is the OData error code where the service gave one
    ("PO_NOT_FOUND", "STALE_PROPOSAL", ...) and a synthetic one otherwise.
    The agent surfaces `code` and `message` to the model, never the traceback.
    """

    def __init__(self, code: str, message: str, status: int):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code} ({status}): {message}")


class ErpClient:
    """One httpx.Client for the life of a run.

    Holding the client (rather than calling httpx.get per request) reuses the
    TCP connection across the six or so calls a run makes.

    `client` is injectable so tests can hand in an httpx.Client wired to an
    ASGI transport -- that runs the agent against the real FastAPI app, with
    real routing and real serialisation, and no socket.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._owns_client = client is None
        self.client = client or httpx.Client(base_url=base_url, timeout=timeout)

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _translate(response: httpx.Response) -> ErpError:
        """Turn any error response into an ErpError.

        The happy path is the service's own OData envelope. The fallback
        matters: FastAPI answers an unrouted path with {"detail": "Not Found"}
        and a crash with an HTML 500, so indexing ["error"] blindly would
        raise KeyError or JSONDecodeError -- the wrong exception type, which
        the tool layer would not catch, which would kill the run.
        """
        try:
            error = response.json()["error"]
            return ErpError(
                code=str(error["code"]),
                message=str(error["message"]["value"]),
                status=response.status_code,
            )
        except (ValueError, KeyError, TypeError):
            return ErpError(
                code="HTTP_ERROR",
                message=response.text[:500] or response.reason_phrase,
                status=response.status_code,
            )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            # Connection refused, DNS failure, timeout. Distinguished from an
            # HTTP error because the fix is different: start the service.
            raise ErpError("ERP_UNREACHABLE", str(exc), 503) from exc

        if response.is_error:
            raise self._translate(response)

        try:
            body = response.json()
        except ValueError as exc:
            raise ErpError("MALFORMED_RESPONSE", response.text[:500], 502) from exc

        if "d" not in body:
            raise ErpError("MALFORMED_RESPONSE", "response has no 'd' envelope", 502)

        # Unwrap OData V2's envelope once, here, so no caller upstream has to
        # know the dialect.
        return body["d"]

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    # -- the five capabilities --------------------------------------------

    def get_purchase_order(self, po_number: str) -> dict:
        """EKKO/EKPO plus the vendor's ToleranceConfig, composed by the service."""
        return self._get(f"/A_PurchaseOrder('{po_number}')")

    def get_invoice(self, invoice_number: str) -> dict:
        """RBKP/RSEG, with any applied amendments already overlaid."""
        return self._get(f"/A_SupplierInvoice('{invoice_number}')")

    def get_goods_receipts(self, po_number: str) -> list[dict]:
        """Flat MSEG rows for one PO. An empty list is a real answer, not an error."""
        data = self._get(
            "/A_MaterialDocumentItem",
            params={"$filter": f"PurchaseOrder eq '{po_number}'"},
        )
        return data["results"]

    def get_vendor_history(self, vendor_id: str) -> dict:
        """Aggregates for context: block rate, average GR-to-invoice lag, prior refs."""
        # The quotes are part of the OData function-import literal, not Python's.
        return self._get("/VendorHistory", params={"VendorID": f"'{vendor_id}'"})

    def propose_correction(
        self,
        invoice_number: str,
        payload: dict,
        agent_reasoning: str,
        scenario_id: str | None = None,
    ) -> dict:
        """Write a PROPOSED row. This is the closest the agent gets to writing.

        It does not change the invoice. Applying requires a human approval and
        a payload-hash match, and the agent has no tool for it.
        """
        return self._post(
            "/ProposeCorrection",
            {
                "invoice_number": invoice_number,
                "payload": payload,
                "agent_reasoning": agent_reasoning,
                "scenario_id": scenario_id,
            },
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Only close what we opened -- an injected client belongs to the caller."""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> ErpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
