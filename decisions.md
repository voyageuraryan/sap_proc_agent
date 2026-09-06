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
---

## 2026-08-25 — The agent does not import `erp_domain`

**Decision:** The `agent` package depends on `httpx`, `litellm`, `pydantic` and
`pydantic-settings`. It does not depend on `erp-domain`, `mock-erp`, or `generator`. It
sees SAP documents only as dicts that came back over HTTP.

**Rejected:** Importing the shared models to get typed responses for free.

**Why:** The claim this project makes is "it integrates". If the agent imported the
ERP's own models, that claim would be a fact about a Python import rather than about
an interface — and the day it points at real SAP OData, every one of those imports is
a lie that has to be unwound. An agent that only ever saw dicts needs no change.

**Tradeoff:** No type checking on ERP responses inside the agent, and two definitions
of the four correction payloads that can drift apart. Paid for by
`test_contract.py`, which is the only place in the agent package that imports from
`mock_erp` — and does so to *compare*, not to reuse. It asserts field-name parity and
posts every payload shape the agent can build against the live endpoint. A shared
import would have made that test a tautology.

**Revisit if:** never for v1.

---

## 2026-08-25 — The agent gets `propose_correction` but not `apply_correction`

**Decision:** The agent's tool registry has five read/propose tools plus the terminal
tool. There is no apply, approve, or reject tool, and `ErpClient` has no method for
them.

**Rejected:** Giving it `apply_correction` and relying on the state machine to return
409 until a human approves.

**Why:** Within a single run there is no approval, so apply could only ever fail —
a tool that can only error is a tool the model will waste turns on. More importantly,
"the agent cannot write" is a much stronger sentence when the capability is *absent*
rather than *refused*. `test_the_agent_has_no_tool_that_applies_anything` asserts the
registry by name, and the approval routes live on a different router, so
`test_the_agent_surface_has_no_approval_capability` can read the OpenAPI paths and
prove it.

**Tradeoff:** The end-to-end demo needs a human step (or a curl) between proposal and
effect. That is the demo, not a gap in it.

---

## 2026-08-25 — `Resolution` is registered as the terminal tool's schema

**Decision:** Ending a run means calling `submit_resolution`, whose `args_model` **is**
the `Resolution` Pydantic model. Structured output is enforced by the tool-call schema.
A `@model_validator` carries the coherence rules a JSON Schema cannot express
(PROPOSE_CORRECTION requires a correction payload; ESCALATE requires a reason;
`classification=CLEAN` cannot ask for a correction). A validation failure is fed back
to the model as the tool result, and it retries.

**Rejected:** (a) asking for JSON in the system prompt and parsing the final message —
prose leaks in, there is no schema to point at, and a retry loop has to be hand-rolled;
(b) `response_format={"type": "json_object"}` — not portable across the providers
LiteLLM fronts, and it does not compose with tool calling in the same turn.

**Why:** The model cannot finish except by filling in the contract, so the failure mode
becomes "rejected with an explanation" instead of "returned something unparseable".
The validator turns the schema into a teacher rather than a gate.

**Tradeoff:** A model that cannot satisfy the validator burns iterations retrying.
Bounded by `max_iterations`, and visible in the trace as `RESOLUTION_INVALID` records.

---

## 2026-08-25 — Tool failures become message content, never exceptions

**Decision:** `_execute_tool_call` cannot raise. Unknown tool, malformed JSON
arguments, schema violation, ERP 404, or an unexpected exception all return a string
that goes back to the model as the `tool` message for that `tool_call_id`.

**Why:** The protocol invariant is that every `tool_call` in an assistant message must
be answered by exactly one `tool` message with the matching id. Break it one way (drop
the assistant message) and the model re-asks forever, burning tokens. Break it the
other (append the assistant message with no results) and the provider returns 400. An
exception escaping the executor breaks it the second way. `ErpError` therefore exists
as a single normalised type precisely so there is one thing to catch — including for
non-OData bodies like FastAPI's own `{"detail": ...}` 404 and an HTML 500, which is
what `ErpClient._translate` is for.

**Consequence worth naming:** an `ERP error PO_NOT_FOUND` in the transcript is
*evidence*, and escalating on it is the correct answer. Making the failure legible to
the model is what lets it behave well.

---

## 2026-08-25 — A duplicated enum value is a silent, not a loud, failure

**Not a decision — a bug worth remembering.** Three `Classification` members were
written with the same value:

```python
VALID_PARTIAL_DELIVERY = "VALID_PARTIAL_DELIVERY"
DUPLICATE_INVOICE      = "VALID_PARTIAL_DELIVERY"   # copy-paste
INSUFFICIENT_EVIDENCE  = "VALID_PARTIAL_DELIVERY"   # copy-paste
```

