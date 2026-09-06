# Demo — shot list

Target: **4 minutes**. The run is scripted (`scripts/demo.py`) so it is
identical every take and every number on screen is read live from the running
services. Nothing here is staged.

## Setup

Three panes. Left is the terminal, right is the browser at
`http://localhost:8001`, and keep a fourth tab on `evals/reports/all-baseline.md`
for the last beat.

```bash
uv sync
uv run uvicorn mock_erp.app:app --port 8000       # pane A
uv run uvicorn review_ui.app:app --port 8001      # pane B
uv run python scripts/demo.py                     # pane C — record this one
```

`--mode baseline` (the default) needs no API key and is byte-identical every
run: use it for the recording. `--mode live` uses a real model — worth one take
if the key is in `.env`, and worth showing the cost table from that run.
`--no-pause` for asciinema.

---

## The beats

### 0 · Cold open — 15s

> "Roughly 95% of enterprise AI pilots never reach production. Almost none of
> them fail because the model was not good enough. They fail on integration,
> permissions, and the fact that nobody in finance will let a language model
> touch a ledger on its own. So I built the integration-hard version."

Do not show code yet.

### 1 · The queue — 20s

Script act 1. 212 invoices, 84 blocked.

> "This is a mock SAP system — purchase orders, goods receipts, supplier
> invoices, served over an OData V2 dialect. 84 invoices are blocked and
> waiting on a person."

### 2 · The trap — 45s  ⟵ *the most important 45 seconds*

Script act 2, side by side.

> "Two invoices. Both short-delivered. Look at what was billed. A billed
> fourteen against thirteen received — that is over-billing, and it needs
> correcting. B billed one against one received — that is a perfectly normal
> partial delivery, and the right answer is to pay it.
>
> They look almost identical. An agent that proposes a correction on B is worse
> than useless: it makes work and it burns the trust the whole thing depends
> on. The eval set has twenty of A and sixteen of B, and separating them is the
> headline metric."

Slow down here. This is the beat that shows you understand the domain rather
than the framework.

### 3 · The agent — 45s

Script act 3.

> "Six tools, an iteration cap, and a typed verdict. It reads the invoice, the
> purchase order, the goods receipts, and the vendor history — and the
> tolerance is not a constant in my code, it comes back on the purchase-order
> response, per supplier, because in SAP tolerance keys are configuration per
> company code, not fields on the document.
>
> It concludes QUANTITY_EXCEEDS_RECEIPT and proposes a correction, citing the
> figures it compared. That verdict is a Pydantic model registered as the
> terminal tool's schema — the model literally cannot finish except by filling
> in the contract, and if it fails validation the error goes back and it tries
> again."

### 4 · The wall — 40s  ⟵ *the second most important beat*

Script act 4.

> "The agent has raised a proposal. Watch the quantity: fourteen.
>
> Try to apply it without an approval — 409. And that is the weak version of
> the claim. The strong version is that the agent's tool list contains no way
> to apply anything at all. Not refused at runtime — absent from the schema the
> model reads. There is a test that enumerates the registry and asserts it."

### 5 · The human — 50s

Switch to pane B (the browser) *while the script runs act 5*.

> "This is the reviewer's screen. It shows the change, the agent's reasoning,
> and then the evidence unsummarised — the PO line, every goods receipt, and
> this supplier's tolerance. Because the point of a human gate is that a person
> can disagree, and a queue that only says 'the agent wants to change
> something' is a slower rubber stamp.
>
> Approving from a stale tab is refused: you approve what you were shown.
>
> Approve it. Quantity: still fourteen — approving is not applying.
>
> Now apply a payload nobody approved — 409, payload mismatch. The hash of the
> approved bytes is checked against the bytes sent.
>
> Apply the real one. Thirteen. That is the first and only write, and it is
> visible on the same URL the agent read from — so 'no write happened' is
> something you check, not something I assert."

### 6 · The evidence — 25s

Script act 6, then flick to a trace file or Langfuse.

> "Every run is traced and priced per call. The interesting thing is the shape:
> input tokens grow every turn because the whole transcript is re-sent, so cost
> is roughly quadratic in tool calls and about ninety percent of it is input.
> That is the number you quote at ten thousand invoices a month, and it says
> the lever is fewer round trips — not a cheaper model."

### 7 · The score — 30s

Run in pane A:

```bash
uv run proc-evals --split all --mode baseline
```

> "Two hundred labelled scenarios, stratified holdout, scored against ground
> truth the agent structurally cannot reach — one module in the repo can open
> the labels, and a test parses every other module to prove it.
>
> Accuracy is reported. Safety fails the build: zero invoices changed, zero
> proposals applied without approval, zero labels leaked.
>
> That hundred percent is a rule engine, not a model — it is the floor, and it
> scores perfectly because I generated the data with rules. Which is the useful
> question: what does the model have to be worth paying for? On this dataset,
> duplicate detection and knowing when to decline."

### 8 · Close — 15s

> "Evals before polish. Approval gate before the agent, because it shapes the
> data model. Forty-nine design decisions written down with the alternatives I
> rejected. The model is close to the least interesting component here, and
> that is the point."

---

## What to cut if you need 2 minutes

Keep 2, 4, 5. Drop 1, 6, 7 and compress 3 to "it reads four documents and
concludes X". The trap and the wall are the whole pitch.

## What not to do

- Do not narrate the code. Nobody watches a video to read a `for` loop.
- Do not apologise for the mock. Say it once, in beat 1, then move on.
- Do not claim a real accuracy number until the golden cassettes are recorded
  against a live model. Say "the rule baseline gets 100%, and here is why that
  is the floor and not the result."
