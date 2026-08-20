# Design decisions

Newest first. Each entry records the decision, what I rejected, and what would make
me revisit it. If an entry has no rejected alternative, it wasn't a decision — it was
a default, and it doesn't belong here.

**Template**

```
## YYYY-MM-DD — <decision in one line>
**Decision:** what I'm doing
**Rejected:** the alternative(s), and why they lost
**Tradeoff:** what this costs me
**Revisit if:** the condition that would change my mind
```

---

## 2026-08-19 — All file writes pin encoding and newline explicitly

**Decision:** Every `open()` in the generator passes `encoding="utf-8", newline="\n"`.

**Rejected:** Plain text mode. On Windows that uses the locale encoding and
translates `\n` to `\r\n`; on Linux CI it does neither. Same seed, same code,
different bytes.

**Why it matters:** the byte-identical determinism test would have passed on my
machine *and* passed in CI while the two disagreed with each other — the failure
only appears when you compare across platforms, which nothing in the suite does.
A test that cannot fail is worse than no test, because you trust it.

**Revisit if:** never. This is a straight bug fix.

---

## 2026-08-19 — Config is a validated object, not a dict

**Decision:** `GeneratorConfig` (Pydantic, `extra="forbid"`) is built once in
`cli.py` and passed inward as a parameter. No module below the CLI reads a file.

**Rejected:** (a) Passing the raw `yaml.safe_load` dict — no validation, and
`config["epoch_dat"]` is a `KeyError` twelve frames deep instead of a clear
message at load. (b) Reading the YAML inside `master_data.py` / `scenarios.py`,
which is what I first wrote — it made the generator depend on the working
directory and unimportable from a test.

**Tradeoff:** one more model to keep in step with the YAML. `extra="forbid"`
turns that into a loud failure rather than a silent drift.

**Why it matters:** "configuration is read at the edge and passed inward" is
what makes the generator a pure function of (config, seed) — which is the
property the whole eval system rests on.

**Revisit if:** config grows nested enough to want sub-models (likely in Step 3,
when tolerances get a real shape).

---

## 2026-08-19 — Document numbers derive from the scenario index, not from rng

**Decision:** `EBELN = f"45{seq:08d}"`, sequential and 1-based.

**Rejected:** `rng.randint(0, 99999999)`. Deterministic given the seed, so the
test passed — but every rng call consumes the stream, so adding one random draw
in Step 3 would renumber all 60 documents. The committed-data diff becomes 200
changed records instead of the 3 I meant, which destroys the reason for
committing the data at all.

**Bonus:** SAP assigns document numbers sequentially from number range objects
(SNRO/NRIV). Random ones are a tell.

**Revisit if:** Step 3 needs several documents per scenario — the format has to
stretch (e.g. `50{seq:06d}{line:02d}`) for partial deliveries and duplicates.

---

## 2026-08-19 — PO prices derive from a material valuation price

**Decision:** `Material` carries `base_price` (aliased `STPRS`). PO net price is
that price wobbled ±5% with integer Decimal arithmetic; quantity is drawn from a
band keyed on the material's unit of measure.

**Rejected:** A flat `rng.randint(20, 1000)` per line. It priced a work shirt at
$378 and could price a laptop at $23. Anyone who opens `purchase_orders.json`
sees that in five seconds, and it contradicts the "realistic mock" claim in the
README.

**Why the material master:** SAP's material master genuinely carries a valuation
price (MBEW-STPRS), so this is authentic rather than a workaround. Unit of
measure driving order size is the same idea — gloves by the box in tens, laptops
in ones.

**Revisit if:** Step 3 wants price variance correlated with vendor rather than
material.

---

## 2026-08-19 — `unit_price` on the invoice item is a deliberate simplification

**Decision:** `InvoiceItem.unit_price` is aliased `NETPR`.

**Reality:** real `RSEG` carries `WRBTR` — the line **amount** — not a unit
price. A correct implementation would divide by quantity before comparing.

