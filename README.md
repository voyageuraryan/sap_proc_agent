# SAP Procurement Exception Agent

An agent that investigates blocked purchase orders and mismatched invoices across
PO / goods-receipt / invoice / vendor-history data, explains what went wrong, and
drafts a correction — **which a human must approve before anything is written back.**

It is not autonomous. That is the point.

> **Status:** In progress. See [Current state](#current-state) for what actually works today.

---

## Why this project exists

The widely-cited figure from MIT's NANDA initiative (*The GenAI Divide: State of AI in
Business*, 2025) is that ~95% of enterprise GenAI pilots produce no measurable P&L
impact. The number is contested and the methodology has been fairly criticised. But
the failure mode it describes matches what I've seen in enterprise environments:
pilots don't die because the model was wrong. They die on integration, on permissions,
on nobody trusting the system enough to let it touch a production record.

So this project deliberately optimises for the boring, hard parts:

- **Typed tool contracts against a real protocol** (OData), not hand-rolled JSON.
- **A hard human-approval gate on every write**, enforced structurally rather than
  by prompt instruction.
- **Full tracing** of every LLM and tool call, with a cost figure per resolution.
- **Evals in CI**, including adversarial cases that try to bypass the approval gate.

The model is close to the least interesting component here. Swapping it should be a
config change, and the eval suite should tell you within minutes whether the swap was
an improvement.

---

## The problem, in the user's words

An Accounts Payable clerk works a queue of blocked invoices. For each one they open
several transactions, eyeball a PO against a goods receipt against an invoice, decide
whether the difference is acceptable, and either release the block or chase someone.

They are not technical. They are measured on throughput and on not paying wrong
amounts — and those two pressures point in opposite directions. An agent that speeds
them up but occasionally approves a wrong payment is worse than no agent at all.

That constraint is why the approval gate is architectural rather than advisory.

---

## The approval gate

**No tool that mutates state can execute without a recorded human approval.**

The gate is a state machine in the mock ERP service, on the *other side of a network
boundary* from the LLM. The agent cannot write. It can only ask the ERP to stage a
proposal; the ERP refuses to execute that proposal until an approval event exists
recording:

- who approved it
- when
- the exact payload approved
- the agent's stated reasoning at the time of proposal

A prompt injection can make the agent *ask*. It cannot make the ERP *comply*.

This is a testable property, not a claim — see the adversarial gate-bypass cases in
the eval suite.

**Rejected alternative:** running the mock ERP as an in-process module of the agent
service. Faster to build, but then the gate is a function call the LLM's own process
could route around, and one careless refactor away from not existing. A trust boundary
that lives inside the same process as the untrusted input is not a trust boundary.

---

## Architecture

```
┌──────────────────┐   HTTP / OData    ┌────────────────────────┐
│  agent service   │ ────────────────► │   mock ERP service     │
│                  │                   │                        │
│  · LLM tool loop │  read tools       │  · PO / GR / invoice   │
│  · tool contracts│  ────────────────►│  · vendor history      │
│  · proposals     │                   │  · tolerance config    │
│                  │  propose (staged) │  · APPROVAL STATE      │
│                  │  ────────────────►│    MACHINE  ◄── the    │
└──────────────────┘                   │    only writer         │
         │                             └────────────────────────┘
         │ traces                                  ▲
         ▼                                         │ approve / reject
   ┌───────────┐                            ┌──────────────┐
   │ Langfuse  │                            │  review UI   │
   └───────────┘                            │  (human)     │
                                            └──────────────┘
```

---

## Scope

### v1 handles exactly three exception types

1. **Price variance** — invoice unit price differs from PO unit price.
2. **Quantity variance** — invoiced quantity exceeds received quantity.
3. **Missing goods receipt** — invoice arrived, no GR posted, PO requires one.

Three, not ten. The interesting question is not how many exception types a demo can
list; it's whether the agent behaves correctly on the *trap* cases — a valid partial
delivery that looks like an over-invoice, or a case with genuinely insufficient
evidence where the correct answer is "I don't know, escalate."

### Explicitly out of scope for v1

ABAP explainer · Kubernetes · multi-agent architectures · fine-tuning · real SAP
connectivity · auth beyond a hardcoded demo user · more than three exception types ·
a polished frontend · multi-line purchase orders.

These are written down so I can point at the list when tempted.

---

## About the "SAP" in the name

**This does not connect to a real SAP system.** There is no SAP licence behind it and
no SAP instance running.

What it does is speak SAP's dialect against a mock: OData V2-shaped responses, SAP
field naming (`EBELN`, `EBELP`, `LIFNR`, `MATNR`, `MENGE`, `NETPR`, `BELNR`), and
SAP-style error payloads. Conventions were taken from the public SAP ES5 / Northwind
OData services.

That's a deliberate choice, not a limitation I'm hiding. A real sandbox would not let
me inject the malformed data the agent needs to be tested against, and the demo would
break whenever the sandbox went down. The tradeoff is real: my mock is almost certainly
cleaner than production SAP, so the agent likely looks better here than it would in the
wild. I mitigate that by seeding deliberately adversarial cases — see
[docs/data-plan.md](docs/data-plan.md).

---

## Data

All scenario data is synthetic and generated deterministically from a seed. No public
dataset contains linked PO / goods-receipt / invoice triples with labelled defects —
receipts and match outcomes live inside private ERP systems and are never published.

Synthesising them is a feature rather than a compromise: it produces a reproducible,
difficulty-controllable eval set with ground-truth labels. Realism (vendor names,
commodity codes, amount distributions) is borrowed from public procurement data.

Ground-truth labels live in a sidecar the agent's tools cannot read. Leaking labels
through the tool layer would silently invalidate every eval.

Full reasoning: [docs/data-plan.md](docs/data-plan.md).

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Mock ERP | FastAPI | Typed request/response via Pydantic; OpenAPI for free |
| Agent | *TBD — see decisions.md* | |
| LLM routing | LiteLLM | Model-agnostic; makes the cost comparison table possible |
| Tracing | Langfuse | Per-call cost and latency; self-hostable |
| Evals | promptfoo | Runs in CI; declarative assertions over the taxonomy |
| Packaging | uv workspace | One lockfile → reproducible clone-and-run |
| Runtime | Docker Compose | Two services, one command |

---

## Repository layout

```
sap-proc-agent/
├── docs/
│   ├── spec.md          # what ships in v1 and what deliberately doesn't
│   └── data-plan.md     # where the data comes from, exception taxonomy
├── decisions.md         # design decisions, alternatives rejected, and why
└── packages/
    ├── generator/       # deterministic scenario generator
    └── mock_erp/        # ERP service + approval state machine
```

Directories appear in the commit where they first do something. There are no empty
placeholder folders.

---

## Current state

Build order is deliberate — the approval state machine comes *before* the agent,
because it shapes the data model.

- [x] Repo skeleton, reproducible environment
- [ ] Scenario generator + exception taxonomy
- [ ] Read-only tool endpoints on the mock ERP
- [ ] Approval state machine
- [ ] Agent loop
- [ ] Tracing
- [ ] Eval suite in CI
- [ ] Review UI
- [ ] Case study + demo video

---

## Running it

*Not yet runnable. This section lands with the first working slice.*

---

## What broke

*A running log of things that failed and what I changed. This section is not
decoration — if it's empty when the project is finished, I wasn't paying attention.*