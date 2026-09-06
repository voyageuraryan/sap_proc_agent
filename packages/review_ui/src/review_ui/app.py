"""The review app. Server-rendered HTML, POST-then-redirect, no JavaScript.

Why no JavaScript: this page must render on a laptop with no network, with no
build step and no CDN. An approval screen is a poor place to introduce a
supply chain, and `uv run` should be the whole setup.

Why a separate app from mock_erp: the ERP stands in for SAP, and SAP does not
serve your review UI. Keeping them apart means this app reaches the documents
over the same HTTP contract the agent does, and the approval routes it calls
are the same ones a curl would hit -- it holds no privileged path.

One property is enforced here rather than in the ERP: **you approved what you
were shown.** Each action form carries the payload hash that was rendered, and
the route refuses if the proposal has moved since the page was drawn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_ui.client import ReviewClient, ReviewError
from review_ui.settings import ReviewSettings, get_settings
from review_ui.views import STATUS_ORDER, build_view, evidence_rows, queue_counts

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Refusal when the page a reviewer acted from is older than the proposal.
STALE_PAGE = ReviewError(
    "STALE_PAGE",
    "This page was drawn before the proposal changed. Reload and look again before deciding.",
    409,
)


def get_client(request: Request) -> ReviewClient:
    return request.app.state.client


def get_config(request: Request) -> ReviewSettings:
    return request.app.state.settings


def create_app(
    settings: ReviewSettings | None = None,
    *,
    client: ReviewClient | None = None,
) -> FastAPI:
    """App factory. `client` is injectable so tests can wire this straight to
    the mock ERP's TestClient -- real routing on both sides, no socket."""
    settings = settings or get_settings()
    app = FastAPI(title="Invoice review")
    app.state.settings = settings
    app.state.client = client or ReviewClient(settings.erp_base_url, settings.request_timeout)

    def page(request: Request, template: str, **context) -> HTMLResponse:
        """Every render gets the header context, so no route can forget it."""
        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            context={
                "reviewer": settings.default_reviewer,
                "erp_base_url": settings.erp_base_url,
                "status_order": STATUS_ORDER,
                **context,
            },
        )

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/queue", status_code=303)

    @app.get("/healthz")
    async def healthz(review: ReviewClient = Depends(get_client)) -> dict:
        """Reports whether the ERP is reachable, not merely whether this
        process is up. A green light that only proves the UI booted would send
        someone hunting in the wrong place."""
        try:
            review.list_proposals()
        except ReviewError as exc:
            return {"ok": False, "erp": "unreachable", "code": exc.code}
        return {"ok": True, "erp": "reachable"}

    @app.get("/queue", response_class=HTMLResponse)
    async def queue(
        request: Request,
        status: Annotated[str | None, Query()] = None,
        flash: Annotated[str | None, Query()] = None,
        review: ReviewClient = Depends(get_client),
        config: ReviewSettings = Depends(get_config),
    ) -> HTMLResponse:
        if status is not None and status not in STATUS_ORDER:
            status = None
        error = None
        rows: list[dict] = []
        try:
            # Always fetch everything: the tab counts have to be right even
            # when a filter is on, and a count that lies about how much work
            # is waiting is worse than no count.
            everything = review.list_proposals()
            rows = [p for p in everything if status is None or p.get("status") == status]
        except ReviewError as exc:
            error, everything = exc, []

        views = [build_view(p) for p in rows[: config.page_size]]
        return page(
            request,
            "queue.html",
            proposals=views,
            counts=queue_counts(everything),
            total=len(everything),
            status=status,
            error=error,
            flash=flash,
        )

    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    async def detail(
        request: Request,
        proposal_id: str,
        flash: Annotated[str | None, Query()] = None,
        error_code: Annotated[str | None, Query()] = None,
        error_message: Annotated[str | None, Query()] = None,
        review: ReviewClient = Depends(get_client),
    ) -> HTMLResponse:
        try:
            proposal = review.get_proposal(proposal_id)
        except ReviewError as exc:
            return page(
                request,
                "queue.html",
                proposals=[],
                counts=queue_counts([]),
                total=0,
                status=None,
                error=exc,
                flash=None,
            )

        # The EFFECTIVE invoice -- base plus anything already applied -- so
        # staleness is measured against what the reviewer is looking at.
        invoice = None
        po = None
        receipts: list[dict] = []
        try:
            invoice = review.get_invoice(proposal["invoice_number"])
            line = (invoice.get("items") or [{}])[0]
            if line.get("EBELN"):
                po = review.get_purchase_order(line["EBELN"])
                receipts = review.get_goods_receipts(line["EBELN"])
        except ReviewError:
            # Evidence is best-effort: a reviewer with a partial page is
            # better served than one staring at a 500.
            pass

        view = build_view(proposal, invoice)
        payload = proposal.get("payload") or {}
        return page(
            request,
            "proposal.html",
            view=view,
            evidence=evidence_rows(invoice, po, receipts, payload.get("inv_item_number", "")),
            flash=flash,
            error=(ReviewError(error_code, error_message or "", 409) if error_code else None),
        )

    # -- the three human actions ------------------------------------------
    #
    # All POST-then-redirect: a reviewer who refreshes after approving must
    # not re-submit, and the browser back button must not show a stale form.

    def _back(proposal_id: str, *, flash: str | None = None, error: ReviewError | None = None):
        query = ""
        if flash:
            query = f"?flash={flash}"
        elif error:
            query = f"?error_code={error.code}&error_message={error.message}"
        return RedirectResponse(f"/proposals/{proposal_id}{query}", status_code=303)

    def _guard(review: ReviewClient, proposal_id: str, payload_hash: str) -> dict:
        """Refuse to act on a page older than the proposal it was drawn from."""
        proposal = review.get_proposal(proposal_id)
        if proposal.get("payload_hash") != payload_hash:
            raise STALE_PAGE
        return proposal

    @app.post("/proposals/{proposal_id}/approve")
    async def approve(
        proposal_id: str,
        reviewer: Annotated[str, Form()],
        payload_hash: Annotated[str, Form()],
        review: ReviewClient = Depends(get_client),
    ):
        try:
            _guard(review, proposal_id, payload_hash)
            review.approve(proposal_id, reviewer.strip())
        except ReviewError as exc:
            return _back(proposal_id, error=exc)
        return _back(proposal_id, flash="Approved. Nothing has changed yet — apply to commit it.")

    @app.post("/proposals/{proposal_id}/reject")
    async def reject(
        proposal_id: str,
        reason: Annotated[str, Form()],
        payload_hash: Annotated[str, Form()],
        review: ReviewClient = Depends(get_client),
        config: ReviewSettings = Depends(get_config),
    ):
        try:
            _guard(review, proposal_id, payload_hash)
            review.reject(proposal_id, config.default_reviewer, reason.strip())
        except ReviewError as exc:
            return _back(proposal_id, error=exc)
        return _back(proposal_id, flash="Rejected. The invoice is untouched.")

    @app.post("/proposals/{proposal_id}/apply")
    async def apply(
        proposal_id: str,
        payload_hash: Annotated[str, Form()],
        review: ReviewClient = Depends(get_client),
    ):
        try:
            proposal = _guard(review, proposal_id, payload_hash)
            # The payload goes back to the ERP so it can hash-check it against
            # what was approved. This app gets no shortcut past that check.
            review.apply(proposal_id, proposal["payload"])
        except ReviewError as exc:
            return _back(proposal_id, error=exc)
        return _back(proposal_id, flash="Applied. The invoice now reflects the correction.")

    return app


app = create_app()