**Why simplified:** modelling the amount means every price comparison carries a
division, and the interesting logic in v1 is tolerance evaluation, not
arithmetic. Field-to-field comparison against `EKPO-NETPR` keeps the agent's
reasoning legible in a trace.

**This is the simplification an SAP reviewer is most likely to spot.** Better to
have the answer ready than to be technically perfect.

**Revisit if:** multi-line invoices arrive, where partial amounts stop mapping
cleanly onto unit prices.

---

## 2026-08-19 — Goods receipts are flat item rows; PO and invoice are header+items

**Decision:** `GoodsReceipt` is one row per MSEG line, carrying `MBLNR` and
`EBELN`/`EBELP` on every row. `PurchaseOrder` and `Invoice` nest their items.

**Rejected:** Making GR header+items for symmetry.

**Why:** S/4HANA's own OData publishes material documents as separate header and
item entity sets, with the item entity carrying the header keys — so flat rows
*are* the SAP shape, not a shortcut. It also matches the query the agent runs:
"sum what was received against this PO line" is one comprehension over flat
rows, versus a nested double loop.

**Tradeoff:** looks inconsistent until explained. Explained here, and in the
model docstring, so it reads as a choice rather than an oversight.

**Revisit if:** the mock ERP needs to serve a GR header entity in its own right.

---

## 2026-08-16 — Generated scenario data is committed to git

**Decision:** The generator's output (JSON) is committed, not gitignored.

**Rejected:** Generating at container build or service startup. Smaller repo, and it
forces determinism to be genuinely real rather than assumed.

**Tradeoff:** Repo carries the data, and every generator change produces a large diff.
That diff is actually the upside — a change to the generator becomes visibly a change
to the data, and I can review it. It also means a reviewer can read the scenarios
without running anything, which matters for a portfolio repo where most visitors will
never clone it.

**Revisit if:** the data grows past a few MB, or generator changes start producing
diffs too large to review meaningfully.

---

## 2026-08-16 — Tolerances: global defaults with per-vendor overrides

**Decision:** One default tolerance set (price %, quantity %) with an override table
keyed by vendor.

**Rejected:** (a) A single global tolerance — simplest, and easiest to write evals
against, but it makes `get_vendor_history` decorative. If vendor identity never changes
the answer, the tool is theatre. (b) Full SAP tolerance keys (upper/lower, absolute and
percentage, per company code) — maximum authenticity, but heavy modelling for something
v1 barely exercises.

**Tradeoff:** Not how SAP actually structures tolerance keys, so an SAP specialist will
spot the simplification. I'd rather they spot a deliberate simplification I can explain
than an accidental one I can't.

**Why it matters:** it enables the scenario I most want in the demo — the *same*
variance being acceptable for one vendor and not another. That is the case where the
agent has to reason rather than pattern-match, and it's the case a naive implementation
gets wrong.

**Revisit if:** an SAP reviewer's first question is about tolerance keys, or v2 needs
per-company-code behaviour.

---

## 2026-08-16 — Single-line POs in v1, but line items modelled as a list

**Decision:** Every generated PO has exactly one line item, but the schema models
items as an array (`PurchaseOrder` → `PurchaseOrderItems`) from day one.

**Rejected:** (a) Multi-line from the start — realistic, since real POs carry 5-50
lines, but matching becomes combinatorial: which invoice line maps to which PO line
maps to which goods receipt. That is a hard sub-problem that would consume weeks
before the agent loop existed at all. (b) Flat header fields with no item array —
marginally simpler now, but going multi-line later would mean a schema migration
*plus* rewriting every tool contract and every eval fixture.

**Tradeoff:** The demo is less realistic than production procurement, and I say so in
the README rather than waiting to be asked. The array shape costs nothing today and
matches how OData models the entity anyway, so the expensive part of the migration is
paid for up front at zero price.

**Revisit if:** v1 is done and stable, and multi-line matching is the most valuable
remaining thing to demonstrate.

---

## 2026-08-16 — Python 3.13, not 3.14

**Decision:** Pin 3.13 in `.python-version`, `requires-python`, and ruff's
`target-version`.