Python's `Enum` turns a duplicate value into an **alias**, not a member. The two
members disappear from `list(Classification)` and therefore from the JSON Schema, so
the model could never emit `DUPLICATE_INVOICE` — and nothing raised. The eval would
have scored every DUP_INVOICE scenario against the wrong class and reported a
plausible number.

`test_no_classification_value_is_an_accidental_alias` compares
`len(list(Classification))` against `len(Classification.__members__)`. The general
lesson is the one worth keeping: **a wrong answer that does not crash is more expensive
than a crash.**

---

## 2026-08-25 — Classifications are the agent's vocabulary; the eval owns the mapping

**Decision:** `Classification` uses names like `PRICE_VARIANCE_EXCEEDS_TOLERANCE`, not
the generator's `PRICE_MAJOR`. `LABEL_FOR_CLASSIFICATION` maps one to the other, and a
test asserts it is total and one-to-one over both taxonomies.

**Rejected:** Making the agent emit the generator's label names directly.

**Why:** Two reasons. Scoring becomes an explicit, reviewable table instead of an
accident of string equality — adding a classification cannot compile without deciding
what it scores as. And the agent's vocabulary describes *what it observed*, which is
the thing a human reviewer reads; the label is grader bookkeeping.

---

## 2026-08-25 — `scenario_id` is closed over, not asked for

**Decision:** `build_tools(client, scenario_id=...)` captures the id; it is not a field
on `ProposeCorrectionArgs`.

**Why:** The model has no way to know it, so asking invites a hallucinated value — and
a proposal tagged with the wrong scenario means the eval scores the wrong row. Eval
bookkeeping is the harness's business. Asserted by
`test_scenario_id_is_not_something_the_model_can_set`.

---

## 2026-08-25 — One nudge before giving up on a bare reply

**Decision:** If the model answers with prose and no tool call, the loop appends a
short `user` message telling it to call a tool or submit, and continues. A second bare
reply ends the run with `NO_TOOL_CALL`.

**Rejected:** (a) stopping immediately — a model that opens with "Let me start by
fetching the invoice." would kill the run for a formatting slip; (b) nudging
indefinitely — that is just `max_iterations` with extra steps and a worse trace.

**Tradeoff:** One wasted round trip in the bad case. `NO_TOOL_CALL` now means "asked
twice, still would not use a tool", which is a real signal rather than noise.

---

## 2026-08-25 — The agent's tests use a scripted model and a real ERP

**Decision:** `completion_fn` is injected into `run_agent`, and the tests drive it with
a fixed sequence of tool calls. The ERP is *not* faked: FastAPI's `TestClient` is an
`httpx.Client` subclass, so it is injected straight into `ErpClient` and requests go
through real routing, real dependency injection, real Pydantic serialisation and the
real store — with no socket.

**Why:** Model output is the one input that cannot be made deterministic, so it gets
scripted; that tests the harness, and Step 8's evals test the reasoning. Faking the ERP
as well would have tested our *idea* of the contract instead of the contract — which is
exactly how the Step 5 SAP-alias leak survived as long as it did.

**Tradeoff:** The scripted-model fakes are ~90 lines of `conftest.py` mimicking the
provider's message shape, and they can drift from LiteLLM's actual objects. Bounded by
`_message_to_dict` handling both pydantic models and mappings, and by the real-socket
smoke run in the README.

---

## 2026-08-25 — Tracing is an interface with three backends, one of which is a file

**Decision:** `loop.py` imports `Tracer` from `agent.tracing` and calls
`tracer.span(name, kind=..., **fields)`. It never imports langfuse. Three
implementations sit behind that interface: `NullTracer` (off), `JsonlTracer` (one
JSON object per span, appended to a local file), and `LangfuseTracer`.
`build_tracer(settings)` is the only place that decides, and `CompositeTracer`
lets more than one run at once.

**Rejected:** (a) calling the Langfuse SDK directly from the loop — then the agent
cannot run without an account, and "observability is part of the demo" becomes "the
demo needs a SaaS signup"; (b) the SDK's `@observe` decorator — it works, but it
puts the tracing vendor's name on every function signature in the call path, and
swapping vendors then means editing the loop.

**Why the file backend exists at all:** it makes tracing *demonstrable offline*. A
reviewer clones the repo, runs one command with `--trace-file`, and sees the span
tree with token usage and cost, with no signup and no network. That is worth more in
a portfolio than a screenshot of someone else's dashboard.

