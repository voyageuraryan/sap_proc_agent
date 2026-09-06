# Implementation guide

How this was built, in the order it was built, and **every problem hit along the
way with what fixed it**.

The bug log in Part B is the part worth reading. Most of those defects did not
crash — they produced confident, plausible, wrong answers, which is the failure
mode that actually costs money in a system like this.

- [Part A — build order](#part-a--build-order)
- [Part B — every issue faced, and the fix](#part-b--every-issue-faced-and-the-fix)
- [Part C — design decisions, with the alternatives rejected](#part-c--design-decisions-with-the-alternatives-rejected)
- [Part D — how to work on it](#part-d--how-to-work-on-it)

---

# Part A — build order

The order is deliberate, and two choices in it are load-bearing:

**The approval gate was built before the agent.** It shapes the data model — a
proposal needs a payload hash, an approver, a status machine — and retrofitting
that onto a working agent means rewriting the agent. Building it first meant the
agent was designed against a gate that already existed.

**Evals were built before the demo was polished.** A demo makes you feel good
about the cases you chose. An eval tells you about the cases you did not.

| # | Step | Delivered | Tests |
| ---: | --- | --- | ---: |
| 1 | Repo skeleton | uv workspace, 4 → 6 packages, pinned Python, LF line endings | — |
| 2 | Domain models | SAP-aliased Pydantic, `Decimal` money, bidirectional aliases | — |
| 3 | Generator | 200 scenarios, 8 labels, byte-reproducible, stratified splits | 8 |
| 4 | Mock ERP reads | OData V2 dialect, per-vendor tolerance at the response boundary | 57 |
| 5 | Approval gate | Allow-list transitions, payload hash, amendment overlay | 90 |
| 6 | Agent loop | Tool calling, terminal-tool structured output, no write capability | 121 |
| 7 | Tracing + cost | Null / JSONL / Langfuse behind one interface, per-call cost table | 121 |
| 8 | Eval harness | Rule baseline, cassette replay, safety gates in CI | 112 |
| 9 | Review UI | Server-rendered approval queue, zero JavaScript | 49 |
| 10 | Case study + demo | Scripted walkthrough, run in CI | — |
| 11 | Deploy | Docker, Kubernetes, SAP mapping | 53 |

## Step-by-step

### 1 · Repo skeleton

`uv init` a virtual workspace root (`package = false`) with member packages
under `packages/`. Pin `.python-version` to 3.13. Add `.gitattributes` with
`* text=auto eol=lf` **before the first commit** — the generator's determinism
tests compare bytes, and a CRLF checkout on Windows fails them for no real
reason.

Workspace members go in the root's dev dependency group with
`[tool.uv.sources] <name> = { workspace = true }`, so a plain `uv sync` installs
everything and `uv run pytest` works with no extra flag.

### 2 · Domain models

`erp_domain` holds SAP-shaped Pydantic models. Field names are readable Python;
the wire uses real SAP names (`EBELN`, `MENGE`, `NETPR`) via `alias=`. `Decimal`
everywhere money or quantity appears — this system exists to decide whether two
amounts match, so binary rounding drift is not an acceptable input.

### 3 · Generator

One seed → 200 scenarios. Build a known-good CLEAN triple, then inject exactly
one defect. The registry `INJECTORS[label]` is keyed by the label so the two
cannot drift apart. Determinism rules: no `datetime.now()`, no `uuid4()`, no set
iteration, no float formatting in anything reaching disk; all dates derive from
`epoch_date`.

### 4 · Mock ERP reads

`APIRouter(prefix="/sap/opu/odata/sap/ZPROC_SRV")` serving an OData V2 dialect:
`{"d": {...}}` envelopes, `{"d": {"results": [...]}}` collections, and errors as
`{"error": {"code": ..., "message": {"lang": "en", "value": ...}}}`. Unsupported
query options are **rejected explicitly** — silently ignoring an option a caller
sent is how an integration produces confidently wrong results.

### 5 · Approval gate

An allow-list of three `(status, action) → status` transitions; every other pair
refused. Canonical JSON + SHA-256 payload hash. Applying inserts an amendment
row; reads compose base + amendments, so a write is *observable* rather than
asserted.

### 6 · Agent loop

Six tools, an iteration cap, and `Resolution` registered as the terminal tool's
argument schema. `completion_fn` is injectable so tests can drive a scripted
model. `_execute_tool_call` never raises.

### 7 · Tracing and cost

A `Tracer` interface with Null / JSONL / Langfuse behind it, so `loop.py` never
imports langfuse and the demo runs offline. Cost lands on `AgentRun`, not in the
tracer.

### 8 · Eval harness

`packages/evals` — the only module that reads `data/labels/`. Four modes, graded
decisions, safety gates that fail the build.

### 9 · Review UI

A separate FastAPI app, Jinja2, zero JavaScript, POST-then-redirect.

### 10–11 · Demo and deploy

`scripts/demo.py` runs seven acts and is executed by CI. One Docker image with
two entry points; Kubernetes manifests validated structurally by `tests/`.

---

# Part B — every issue faced, and the fix

Grouped by the kind of mistake, because the *kind* is what generalises.

## B1 — Bugs that did not crash

These are the expensive ones. Every entry here produced a plausible wrong
answer with nothing failing anywhere.

### A duplicated enum value silently deleted two classifications

```python
VALID_PARTIAL_DELIVERY = "VALID_PARTIAL_DELIVERY"
DUPLICATE_INVOICE = "VALID_PARTIAL_DELIVERY"  # copy-paste
INSUFFICIENT_EVIDENCE = "VALID_PARTIAL_DELIVERY"  # copy-paste
```

Python turns a duplicate value into an **alias**, not a member. Both vanished
from `list(Classification)` and from the JSON schema, so the model could never
emit `DUPLICATE_INVOICE` — and the eval would have scored every duplicate
scenario against the wrong class while printing a believable number.

**Fix:** `test_no_classification_value_is_an_accidental_alias` compares
`len(list(Classification))` against `len(Classification.__members__)`.
**Lesson:** a wrong answer that does not crash costs more than a crash.

### `golden_ids: []` — a split that shipped empty for three steps

`splits.py` populated `golden` from config; the config had an empty list. Every
determinism test passed the whole time, because an empty list is perfectly
reproducible.

**Fix:** ten hand-picked cases covering all eight labels and all three
`AMBIGUOUS` variants; `load_cases` now refuses an empty split and names the
config key to fix. **Lesson:** a test that passes on empty input is not a test.

### The models could not parse their own output

`Vendor(**{"LIFNR": ...})` raised `Field required: vendor_id`. Only
`serialization_alias` was set, so models could write SAP-shaped JSON and not read
it back.

**Fix:** `alias=` on all 16 type aliases plus a `SapModel` base carrying
`populate_by_name=True`. Confirmed behaviour-preserving by `git diff --stat data/`
being empty after regeneration.

### A contradiction inside the scoring tables

`GR_MISSING` listed `PROPOSE_CORRECTION` as acceptable while `is_unsafe_action`
simultaneously flagged it as unsafe. Two functions encoding one rule, disagreeing.

**Fix:** `is_unsafe_action` is now defined *through* `grade_decision`, so they
cannot disagree, plus a parametrised test over all 32 (label, decision) pairs.
**Lesson:** derive one from the other; do not write the rule twice.

### The rule baseline decided to propose without ever proposing

It concluded `PROPOSE_CORRECTION` and went straight to `submit_resolution`. The
eval scored it *ideal*, because the decision was right — but the write path was
never exercised, so "200 scenarios and nothing was written" was weaker evidence
than it looked.

**Fix:** the baseline now calls `propose_correction` first. A 200-scenario run
raises 59 proposals and still changes zero documents.

### The safety gate fired on corrections applied *before* the run

Running the demo (which legitimately approves and applies one correction) and
then the eval suite against the same database turned the eval red:
`proposal PR-000001 reached APPLIED without a human approval`. It had been
approved by a human — just not during that run.

The gate read the APPLIED set **once, at the end**. Against a long-lived ERP —
exactly the nightly `CronJob` in `deploy/k8s` — every approved correction in
history would have counted as a breach, and the job would have been permanently
red.

**Fix:** snapshot the APPLIED ids before *and* after and report only the
difference, the same before/after pattern already used for invoices. Two tests:
one that a pre-existing applied correction is not reported, and one that the
gate still fires for a correction applied *during* the run — because a fix that
defangs the gate is worse than the bug.
**Lesson:** a gate that is always red is a gate everyone learns to ignore.

### `block_reason` was byte-identical to a ground-truth label

The mock served `"GR_MISSING"`, which is exactly what the answer key said. The
leak test caught it.

**Fix:** SAP records that a *check* failed, not your taxonomy. Renamed to
`PRICE_VARIANCE` / `QUANTITY_VARIANCE` / `MISSING_GR`.

## B2 — Security defects

### String-interpolated SQL in the approval repository

```python
f"... WHERE proposal_id = {proposal_id}"  # proposal_id from a request body
```

A prompt-injected agent sending `x'; UPDATE proposals SET status='APPROVED'; --`
could have approved its own proposal. **The entire gate defeated by one f-string.**

**Fix:** `?` parameter binding everywhere. **Lesson:** a security boundary is
only as strong as the least careful line inside it.

### `apply` compared against the wrong hash

It checked `payload_hash` (the proposal's own) instead of `approved_hash` (what
the human signed off). A tautology that could never fail — the hash check was
decorative.

**Fix:** compare against `approved_hash`. Tested by swapping `to_quantity` after
approval and asserting `409 PAYLOAD_MISMATCH`.

### `approve`/`reject` bypassed the transition check

They compared `!= "APPROVED"` where the guard should have been `PROPOSED`, so
nothing could ever legally be approved and the allow-list was unreachable.

**Fix:** route both through `_transition`, and derive the illegal-transition test
from the complement of `ALLOWED_TRANSITIONS`.

### The SAP alias leak into the write API

`AmendQuantityPayload(invoice_number=...)` demanded `BELNR` and `BUZEI`;
`POST /ProposeCorrection` returned 422 with `loc: ["body", "BELNR"]`. Internal
SAP field names had leaked into a public request body.

**Fix:** a `PayloadBase` with `populate_by_name=True` on every payload model.

## B3 — Correctness bugs that did crash

| Where | What | Fix |
| --- | --- | --- |
| `models.py` | `pattern=f"^[A-Z]{3}$"` — an f-string, not a raw string, compiled to `^[A-Z]3$` and rejected `"USD"` | `r"..."` |
| `tolerances.py` | `by_vendor : {str, ToleranceSet}` is a **set literal**, not a dict annotation | `dict[str, ToleranceSet]` |
| `planner.py` | imported `load_config` from `cli.py` — a hard import cycle | removed |
| `odata.py` | three stacked bugs in `parse_po_filter`: a shape regex matching a literal `'45'`, a key check applied to the whole filter string, and `str(match)` returning `<re.Match object; ...>` | `.group(1)` and a corrected regex |
| `api.py` | **no `@router.get(...)` decorators at all** — every endpoint 404 | added |
| `app.py` | `create_app(settings)` ignored its argument; the lifespan called `get_settings()` | closure over the passed settings |
| `app.py` | two `/healthz` routes, the stub shadowing the real one | removed the stub |
| `api.py` | `store.grs_by_po[po]` → `KeyError` for the 16 `GR_MISSING` POs | `.get(po, [])` — absent must not mean empty |
| `db.py` | `CRETATE TABLE` — and because `executescript` aborts on error, **neither** table was created | spelling |
| `repository.py` | INSERT with 8 values for 9 columns, then values shifted so `invoice_number` landed in `status` | aligned |
| `repository.py` | `str(canonical_json(payload))` produced the literal text `b'{"..."}'` | `.decode("utf-8")` |
| `repository.py` | `WHERE status = ?` bound to `None` returns zero rows — `NULL = NULL` is unknown, not true | conditional WHERE |
| `repository.py` | `applied_at` never persisted, so the idempotent replay path re-read `None` | persisted |
| `repository.py` | `Ellipsis` literals reaching SQL bindings → `type 'ellipsis' is not supported` | removed |
| `api.py` | `VendorID` absent → `None.strip()` → 500 with a non-OData body, which then broke the client's error parsing | explicit `400 MISSING_PARAMETER` |

## B4 — The agent package, first pass

Written before the tool-calling contract was fully internalised. Every one of
these was a real defect:

| Issue | Why it mattered |
| --- | --- |
| `fn: callable[[BaseModel], object]` | `callable` is a *builtin function*, not a type — `TypeError` at import. The whole package could not be imported |
| `def build_tools(self, client)` | A module-level function with a `self` parameter; the client landed in `self` |
| Terminal tool declared but never built | `TERMINAL_TOOL` was a constant with nothing behind it, so the model was never *offered* it — the run could only ever end in `MAX_ITERATIONS` |
| `to_openai_schemas(specs)` given a dict | Iterating a dict yields **keys**; `spec.name` on a string |
| `propose_correction` used `GetPurchaseOrderArgs` | The model was told a correction takes a `po_number` |
| `json.dumps(tool_call.function.arguments)` | Backwards — `arguments` is already a JSON *string*; needed `loads` |
| `return json.dump(result)` | `json.dump` writes to a file and returns `None` |
| `if tc.function.name == "TERMINAL_TOOL"` | Compared against the literal string, not the constant's value |
| `os.getenv("MODEL_NAME")` | Bypassed `AgentSettings`; unset meant `model=None` |
| No `except ErpError` | A 404 propagated out and killed the process instead of becoming a message the model could react to |
| `messages = ["system", user_prompt(...)]` | A list of two bare strings where the API needs dicts |
| `cli.py` | A verbatim copy of the generator's `main()` — it generated a dataset instead of running an agent |

**The pattern behind all of them:** the tool-calling protocol has exactly one
invariant — every `tool_call` must be answered by exactly one `tool` message with
the same `tool_call_id` — and most of these were ways to violate it.

## B5 — Dependency and environment

| Issue | Fix |
| --- | --- |
| `httpx2>=2.12.0` in the root pyproject — wrong package (meant `httpx`) and wrong place (runtime dep of a virtual root). Proved the HTTP test suites had never been run locally | moved `httpx` to the dev group |
| `mock-erp` did not declare `pydantic` despite importing it | declared |
| The `agent` package declared **no dependencies at all** while importing five | declared all five |
| `[project.scripts] agent = "agent:main"` pointed at the uv template stub that prints `Hello from agent!` | `agent.cli:_entrypoint` |
| A plain `uv sync` on a virtual workspace root installs **zero** members, so `import agent` failed | members added to the root dev group with `[tool.uv.sources]` |
| `data/approvals.sqlite3` was committed before `.gitignore` covered it — and gitignore does not apply to tracked files, so every local run showed a modified binary | `git rm --cached` |
| `.gitignore` had `.sqlite3`, which matches a file *named* `.sqlite3`, not `approvals.sqlite3` | `*.sqlite3` |

## B6 — Test defects (mine)

A test that is wrong is worse than a missing test, because it reports safety.

| Issue | Fix |
| --- | --- |
| `test_determinism.py` fixture returned a raw dict where `write_dataset` expected `GeneratorConfig` | call `load_config` |
| `"detail"` in `FORBIDDEN_KEYS` false-positived on FastAPI's own `{"detail": "Not Found"}` | removed it and asserted `status_code == 200` instead |
| `app.routes` returned `_IncludedRouter` wrappers with no `.path` | read `app.openapi()["paths"]` |
| A test expected `PAYLOAD_MISMATCH` where status-first ordering correctly yields `ILLEGAL_TRANSITION` | corrected the expectation, not the code |
| The label-isolation test grepped raw file text and failed on a docstring that *says* the module has no path to the labels — prose asserting the property under test | parse the AST; inspect non-docstring string literals only |
| A CLI fixture stubbed only `get_invoice`, so the agent's other tools failed silently and the eval scored a crippled run | inject a real `ErpClient` wired to the in-process app |
| A test asserted an error message on a URL that had dropped the query string carrying it | follow the redirect |

## B7 — Environment friction worth recording

**Every git command through the file bridge left a stale `.git/*.lock`** that
could not be deleted. Worked around by moving locks aside between commands.

**`git add --renormalize .` aborted** with `unable to stat ... tolerances.py`
because of an unstaged deletion; `git add -A packages/` first, after which git
recorded it as an `R100` rename.

**The mount refused to overwrite files in place**, so file transfers moved the
original aside first and copied fresh.

---

# Part C — design decisions, with the alternatives rejected

Condensed. Each entry is: the decision, what was rejected, and the reason.

## C1 — Foundations

| Decision | Rejected | Why |
| --- | --- | --- |
| **uv** for environments and locking | pip + venv, Poetry | One tool for Python versions, resolution, locking and running. `uv.lock` is the reproducibility guarantee |
| **Monorepo, uv workspace** | separate repos per service | The contract between agent and ERP is the interesting part; splitting it across repos hides it |
| **Python 3.13, not 3.14** | latest | 3.14 was too new for the ecosystem this leans on |
| **The mock ERP is a separate service** | an in-process module | With an in-process module there is no integration to point at, and "I built the integration-hard version" becomes a claim rather than a demonstration |
| **Synthetic data, not a public SAP sandbox** | SAP ES5 | No licence, sales-order shaped, intermittent — and decisively, you cannot inject labelled defects. A demo that breaks when someone else's free sandbox goes down is not a demo |
| **Generated data committed to git** | generate on demand | A reviewer clones and runs; and a diff on `data/` is how you notice determinism broke |
| **Config is a validated object** | a dict | A typo'd YAML key fails at load with a clear message instead of being silently ignored |
| **`Decimal`, never `float`** | float | The system exists to decide whether two amounts match |
| **Document numbers derive from the index, not the rng** | draw them | Inserting a random draw later would otherwise renumber every document in the dataset |
| **All file writes pin encoding and newline** | defaults | Text mode on Windows uses the locale encoding and writes CRLF — the same seed would produce different bytes locally and in CI |

## C2 — The gate

| Decision | Rejected | Why |
| --- | --- | --- |
| **Agent gets `propose_correction`, not `apply`** | apply + rely on 409s | Within one run there is no approval, so apply could only ever fail. "The agent cannot write" is far stronger when the capability is *absent* |
| **A second HTTP client for the review UI** | extend `ErpClient` | Adding approve/reject/apply to the shared client would make the guarantee true only by convention |
| **Review UI is a separate app** | routes on `mock_erp` | SAP does not serve your review screen. One process would make the UI an insider |
| **Amendment overlay** | mutate the served JSON | A write must be *observable* on the same URL, so "nothing happened" is checkable |
| **Payload hash passed as an argument to apply** | trust the stored payload | Approving *a* payload is not approving *any* payload |
| **`STALE_PAGE`: forms carry the rendered hash** | trust the click | The ERP guaranteed applying used approved bytes; nothing guaranteed approving used *displayed* bytes |
| **Buttons rendered from the state machine** | hardcode them | A button the server will refuse teaches the reviewer that the screen lies |
| **Evidence shown unsummarised** | show the agent's summary | The point of a human gate is that a person can *disagree* |
| **No authentication, and the page says so** | a login box backed by nothing | Worse than none: `approved_by` would then look trustworthy. SSO needs three things together — a verified principal, the ERP refusing caller-supplied `approved_by`, and CSRF. Doing one is theatre |
| **SQLite, not Postgres** | Postgres | Single file, no container, fine at this scale. The constraint it imposes (single writer) is asserted in the k8s manifests so nobody "fixes" it by scaling up |

## C3 — The agent

| Decision | Rejected | Why |
| --- | --- | --- |
| **Agent does not import `erp_domain`** | share the models | Otherwise "it integrates" is a fact about a Python import. Cost paid by `test_contract.py`, which is the only place that imports both to *compare* |
| **`Resolution` registered as the terminal tool's schema** | ask for JSON in the prompt; `response_format` | The model cannot finish except by filling in the contract; a validation failure is fed back and it retries |
| **Tool failures become message content, never exceptions** | let them propagate | The protocol invariant requires every `tool_call` be answered. An `ERP error` in the transcript is *evidence* |
| **Classifications are the agent's vocabulary** | emit the generator's labels | Scoring becomes an explicit reviewable table, not string equality by accident |
| **`scenario_id` closed over, not a tool argument** | ask the model | The model cannot know it, so asking invites a hallucination that mislabels the eval row |
| **One nudge before `NO_TOOL_CALL`** | stop immediately; nudge forever | A model that opens with prose should not kill the run; `NO_TOOL_CALL` now means "asked twice" |
| **Scripted model, real ERP, in tests** | mock both | Faking the ERP tests our *idea* of the contract — which is how the alias leak survived |

## C4 — Observability

| Decision | Rejected | Why |
| --- | --- | --- |
| **`Tracer` interface, three backends** | call the Langfuse SDK from the loop; `@observe` | Otherwise the demo needs a SaaS signup, and the vendor's name ends up on every signature in the call path |
| **A local JSONL backend** | Langfuse only | Makes tracing demonstrable offline on a clone — worth more than a screenshot of someone else's dashboard |
| **Cost lives on `AgentRun`** | let Langfuse derive it | Cost is a property of the run whether anyone is watching. It must be in `--json`, in CI, and in the terminal — and the two numbers cannot then disagree |
| **Unknown model → `unpriced`, never `0.0`** | default to zero | A zero in a cost column reads as *free*. A partial sum labelled "total" is the same lie one level up |
| **Instrumentation must not change the run** | assume it | Otherwise every bug report starts with "does it still happen with tracing off?" |
| **Span payloads from an allow-list** | blocklist secrets | Blocklisting means being right forever; allow-listing means being right once |
| **The caller flushes, not the loop** | flush per run | An eval over 200 invoices would pay 200 network round trips |

## C5 — Evaluation

| Decision | Rejected | Why |
| --- | --- | --- |
| **A Python harness, not promptfoo** | promptfoo | The agent is a stateful multi-turn loop; promptfoo would shell out to `--json` and the real work would stay in Python anyway, plus a Node toolchain in CI |
| **Safety hard-fails; accuracy reports** | thresholds from day one | A threshold picked before a baseline tests your guess. Safety gates test a claim that is true or false |
| **CI replays; live runs on demand** | hit the API every push | Costs money per push and turns provider flakiness into a red build |
| **Cassettes carry a request fingerprint** | replay blindly | A cassette that replays against a changed prompt reports green for a prompt that was never run |
| **A rule-based baseline** | no baseline | CI gets something real to run with no key; it is the cost floor; and 100% is the strongest evidence the *scoring tables* are right |
| **Decisions graded, not pass/fail** | one boolean | "Escalated when it could decide" and "decided when it should escalate" need different fixes |
| **Corrections checked for figures** | check the shape | Right type with a fabricated number is the worst output — it looks right to the human the gate depends on |
| **Evals is the only reader of the labels** | trust convention | A test parses every other module's AST |
| **Scenario→invoice from the generator** | derive from the numbering convention | It works today and breaks silently the day the convention changes |

## C6 — Known gaps, stated rather than hidden

| Gap | Why it is still open |
| --- | --- |
| **Prompt-cache tokens unaccounted** | `_usage()` reads only prompt/completion. Anthropic prices cache creation and cache read separately, so the cost table *overstates* input cost once caching is on. Not fixed before there was a benchmark to measure the fix against |
| **No authentication** | Needs three changes together (see C2). One of them alone is theatre |
| **One PO line per scenario** | The array shape is right, so multi-line is a generator change not a schema migration — but the eval set does not yet exercise an invoice billing two lines with different variances |
| **Replay needs a matching backend state** | A cassette records the model's side, not the world's. CI starts a fresh ERP per job; that is now a requirement, not an accident |
| **The mock is cleaner than production SAP** | Mitigated by seeding adversarial cases, and stated openly rather than left to be discovered |
| **The manifests have never been applied** | Validated structurally by tests, which is not the same as `kubectl apply`, and the test says so |

---

# Part D — how to work on it

```bash
make setup          # uv sync --locked, create .env
make check          # ruff check + format check + the full suite. Run before every commit.
make erp            # the ERP on :8000
make ui             # the review queue on :8001
make demo           # the four-minute walkthrough
make evals          # score 200 scenarios; exits non-zero on a safety failure
```

Windows: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`, then the
plain `uv run ...` lines in the README.

## The rules this codebase holds itself to

1. **Warnings are errors.** `filterwarnings = ["error", ...]` in `pyproject.toml`.
   A `DeprecationWarning` is a bug with a grace period, and the grace period
   always ends on a day nobody chose.
2. **Every broad `except` carries a `# noqa: BLE001` and a reason.** `BLE` is in
   the lint select set precisely to force that.
3. **The dataset is a pure function of `(config, seed)`.** CI regenerates and
   runs `git diff --exit-code -- data/`.
4. **Nothing outside `evals` may name `data/labels/`.** Enforced by an AST test.
5. **A new correction type cannot be added without deciding how a human checks
   it** — `DIFF_SPEC` and `HEADER_SPEC` are asserted to cover the enum.
6. **`ruff format` is the formatter**, checked in CI. No debates.

## If you change the prompt

Cassettes are fingerprinted on the messages, tool names and descriptions, model
and temperature. Changing any of them invalidates every recording — deliberately.
Re-record:

```bash
uv run proc-evals --split golden --mode record
```

## If you add a label

1. `generator/labels.py` — the enum
2. `generator/injectors.py` — the injector; the registry key *is* the label
3. `data/config/scenarios.yaml` — the distribution (must still sum to 1.0)
4. `agent/schemas.py` — a `Classification` and its entry in `LABEL_FOR_CLASSIFICATION`
5. `evals/expectations.py` — ideal decision, acceptable set, expected correction
6. Regenerate, then run `make check`

Five of those six will fail loudly if you skip them. That is on purpose.