**Rejected:** 3.14. Current, but the transitive dependency trees of this stack
(LiteLLM, Langfuse, agent frameworks, anything with C extensions) routinely lag the
newest CPython by months, and Docker base images are thinner. That's an evening lost
to a problem unrelated to the project.

**Tradeoff:** Slightly behind the newest release.

**Revisit if:** the whole dependency set publishes 3.14 wheels, or a 3.14 feature
becomes load-bearing.

---

## 2026-08-16 — uv for dependency and environment management

**Decision:** uv, with `pyproject.toml` and a committed `uv.lock`.

**Rejected:** (a) `pip` + `requirements.txt` — no real lockfile, so "clone and run"
isn't reproducible. On a project whose data generator promises *same seed → identical
output*, shipping a non-reproducible environment would contradict the thesis in the
first file anyone opens. (b) Poetry — equally correct and better documented, but
slower and more ceremony, and the ecosystem has clearly moved.

**Tradeoff:** Less StackOverflow history when something breaks. Newer tool, thinner
long tail of answers.

**Why it matters commercially:** in an AP department, "the agent released the wrong
invoice" is a financial control failure — auditable and reportable. Nobody buys a
system whose behaviour they can't reproduce. Locked dependencies and seeded data aren't
developer hygiene here; they're the precondition for the thing being sellable at all.

**Revisit if:** uv's workspace support proves unstable under CI.

---

## 2026-08-16 — Monorepo with a uv workspace

**Decision:** `mock_erp` and `generator` are separate packages sharing one repo and
one lockfile.

**Rejected:** (a) One flat package — the generator has no business depending on
FastAPI, and I want to run it standalone. (b) Two repos — version-syncing a shared
schema across repo boundaries for no gain at this size. Separate repos make sense when
separate teams own them; one person owns both of these.

**Tradeoff:** More config up front, and anyone reading the repo needs to understand
what a workspace is.

**Revisit if:** the generator ever needs to be published or consumed independently.

---

## 2026-08-16 — The mock ERP runs as a separate service, not an in-process module

**The most important decision in this project.**

**Decision:** The mock ERP is its own FastAPI application in its own container. The
agent reaches it over HTTP using OData semantics. The approval state machine — and
every write path — lives inside the ERP service.

**Rejected:** A single application where the agent's tools call Python functions
directly. Faster to build and far less Docker plumbing.

**Why it lost:** the spec's non-negotiable is that a jailbroken prompt must not be able
to cause a write. If the gate is an in-process function call, that guarantee holds only
as long as nobody refactors carelessly — it's a code convention, not a security
property. Putting the gate behind a network boundary means the untrusted input (the
LLM's output) and the authority to write live in different processes. A prompt
injection can make the agent *ask*. It cannot make the ERP *comply*.

There's a secondary reason: with an in-process module there is no actual integration
to point at, and "I built the integration-hard version" becomes a claim rather than a
demonstration.

**Tradeoff:** an extra container, real network error handling, serialisation overhead,
and a slower local dev loop. All of that is work I'd have to do against real SAP
anyway, so it's cost I'd rather pay in the demo than discover in production.

**Revisit if:** never, for v1. This one is load-bearing.

---

## 2026-08-16 — Synthetic data instead of a real SAP sandbox

**Decision:** Generate all PO / goods-receipt / invoice triples deterministically from
a seed, with ground-truth labels in a sidecar the agent's tools cannot read.

**Rejected:** SAP ES5 or a similar public sandbox. No licence, it's sales-order shaped
rather than procurement shaped, it has intermittent downtime, and — decisively — I
can't inject the labelled defects the eval set depends on. A demo that breaks when
someone else's free sandbox goes down is not a demo.

**Tradeoff:** My mock is almost certainly cleaner than production SAP, so the agent
looks better here than it would in the wild. Stated openly in the README rather than
left to be discovered. Mitigated by seeding deliberately adversarial cases —
particularly valid partial deliveries (which resemble over-invoicing) and
underdetermined cases where the correct answer is to escalate rather than resolve.

**Revisit if:** I get access to a real system with permission to write test data.