**Tradeoff:** an adapter layer to maintain, and my field names
(`error`, `usage`, `cost`) have to be mapped onto the SDK's (`level` +
`status_message`, `usage_details`, `cost_details`). Paid for by
`test_the_langfuse_adapter_matches_the_real_sdk`, which runs the real SDK against a
dead host — so the mapping is checked against the actual API rather than my memory
of it.

---

## 2026-08-25 — Instrumentation must not change the run

**Decision:** `test_tracing_does_not_change_the_run` executes the same script twice,
once with tracing off and once with a recording tracer, and asserts the two
`AgentRun` objects are equal — excluding only the trace pointer fields, wall-clock
durations, and provider-generated tool_call ids.

**Why:** if instrumentation can change an outcome, every bug report starts with "does
it still happen with tracing off?" and the trace stops being evidence. This is also
the test that would catch the classic mistake of consuming a generator or mutating a
message list while building a span payload.

**Related:** every backend call is wrapped so a tracing failure cannot fail the run.
`LangfuseTracer.span` degrades to a no-op span if the client raises; `JsonlTracer`
swallows `OSError` on write. An observability tool that can take down the thing it
observes is worse than no observability tool.

---

## 2026-08-25 — Span payloads are an allow-list, and prompts are switchable

**Decision:** `TRACED_SETTINGS` names the five configuration fields that may appear in
a span. `settings_metadata()` reads only those. Nothing dumps a settings object, an
environment, or a `**kwargs` into a span.

**Why:** blocklisting secrets means being right every time forever; allow-listing
means being right once. A key added to `AgentSettings` next month cannot leak into a
trace by default — and a test asserts no traced field name contains "key", "secret",
"token", "password" or "credential".

**Separately:** `trace_payloads` (default True) controls whether prompts and tool
results reach spans. They are the most useful thing in a trace and the most likely
place for anything sensitive to appear. The ERP data here is synthetic, so they are
on; the switch exists because that will not always be true, and because the switch is
much easier to add now than after the first customer transcript lands in a
third-party dashboard. Redaction goes through a single `payload()` closure so it
cannot be half-applied.

---

## 2026-08-25 — Cost lives on the run, not in the tracer

**Decision:** `cost.py` computes cost; `AgentRun` carries `llm_calls`
(a `LlmCallRecord` per model call) plus `input_usd` / `output_usd` / `total_usd`.
Tracing is one *consumer* of that, not the owner.

**Rejected:** letting Langfuse compute cost from the model name and token counts,
which it will happily do.

**Why:** cost is a property of the run whether or not anyone is watching. It has to be
in `--json` for the Step 8 eval table, printed by the CLI with no backend configured,
and available in CI where there is no dashboard. Deriving it once and *sending* it to
the tracer also means the number in the terminal and the number in the dashboard
cannot disagree.

**Input and output are kept separate** because output tokens cost several times what
input tokens do — so a run that looks expensive is usually one where the model wrote
too much, not one where it read too much, and the split is the diagnosis.

---

## 2026-08-25 — An unknown model is "unpriced", never zero

**Decision:** `cost_for()` consults LiteLLM's maintained price map first, then a small
pinned `LOCAL_PRICES` table, and returns `UNPRICED` (a `TokenCost` with `None`
fields) if neither knows the model. `total_cost()` of a list containing any unpriced
call is itself unpriced. The CLI prints `unpriced`.

**Rejected:** defaulting to `0.0`. A zero in a cost column reads as *free*, which is a
wrong answer. A blank reads as *we do not know*, which is the true one. Summing the
priced calls and labelling the result "total" is the same mistake one level up: a
partial sum presented as a total is a false number, and a false number is worse than
a missing one.

**On the pinned table:** `LOCAL_PRICES` exists as the escape hatch for models LiteLLM
has never heard of (self-hosted, behind a gateway, newer than the pinned litellm),
and as a **drift detector**. Every entry carries `checked_on` and `source` as
required fields, because a price with no date is indistinguishable from a guess — and
the values currently there were read from LiteLLM's own map, *not* independently
verified against the provider's pricing page.
`test_pinned_prices_have_not_drifted_from_litellm` compares the two exactly and is
deliberately brittle: a pricing change *should* break the build of a system that
reports cost to a human.

---

## 2026-08-25 — The caller flushes, not the loop

**Decision:** `run_agent` never calls `tracer.flush()`. The CLI does, in a `finally`
block; the Step 8 eval harness will do it once at the end.

**Why:** Langfuse batches spans in a background thread, so a process that exits
without flushing silently loses the trace it just paid to produce — which is why the
CLI builds the tracer itself and passes it in, rather than letting `run_agent` build
one it cannot then flush. But flushing *inside* `run_agent` would make an eval over
200 invoices pay a network round trip 200 times. The party that knows when the process
is ending is the party that should flush.

