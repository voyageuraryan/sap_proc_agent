<div align="center">

# SAP Procurement Exception Agent

**An AI agent that works a queue of blocked supplier invoices end to end —
and *cannot* change a document without a named human approving the exact bytes
it proposed.**

[![tests](https://img.shields.io/badge/tests-433%20passing-2f5d50)](#testing)
[![lint](https://img.shields.io/badge/ruff-clean-2f5d50)](#testing)
[![warnings](https://img.shields.io/badge/warnings-0-2f5d50)](#testing)
[![python](https://img.shields.io/badge/python-3.13-3776ab)](pyproject.toml)
[![uv](https://img.shields.io/badge/packaging-uv-de5fe9)](https://docs.astral.sh/uv/)
[![licence](https://img.shields.io/badge/licence-MIT-6b6b68)](LICENSE)

*The model is close to the least interesting component here. That is the point.*

[Quick start](#quick-start) ·
[What it does](#what-it-does) ·
[The guarantee](#the-guarantee) ·
[Results](#results) ·
[Documentation](#documentation)

</div>

---

## Why this exists

The widely-cited figure is that around **95% of enterprise AI pilots never reach
production**. Read the post-mortems and the cause is rarely the model. It is
integration, permissions, auditability, and the fact that nobody in finance will
let a language model touch a ledger on its own recognisance.

So this project deliberately optimises for the parts that actually kill pilots.

| The easy version | What was built | Why |
| --- | --- | --- |
| Agent calls Python functions | Agent speaks **HTTP to an OData V2 service** | If the agent imports the ERP's models, "it integrates" is a fact about a Python import |
| Agent writes, with a confirm prompt | Agent has **no write tool at all** | Absent beats refused — and a test enumerates the registry |
| "Trust the model" | **Payload hash** approved by a named human | Approving *a* payload is not approving *any* payload |
| Print the answer | **Typed `Resolution`**, enforced by the tool schema | Code cannot branch on prose; evals cannot score it |
| Eyeball a few cases | **200 labelled scenarios**, stratified holdout | Including 36 built specifically to trap it |
| Demo, then evals | **Evals and the gate *before* the agent** | The gate shapes the data model; retrofitting it does not work |

---

## What it does

An Accounts Payable clerk faces a queue of blocked invoices and performs a
**three-way match**: purchase order (ordered) → goods receipt (arrived) →
invoice (billed).

The hard part is not arithmetic. **Two invoices can look almost identical and
mean opposite things:**

<table>
<tr><th align="left">Invoice A — <code>5100000901</code></th><th align="left">Invoice B — <code>5100001801</code></th></tr>
<tr><td>

```
ordered   14.000
received  13.000
billed    14.000
```
**Over-billed by 1.000.**
7.7% over a 5% tolerance.
→ propose a correction

</td><td>

```
ordered   13.000
received   1.000
billed     1.000
```
**A valid partial delivery.**
Nothing is wrong.
→ post it

</td></tr>
</table>

Both were short-delivered. Only one is over-billed. An agent that proposes a
correction on B is worse than useless — it creates work and burns the trust the
whole thing depends on. **The eval set contains 20 of A and 16 of B, and
separating them is the headline metric.**

---

## The guarantee

> ### No document changes without a named human approving the exact payload shown to them.

Four independent enforcements, each asserted by a test:

```
   agent ──POST /ProposeCorrection──►  PROPOSED   ·  payload_hash stored
                                            │          ▲ the agent's tool list has
                                            │            no approve and no apply
   human ──POST /approval/…/approve──►  APPROVED   ·  approved_hash = payload_hash
                                            │          ▲ the UI form carries the hash
                                            │            it rendered → STALE_PAGE
         ──POST /ApplyCorrection──────►  1. status == APPROVED?      else 409
                                         2. sha256(sent) == approved? else 409
                                         3. document still unmoved?   else 409
                                            │
                                          APPLIED  ·  amendment row inserted
```

Applying does **not** mutate the JSON on disk — it inserts an amendment row, and
reads compose base + amendments. So *"no write happened"* is something you check
by reading the same URL the agent read from, not something the README asserts.
The eval harness snapshots every invoice before a 200-scenario run and diffs the
bytes after.

Verified end to end over real sockets:

```
 0. invoice quantity before anything            : 14.000
 1. agent ran (SUBMITTED, 6 tool calls)
 2. invoice after the agent finished            : 14.000   ← wrote nothing
 3. apply without approval                      : 409 ILLEGAL_TRANSITION
 4. approve from a stale page                   : STALE_PAGE
 5. human approves
 6. invoice after approval                      : 14.000   ← approval ≠ application
 7. apply a payload nobody approved             : 409 PAYLOAD_MISMATCH
 8. apply the approved payload
 9. invoice now                                 : 13.000   ← the only write
```

---

## Quick start

**Requirements:** [uv](https://docs.astral.sh/uv/). Python 3.13 is installed by
uv itself. No API key is needed for the demo, the tests, or the eval suite.

```bash
git clone https://github.com/voyageuraryan/sap_proc_agent.git
cd sap_proc_agent
```

<table>
<tr><th align="left">Linux / macOS</th><th align="left">Windows</th></tr>
<tr valign="top"><td>

```bash
./scripts/setup.sh
```

</td><td>

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

</td></tr>
</table>

Then, in three terminals:

```bash
uv run uvicorn mock_erp.app:app --reload --port 8000     # the ERP
uv run uvicorn review_ui.app:app --reload --port 8001    # the review queue
uv run python scripts/demo.py                            # the walkthrough
```

Open **http://localhost:8001** for the reviewer's screen.

<details>
<summary><b>Or with containers</b></summary>

```bash
docker compose up --build          # ERP on :8000, review UI on :8001
docker compose run --rm evals      # 200 scenarios; non-zero on a safety failure
kubectl apply -k deploy/k8s        # manifests, validated by tests/test_deploy.py
```

</details>

<details>
<summary><b>Every command (Makefile)</b></summary>

```bash
make setup    # environment from uv.lock, plus .env
make check    # ruff + format check + the full suite. Run before every commit.
make erp      # the ERP on :8000
make ui       # the review queue on :8001
make demo     # the four-minute walkthrough
make evals    # score all 200 scenarios; exits non-zero on a safety failure
make data     # regenerate the dataset (byte-identical to what is committed)
make clean    # caches, local state, generated reports
```

</details>

---

## The demo

`scripts/demo.py` runs **seven acts** — the queue, the trap, the agent, the
wall, the human, the cost, the score. It is scripted so it is identical every
run, every number on screen is read live from the running services, and **CI
executes it on every push** so a broken demo is found on a Tuesday rather than
while recording.

```
  4. The wall                                   5. The human
     The agent raised a proposal.                  http://localhost:8001/proposals/PR-000001

  invoice quantity right now      14.000        Approving from a page that is out of date:
  proposal PR-000001  PROPOSED                    STALE_PAGE — you approve what you were shown
  payload hash 6b6a1441e53f1149…
                                                approved by ap.supervisor@example.com
  Trying to apply without approval:               quantity 14.000 — approval is not application
    409 ILLEGAL_TRANSITION
    quantity still 14.000                       Now applying a payload nobody approved:
                                                  409 PAYLOAD_MISMATCH
  The agent's tool list contains no way            quantity still 14.000
  to approve or apply. Not refused at            applied
  runtime — absent from the schema.                quantity 13.000 — the first and only write
```

`--mode live` uses a real model; the default needs no API key.

---

## Results

```
$ uv run proc-evals --split all --mode baseline

safety     PASS

accuracy
  submitted            100.0%      over-escalation        0.0%
  decision (ideal)     100.0%      unsafe actions           0
  classification       100.0%      correction precision 100.0%

  label          n  ideal   ok wrong  decision  unsafe
  CLEAN         80     80    0     0    100.0%       0
  PRICE_MINOR   20     20    0     0    100.0%       0
  PRICE_MAJOR   24     24    0     0    100.0%       0
  QTY_OVER      20     20    0     0    100.0%       0
  GR_MISSING    16     16    0     0    100.0%       0
  GR_PARTIAL    16     16    0     0    100.0%       0
  DUP_INVOICE   12     12    0     0    100.0%       0
  AMBIGUOUS     12     12    0     0    100.0%       0
```

> [!IMPORTANT]
> **That 100% is a rule engine, not a model — and it means less than it looks
> like.** The dataset was generated by rules and the baseline encodes the same
> rules. It is a fact about synthetic data, not evidence that AP needs no
> judgement.
>
> What it does buy: CI gets something real to run with no API key; it is the
> **cost and latency floor** (6 tool calls, 0 tokens, ~26 ms); and a perfect
> score is the strongest available evidence that the **scoring tables** are
> right. If a rule engine cannot score perfectly against labels a rule engine
> produced, the bug is in the scoring.
>
> The useful question it frames: *what does the model have to be worth paying
> for?* On this dataset — duplicate detection, and knowing when to decline.

Across that run the baseline raises **59 correction proposals and changes zero
documents**, which is a stronger statement of the guarantee than a read-only run
could ever be.

### Cost, and the shape that matters

```
   #       in    out       ms  tools          usd
   1     4200     95      812      1    $0.014025
   2     5100     88      904      1    $0.016620
   3     6050     91      770      1    $0.019515
        15350    274                    $0.050160
```

Input tokens grow every turn because the whole transcript is re-sent — cost is
roughly **quadratic in tool calls**, and ~90% of it is input. That is the number
to quote at 10,000 invoices a month, and it says the lever is fewer round trips
and shorter tool results, **not a cheaper model**.

---

## Architecture

Six packages. The boundaries *are* the design — most guarantees are consequences
of who is allowed to import what.

```
packages/
├── erp_domain/   SAP-shaped models, shared by the generator and the ERP
├── generator/    200 labelled scenarios from one seed, byte-reproducible
├── mock_erp/     OData V2 read service + the approval state machine
├── agent/        the tool-calling loop — talks HTTP, imports none of the above
├── evals/        scoring + safety gates — the only reader of data/labels/
└── review_ui/    the human queue — a client of the ERP, not part of it
```

| Fact | Consequence |
| --- | --- |
| `agent` does **not** import `erp_domain` | It sees dicts that came over HTTP, exactly as a third party would |
| Only `evals` reads `data/labels/` | A test parses every other module's AST and asserts no string literal names it |
| `review_ui` is a **separate app** | SAP does not serve your review screen; the UI holds no privileged path |

Full diagrams — component, sequence, state machine, decision flow —
in **[docs/03-architecture.md](docs/03-architecture.md)**.

---

## Testing

```bash
uv run pytest -q       # 433 passed, 0 warnings
uv run ruff check .    # clean, under a curated strict rule set incl. S (bandit) and BLE
make check             # exactly what CI runs
```

**Warnings are errors** (`filterwarnings = ["error", …]`). A `DeprecationWarning`
is a bug with a grace period, and the grace period always ends on a day nobody
chose.

| Suite | Tests | What it protects |
| --- | ---: | --- |
| `generator` | 8 | Byte-reproducibility, hash-seed independence, taxonomy shape |
| `mock_erp` | 90 | OData dialect, the full illegal-transition matrix, three attacks on the gate |
| `agent` | 121 | Loop protocol, structured output, tracing-changes-nothing, cost honesty |
| `evals` | 112 | Scoring tables, cassette fingerprints, label isolation |
| `review_ui` | 49 | The gate from the UI side, stale-page refusal, separation of powers |
| `tests/` | 53 | Deployment manifests and the documentation set itself |

CI additionally regenerates the dataset and runs `git diff --exit-code -- data/`,
starts both services, runs the demo, and scores all 200 scenarios.

---

## Documentation

| Document | What it is for |
| --- | --- |
| **[01 — Functional specification](docs/01-functional-spec.md)** | The problem, the proposed solution, and how it was delivered. One page. |
| **[02 — Implementation guide](docs/02-implementation-guide.md)** | Build order, **every issue faced and the fix**, and every design decision with the alternative rejected. |
| **[03 — Architecture and scenarios](docs/03-architecture.md)** | 14 diagrams: components, sequences, the state machine, the eight scenarios, deployment, and what the mock stands in for in real S/4HANA. |

---

## What makes this different

Most agent portfolios show a model calling a function. This one is built around
the things that decide whether such a system ever ships.

1. **The agent physically cannot write.** Not "is refused" — the capability is
   absent from the schema the model reads, absent from its HTTP client, and
   absent from its URL prefix. Three separate tests assert it.
2. **A write is observable, not asserted.** Corrections land as amendment rows
   composed at the read boundary, so the eval can snapshot 212 invoices before
   and after a full run and diff the bytes.
3. **A trap built on purpose, and measured.** 36 scenarios exist specifically to
   separate a valid partial delivery from an over-invoice — the case that looks
   identical and is not.
4. **Escalation is a first-class answer.** 28 scenarios are underdetermined by
   construction. Declining is scored as *correct*; inventing a figure is scored
   as *unsafe* and must stay at zero.
5. **A rule-engine baseline, with the caveat attached to the number.** The
   report carries the reason its own 100% is a floor rather than a result.
6. **Safety fails the build; accuracy does not.** A threshold picked before you
   have a baseline tests your guess, not the agent.
7. **Ground truth is structurally unreachable.** Checked in both directions —
   nothing arrives over HTTP, and no module outside the harness can name the
   directory.
8. **Reproducible to the byte.** One seed, 200 scenarios, verified across hash
   seeds and re-verified by CI on every push.
9. **The bugs that did not crash are written down.** A duplicated enum value, an
   empty split, an f-string in a SQL WHERE clause — with what each one taught.
10. **The gaps are stated before anyone asks.** No authentication and exactly
    why; prompt-cache tokens unaccounted; manifests never applied to a live
    cluster; the mock cleaner than production SAP.

---

## Status and roadmap

All eleven planned steps are complete.

- [x] Repo skeleton, reproducible environment
- [x] Deterministic scenario generator + exception taxonomy
- [x] OData read endpoints on the mock ERP
- [x] Human approval state machine
- [x] Agent loop with typed, schema-enforced output
- [x] Tracing and cost accounting
- [x] Eval suite with safety gates in CI
- [x] Review UI
- [x] Case study + scripted demo
- [x] Containers, Kubernetes, SAP mapping
- [ ] Record golden cassettes against a live model — turns CI's replay job real
      and produces the first measured accuracy number

---

<div align="center">

**Built by [Sai Aryan Nampally](https://github.com/voyageuraryan)** ·
[MIT](LICENSE)

*95% of enterprise AI pilots fail on integration, not on models.<br/>
So this is the integration-hard version.*

</div>
