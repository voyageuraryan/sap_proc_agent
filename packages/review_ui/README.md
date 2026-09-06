# review-ui

The human half of the approval gate. Until now the only way to approve a
correction was `curl`.

## Running it

```bash
uv run uvicorn mock_erp.app:app --port 8000          # the ERP
uv run uvicorn review_ui.app:app --port 8001         # this
open http://localhost:8001
```

`REVIEW_ERP_BASE_URL` points it at the ERP's **root** (not the OData prefix) —
it needs both the read endpoints and the approval routes, which sit on
different mounts.

## Shape

A separate FastAPI app, not routes bolted onto `mock_erp`. The ERP stands in
for SAP, and SAP does not serve your review screen. Keeping them apart means
this app reaches documents over the same HTTP contract the agent does, and
calls the same approval endpoints a `curl` would — **it holds no privileged
path**.

Server-rendered Jinja2, POST-then-redirect, **zero JavaScript**. No build step,
no CDN, no npm: it renders on a laptop with no network. An approval screen is a
poor place to introduce a supply chain.

`ReviewClient` is a second client, not an extension of `agent.erp_client`.
The agent's client has no `approve`, `reject` or `apply` — that absence is the
guarantee the project rests on, and bolting the human capabilities onto it
would have erased exactly the distinction being demonstrated. A test asserts
the two method sets stay disjoint.

## The property this app adds

**You approved what you were shown.** Every action form carries the payload
hash that was rendered into it, and the route refuses (`STALE_PAGE`) if the
proposal has moved since the page was drawn. Without it, a reviewer with a
stale tab could approve a payload they never read — the failure the gate
exists to prevent, arriving through the human instead of the agent.

The page also warns *before* you click when the invoice no longer holds the
value the correction was computed from, rather than letting the ERP refuse it
afterwards.

## What it is not

There is no authentication. The identity recorded as `approved_by` is whatever
the form said, and the header says so on every page. See `decisions.md` for
where SSO would attach and what else has to change with it.