**Tested by** `test_the_tracer_is_flushed_even_when_the_run_raises`.

---

## 2026-08-25 — Span time and provider time are both recorded

**Decision:** `LlmCallRecord.duration_ms` times only the provider call. The
`llm.completion` span additionally carries `provider_ms` in its metadata.

**Why:** the span wraps the completion *and* the bookkeeping around it (usage
accounting, the price lookup, appending the assistant message). Recording only the
span duration silently attributes our overhead to the model — which showed up
immediately: the first span in a run was 3.1 seconds because that is when litellm's
price map gets imported. Span time minus `provider_ms` is our overhead, and having
both numbers is what makes that subtraction possible.

---

## 2026-08-25 — Known gap: cache tokens are not accounted for

**Not a decision, a limitation to state before someone finds it.** `_usage()` reads
only `prompt_tokens` and `completion_tokens`. Anthropic's prompt caching reports
`cache_creation_input_tokens` and `cache_read_input_tokens` separately, priced
differently from ordinary input tokens. This agent re-sends a large system prompt on
every iteration, which is exactly the workload caching is for, so the cost table will
*overstate* input cost once caching is enabled.

Deliberately not fixed in Step 7: the eval suite comes first, and there is no point
optimising a cost number before there is a benchmark to measure the optimisation
against. Recorded here so the number is read with the right caveat.

---

## 2026-09-06 — A Python harness in the repo, not promptfoo

**Decision:** `packages/evals` is a workspace member with its own CLI
(`proc-evals`). The original spec named promptfoo.

**Why the change:** promptfoo grades prompt/response pairs well. This agent is a
stateful multi-turn tool-calling loop, so promptfoo would have driven
`proc-agent --json` as a custom provider and graded the output — meaning the real
work (running the loop, mapping classifications onto labels, aggregating cost,
checking that nothing was written) still happens in Python behind a shell-out,
and CI grows a Node toolchain to reach it.

Doing it natively means the harness reuses what already exists: `AgentRun` as the
unit of measurement, `completion_fn` injection as the seam for baselines and
replay, `LABEL_FOR_CLASSIFICATION` as the scoring bridge, `total_usd` as a
column. One `uv run`, one language, one lockfile.

**Tradeoff:** no web UI, and "promptfoo" is a recognisable line on a CV that
"I wrote the harness" is not. Accepted: the harness is more interesting than the
tool would have been, and Langfuse already provides the UI for individual runs.

---

## 2026-09-06 — Safety hard-fails; accuracy is reported

**Decision:** `proc-evals` exits `1` only when a safety gate fails: an invoice
changed during the run, a proposal reached APPLIED, or a ground-truth string
appeared in something the agent was shown. Accuracy, over-escalation and cost
are printed and written to a report, and never fail the build. Exit `2` is
reserved for the harness being unable to run at all.

**Rejected:** accuracy thresholds from day one.

**Why:** a threshold chosen before there is a baseline tests the person who
chose it. When it goes red you learn that your guess was wrong, and the
reflex — lower the number — teaches you nothing. Safety gates are different in
kind: they are claims that are either true or false, they cost nothing to
check, and the one time they matter is the time nobody was looking.

Three exit codes rather than two because three different people need to hear
about the three outcomes: nobody, the person who broke the gate, and the person
whose cassette went stale.

---

## 2026-09-06 — CI replays; the live model runs on demand

**Decision:** every push runs the rule baseline over all 200 scenarios, plus a
replay of committed cassettes once they exist. A separate manual and weekly
workflow runs a real model against the golden split.

**Rejected:** hitting the API on every push. It costs money per push, it turns
the build red on provider flakiness rather than on the change under review, it
needs a key in secrets that forks cannot have, and it makes non-determinism a
property of CI.

**What each one actually proves:** the replay job proves the *harness* did not
regress — the loop, the tool layer, the OData service, the approval gate, the
scoring. It proves nothing about the model, and the report says so. The weekly
live job is what catches provider drift: a model update that quietly changes
behaviour shows up as a dated report rather than as a surprise during a demo.

---

## 2026-09-06 — Cassettes carry a request fingerprint, and refuse to replay when it changes

**Decision:** each recorded turn stores a SHA-256 of the request that produced
it — messages, tool names and descriptions, model, temperature. On replay the
hash is recomputed and compared, and a mismatch raises rather than replaying.

**Why:** a cassette that happily replays against a changed prompt reports a
green eval for a prompt that was never run. That is a false negative on exactly
the change you most wanted to measure, and it is silent. Failing loudly and
demanding a re-record is the correct cost.

**What is deliberately excluded from the hash:** tool_call ids, because the
provider generates them and they differ between the recording run and the
replay run through no fault of ours — including them would make every cassette
single-use. Registry *order* is excluded too (the names and descriptions sort),
because reordering a dict is not a prompt change.

`--allow-stale` exists for debugging the harness itself and records which turns
mismatched. It must never be used to produce a number.

---

## 2026-09-06 — A rule-based baseline, and what it does NOT prove

**Decision:** `evals/baseline.py` is a deterministic three-way match that plugs
in as `completion_fn`. It reads prior tool results out of the message list
exactly as a model does, emits real tool calls, and scores **100% ideal on all
200 scenarios**.

**Why it earns its place:** CI has something real to run with no API key and no
cassettes; it is the cost and latency floor every model call has to beat (five
iterations, zero tokens, 24 ms); and a perfect score is the strongest available
evidence that the *scoring tables* are correct — if a rule engine cannot score
perfectly against labels a rule engine generated, the bug is in the scoring, not
in the "model".

**What it does not prove, stated before anyone else says it:** it scores 100%
*because the dataset was generated by rules and this encodes the same rules*.
That is a fact about synthetic data, not evidence that AP needs no judgement.
The runner attaches that caveat to the report itself, in `notes`, so the number
cannot travel without it.

The honest framing is the interesting one: here is exactly what a rule engine
already does for free, so here is what the model has to be worth paying for.

---

## 2026-09-06 — Decisions are graded, not scored pass/fail

**Decision:** each case gets a grade of `ideal`, `acceptable` or `wrong`, plus
two separate flags: `over_escalated` and `unsafe_action`.

**Why:** collapsing this to a boolean loses the distinction that matters.
Escalating a PRICE_MAJOR the agent could have corrected costs a human five
minutes and is never unsafe — `acceptable`. Correcting an AMBIGUOUS case the
agent could not possibly have resolved means it invented a figure — `wrong`,
and flagged `unsafe`. Both would have been "incorrect" under a single boolean,
and they need different fixes.

The asymmetry is deliberate and encoded in `ALSO_ACCEPTABLE`: escalation is a
fallback for the hard labels and **not** for CLEAN or PRICE_MINOR, which are
half the queue. An agent allowed to punt on the easy majority scores no wrong
answers and automates nothing.

`GR_MISSING` has no acceptable alternative to escalating at all: nothing was
received, so there is no quantity to amend *to*, and any correction is a
fabrication. That table originally listed PROPOSE_CORRECTION as acceptable for
GR_MISSING while `is_unsafe_action` simultaneously flagged it — a direct
contradiction that produced confident, wrong numbers with nothing failing.
`is_unsafe_action` is now defined *through* `grade_decision` so the two cannot
disagree, and a parametrised test asserts it for every (label, decision) pair.

---

## 2026-09-06 — Corrections are checked for their figures, not just their shape

**Decision:** when the agent proposes a correction, the harness checks the
correction *type* against the label and then the target *value* against the
figure in the label's `detail` block — `to_quantity` against `received_qty`,
`to_price` against `po_price`, `duplicate_of` against the original invoice.
`correction_type_correct` and `correction_value_correct` are reported
separately, and both are `None` when nothing was proposed (distinct from
`False`, which means something wrong was).

**Why:** "proposed a quantity amendment" and "proposed amending 14.000 down to
the 13.000 that actually arrived" are very different outcomes. A correction with
the right type and a fabricated number is the *worst* possible output, because
it looks right to a human skimming an approval queue — which is precisely the
human the gate depends on.

Values compare as `Decimal`, not as strings: `13.0` and `13.000` are the same
quantity and a model may emit either. Comparing text would have made the eval
measure formatting.

---

## 2026-09-06 — The eval package is the only reader of the answer key

**Decision:** `evals/dataset.py` is the sole module that opens
`data/labels/`. A test parses every other `.py` in `packages/` and asserts that
no string literal names the labels directory.

**Why:** this repo has claimed since Step 3 that ground truth is structurally
unreachable from the served data. That claim was previously supported by a leak
test over HTTP responses, which shows nothing *arrived* — this shows nothing can
*reach*. Together they cover both directions.

**Detail worth keeping:** the first version of the test grepped raw file text
and failed on `mock_erp/settings.py`, whose docstring says it has "no code path
pointing at data/labels/" — prose asserting the very property under test. It now
parses the AST and inspects non-docstring string literals only. A test that
punishes you for documenting the rule is a bad test.

---

## 2026-09-06 — The scenario-to-invoice map comes from the generator, not a convention

**Decision:** `load_cases` rebuilds the dataset in memory via
`generator.cli.build_dataset` to learn which invoice belongs to which scenario,
and cross-checks every label against the committed `labels.json`. A disagreement
is a hard error telling you to regenerate.

**Rejected:** deriving the invoice number from the numbering convention
(`51` + zero-padded sequence + line). It works today and would break silently
the day the convention changes — and a harness that quietly targets the wrong
document still prints a confident number.

**Consequence worth naming:** for a `DUP_INVOICE` scenario the harness targets
the **second** invoice. The first one is legitimate; asking the agent to judge it
and rejecting it would be the wrong answer.

---

## 2026-09-06 — golden_ids was empty, and nothing noticed

**Not a decision — a gap found while building the harness.** `splits.py` has
always populated `golden` from `cfg.golden_ids`, and `scenarios.yaml` has always
had `golden_ids: []`. So `data/labels/splits.json` shipped with an empty golden
split for three steps, and the determinism tests passed the whole time because
an empty list is perfectly reproducible.

Now filled with ten hand-picked dev cases covering all eight labels and all
three AMBIGUOUS variants, and `load_cases` refuses an empty split with a message
naming the config key to fix. Regenerating changed `splits.json` and nothing
else, which is itself the evidence that the generator is still deterministic.

The lesson is the one that keeps recurring in this project: **a test that passes
on empty input is not a test.**

---

## 2026-09-06 — The review UI is a separate app, not routes on the mock ERP

**Decision:** `packages/review_ui` is its own FastAPI application. It reaches
documents through the same OData endpoints the agent uses and drives the
approval state machine through the same `/approval` routes a `curl` would hit.

**Rejected:** adding HTML routes to `mock_erp`. It would have been less code and
one fewer process.

**Why:** the mock ERP stands in for SAP, and SAP does not serve your review
screen. Putting them in one process would have made the UI an *insider* — able
to reach the repository directly, able to grow a shortcut past the state
machine, and impossible to point at a different backend later. As a separate
app it is a client with no privileged path, and that is a property a test can
assert rather than a claim: `test_the_review_app_exposes_no_odata_read_path_of_its_own`
enumerates its OpenAPI paths and finds six, none of them a document route.

**Tradeoff:** two processes to run for a demo, and a second HTTP hop on every
page. Both are the honest cost of the separation being real.

---

## 2026-09-06 — A second client, because the agent's must stay unable to approve

**Decision:** `review_ui.client.ReviewClient` duplicates the five read methods
of `agent.erp_client.ErpClient` and adds `approve`, `reject` and `apply`.

**Rejected:** importing `ErpClient` and subclassing it, or adding the three
methods to it behind a flag.

**Why:** "the agent has no way to approve its own proposal" has been the central
claim since Step 5, and it is currently true in the strongest possible sense —
the capability is *absent*, not refused. Adding the human methods to the shared
client would have made it true only by convention, and one careless
`build_tools` edit away from false.

`test_the_agents_client_still_cannot_approve` asserts the two method sets stay
disjoint, and `test_the_agent_has_no_tool_for_anything_the_ui_does` checks the
same thing from the tool registry — which is what the model actually sees.

**Tradeoff:** ~40 duplicated lines of read methods. Cheap insurance on the one
property the project is about.

---

## 2026-09-06 — You approved what you were shown

**Decision:** every action form carries the `payload_hash` that was rendered
into the page. The route re-reads the proposal and refuses with `STALE_PAGE` if
the hash no longer matches, before calling the ERP at all.

**Why:** the ERP already guarantees that *applying* uses the bytes that were
approved (`PAYLOAD_MISMATCH`). Nothing guaranteed that *approving* used the
bytes that were displayed. A reviewer with a tab open from this morning could
approve a payload they never read — the exact failure the gate exists to
prevent, arriving through the human instead of the agent.

The mechanism is deliberately the hash that already exists, not a new one: one
concept, checked at both ends of the human's involvement.

**Related, and separate:** the detail page warns *before* the click when the
effective invoice no longer holds the value the correction was computed from.
The ERP would refuse that apply anyway, but telling someone after they have
approved is a worse experience than telling them before they decide.

---

## 2026-09-06 — Server-rendered, zero JavaScript

**Decision:** Jinja2 templates, plain form POSTs, POST-then-redirect, one
inlined stylesheet. No React, no htmx, no CDN, no npm.

**Rejected:** htmx from a CDN (breaks offline, adds a supply chain to an
approval screen), and a React SPA (adds a build step to a repo whose whole
setup story is `uv sync`).

**Why:** the demo has to run on a laptop with no network, and `uv run` should be
the entire setup. POST-then-redirect means refreshing after approving does not
re-submit, and the back button does not show a stale form — the two ways a
naive form UI double-applies things.

**Tradeoff:** a full page load per action and no live updates. For a queue a
person works through one item at a time, neither costs anything real.

---

## 2026-09-06 — Buttons are rendered from the state machine, not hardcoded

**Decision:** `ACTIONS_FOR_STATUS` maps each proposal status to the actions the
page offers, and a test asserts it equals the complement of `ALLOWED_TRANSITIONS`
in `mock_erp.proposals`.

**Why:** a button the server will refuse is worse than no button — it teaches
the reviewer that the screen lies. Deriving the buttons from a table that is
tested against the ERP's own transition map means the screen and the state
machine cannot drift apart silently.

---

## 2026-09-06 — The evidence is shown unsummarised

**Decision:** the detail page renders the PO line, every goods-receipt document
with its quantity and posting date, the invoice line, the supplier and their
reference, and this supplier's tolerance percentages — side by side, in the
units the documents use.

**Why:** the point of a human gate is that a person can *disagree*. A queue that
shows only "the agent proposes changing 14.000 to 13.000, because it says so"
gives them nothing to disagree with, and a gate nobody can fail is a slower
rubber stamp with worse ergonomics than no gate at all. The reviewer should be
checking the evidence, not checking the agent's summary of it.

**Detail worth keeping:** an invoice with no receipts renders "nothing received"
rather than `0`. They are the same number and very different facts, and the
difference is exactly what separates GR_MISSING from a partial delivery.

---

## 2026-09-06 — There is no authentication, and the page says so

**Decision:** the reviewer identity is a form field pre-filled from
`REVIEW_DEFAULT_REVIEWER`. Every page header reads "signed in as X — not
authenticated".

**Rejected:** a login form backed by a hardcoded password or a cookie that
checks nothing. That would *look* like authentication, which is worse than
having none: a reviewer would trust the `approved_by` field in the audit log,
and it would be worth nothing.

**Where it attaches when it matters:** an auth dependency on the FastAPI app,
resolving to a verified principal, replacing the `reviewer` form field on the
approve route. Two other things have to change with it, and neither is free:
the ERP must stop accepting `approved_by` as a caller-supplied string
(otherwise the gate is bypassable by anyone who can reach the approval router),
and the approval routes need CSRF protection, which plain form POSTs currently
do not have. A localhost demo does not need any of it; a system holding real
money needs all three together, and doing one of them would be theatre.

**This is stated here rather than discovered by a reviewer,** because "I know
exactly what is missing and why I stopped there" is a better answer than a
half-built login screen.

---

## 2026-09-06 — The demo is a program, not a set of instructions

**Decision:** `scripts/demo.py` runs the seven acts in order, at a readable
pace, reading every number live from the running services. CI runs it with
`--no-pause` on every push.

**Rejected:** a written runbook to follow while recording.

**Why:** a recorded walkthrough that fumbles is worse than no video, and the
things that go wrong on camera are the boring ones — a service not started, a
proposal id that moved, a copy-paste that lost a quote. Scripting it makes every
take identical and every figure on screen provably live rather than staged.
Testing it in CI means a broken demo is found on a Tuesday, not while recording.

**Consequence worth naming:** the default mode is the rule baseline, so the demo
needs no API key and is byte-identical every run. `--mode live` exists for the
one take where the real cost curve is worth showing.

---

## 2026-09-06 — The rule baseline now actually raises its proposals

**Bug, found while building the demo.** The baseline concluded
`PROPOSE_CORRECTION` and then went straight to `submit_resolution` — deciding to
propose without ever proposing. The eval scored it as ideal, because the
*decision* was right, and the demo's act 4 found no proposal to show.

Two things were wrong with that beyond the demo. The eval never exercised the
write path through the baseline, so 200 scenarios of "nothing was written" were
weaker evidence than they looked. And "decided to propose" is not the same act
as "raised something a human can approve" — which is exactly the distinction
this project is about.

The baseline now calls `propose_correction` first and submits on the next turn.
The 200-scenario run raises ~56 proposals and still reports zero applied and zero
documents changed, which is a materially stronger result than before.

---

## 2026-09-06 — A cassette records the model's side, not the world's

**Found by the fingerprint doing its job.** Recording a QTY_OVER run and
replaying it against the same ERP fails: the recorded run raised a proposal, the
replay raises a *second* one, so the tool result differs, so the transcript
differs, so the fingerprint no longer matches.

That is correct behaviour, not a false alarm — the run genuinely diverged. But
it is a real limitation and it is now documented and tested
(`test_replaying_a_run_with_side_effects_diverges_and_says_so`) rather than
discovered later by someone whose CI went red.

**The consequence:** replay needs a backend in the state the recording was made
against. CI already starts a fresh ERP per job, so it holds — but that is now a
requirement rather than an accident, and the workflow comment says so.

**Rejected:** recording tool results into the cassette as well, so replay would
not execute them. That would turn the cassette into a mock of the entire ERP,
and the eval would stop testing the integration — which is the only part of this
project worth testing.

---

## 2026-09-06 — One image, two entry points

**Decision:** a single Dockerfile; `command:` selects the ERP or the review UI.
The dataset is baked into the image rather than mounted.

**Why:** the services share every dependency, so separate images would double
build time and registry footprint to save nothing. And the dataset is a pure
function of a seed and a config that both live in version control, so baking it
in makes an image and its data one reproducible artefact — `sap-proc-agent:abc123`
fully determines what the agent will see.

**Tradeoff:** regenerating the data means rebuilding the image. Correct: they
are the same artefact, and being able to change one without the other is the
bug, not the feature.

Multi-stage so the runtime layer carries no build tooling; non-root, read-only
root filesystem, and the approval database on the only writable volume.

---

## 2026-09-06 — The ERP is a StatefulSet with exactly one replica

**Decision:** `deploy/k8s` runs the ERP as a StatefulSet with a
`volumeClaimTemplate` and `replicas: 1`; the review UI is a Deployment with two.

**Why:** the approval database is the audit trail. An `emptyDir` would lose who
approved what on the first reschedule, which would make the central guarantee
unprovable after any node drain. The review UI holds nothing — it is a client of
the ERP — so it scales horizontally and the approval state stays where the state
machine can guard it.

**`replicas: 1` is a constraint, not an oversight.** SQLite is a single-writer
store; a second replica would serve stale approvals. `test_the_erp_is_single_writer`
asserts it so nobody "fixes" it by scaling up. The thing that removes the
constraint is Postgres, not more replicas.

---

## 2026-09-06 — The NetworkPolicy is belt and braces, and says so

**Decision:** the manifests include a NetworkPolicy restricting who may reach
the ERP, and the deploy README states plainly that it is **not** the mechanism
preventing the agent from approving its own proposals.

**Why:** approval and proposal live on the same Service and the same port, so no
network rule can tell them apart. A policy that *looked* like it enforced the
separation would be worse than none — someone would trust it. The separation is
enforced where it actually can be: the agent's tool registry contains no
approval capability, and a test enumerates it.

This is the same instinct as refusing to ship a login box that checks nothing.
Security theatre is more dangerous than a documented gap, because a documented
gap gets fixed.

---

## 2026-09-06 — Deployment manifests are tested, and the test says what it is not

**Decision:** `tests/test_deploy.py` parses every manifest and asserts the
things that survive code review and surface in production: non-root, dropped
capabilities, read-only root filesystem *with a writable /tmp*, resource
requests and limits, both probes with liveness more patient than readiness, no
secret shipped with a value, and compose and Kubernetes agreeing on the entry
points.

**What it explicitly is not:** a green `kubectl apply`. These manifests have
never touched a live cluster, and both the test docstring and the deploy README
say so. Claiming cluster experience I do not have would be the one thing in this
repo that could not survive a follow-up question.

Kept in a top-level `tests/` rather than inside a package, because deployment
belongs to no package.

---

## 2026-09-06 — The SAP mapping document is the differentiator, so it is honest

**Decision:** `docs/sap-mapping.md` maps every mock endpoint to its real
S/4HANA counterpart, includes illustrative CDS and service-definition ABAP, and
carries an explicit list of what breaks on a live system — authorisation
objects, rate limits, multi-line POs, unit-of-measure conversion, currency and
FX, fiscal year as part of the invoice key, pagination over `MSEG`, and change
documents.

**Why it is written that way:** for an SAP-adjacent role this document is the
thing that separates "built a demo" from "understands the system". But the
fastest way to destroy that is one claim that does not survive a follow-up
question. So it opens by stating that the ABAP is illustrative and has not been
activated, and the "what changes" list is longer and more specific than the
"what I built" list.

**The sharpest point in it:** on a real system, authorisation objects would
enforce read-only access for the agent's technical user — which is a *stronger*
guarantee than my tool registry, because it is enforced by the system of record
rather than by my own code. Saying that out loud is better than pretending my
version is the ceiling.
