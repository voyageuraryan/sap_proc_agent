# Functional specification

One page on what the problem is, what was built, and how it was delivered.
Detail lives in [the architecture doc](03-architecture.md) and
[the implementation guide](02-implementation-guide.md).

---

## 1. Problem statement

An Accounts Payable clerk works a queue of blocked supplier invoices. For each
one they open several SAP transactions and perform a **three-way match** — the
purchase order (what was ordered), the goods receipt (what arrived), and the
invoice (what was billed) — then decide: release the block, correct the invoice,
or chase somebody.

Three things make this expensive:

1. **Volume.** Most of the queue is fine. Human attention is spent proving that.
2. **The differences are subtle.** Two invoices can look almost identical and
   mean opposite things. Ordered 14 / received 13 / **billed 14** is
   over-billing. Ordered 13 / received 1 / **billed 1** is a perfectly normal
   partial delivery. Both were short-delivered; only one is wrong.
3. **The cost of being wrong is asymmetric.** Paying a wrong amount is a
   financial loss. Wrongly correcting a good invoice damages a supplier
   relationship *and* burns the trust that any automation depends on.

**Why this is not already automated.** The rules are only half the job. The
other half is judgement — knowing when the evidence does not support any answer
— and no finance function will let software post to a ledger unsupervised. The
common industry observation is that the large majority of enterprise AI pilots
never reach production, and the post-mortems point at integration, permissions
and auditability rather than at model quality.

---

## 2. Proposed solution

An agent that investigates a blocked invoice end to end against an ERP over its
real integration protocol, and **cannot change any document without a named
human approving the exact bytes it proposed**.

### Scope

**In scope**

- Investigate one supplier invoice across PO, goods receipts, and vendor history
- Classify it into one of eight outcomes and choose one of four decisions
- Where a fix is stateable, propose it with the current and intended values
- Where the evidence is underdetermined, **escalate** — treated as a correct
  answer, not a failure
- A human review queue to approve, reject, or apply
- Measurement: 200 labelled scenarios, graded decisions, cost per correct
  decision, and safety gates that fail the build

**Out of scope (v1)**

- Authentication (deliberate — see the implementation guide; a login box backed
  by nothing is worse than none)
- Multi-line purchase orders, unit-of-measure conversion, multi-currency
- Writing to a real SAP system
- Autonomous action of any kind

### The eight outcomes

| Ground truth | Right decision |
| --- | --- |
| `CLEAN` — documents agree | `POST_INVOICE` |
| `PRICE_MINOR` — variance inside this supplier's tolerance, invoice blocked | `RELEASE_BLOCK` |
| `PRICE_MAJOR` — variance outside tolerance | `PROPOSE_CORRECTION` |
| `QTY_OVER` — billed more than was received | `PROPOSE_CORRECTION` |
| `GR_MISSING` — nothing received | `ESCALATE` |
| `GR_PARTIAL` — a valid partial delivery | `POST_INVOICE` |
| `DUP_INVOICE` — same supplier reference billed twice | `PROPOSE_CORRECTION` |
| `AMBIGUOUS` — underdetermined by construction | `ESCALATE` |

### The guarantee

> **No document changes without a named human approving the exact payload that
> was shown to them.**

Enforced in four independent ways, each asserted by a test:

1. The agent's tool registry contains **no** apply, approve or reject — the
   capability is absent, not refused.
2. Applying requires status `APPROVED`; anything else returns `409`.
3. Applying requires `sha256(payload sent) == approved_hash` — approving *a*
   payload is not approving *any* payload.
4. The review UI's forms carry the payload hash they rendered, so a reviewer
   acting from a stale page is refused before the ERP is touched.

### Non-functional requirements

| Requirement | How it is met |
| --- | --- |
| Reproducible | One seed → 200 byte-identical scenarios; CI regenerates and diffs |
| Auditable | Every proposal records who approved what, when, and the hash |
| Observable | One trace per run; token usage and cost per model call |
| Measurable | Labelled eval set with a stratified holdout; safety gates fail the build |
| Runnable offline | Rule baseline, local JSONL tracing, no API key needed for the demo |
| Portable | One container image; Kubernetes manifests; `uv sync` on a clean machine |

---

## 3. Implementation, in brief

Six packages. The boundaries are the design — most guarantees are consequences
of who may import what.

| Package | Responsibility |
| --- | --- |
| `erp_domain` | SAP-shaped models shared by the generator and the ERP |
| `generator` | 200 labelled scenarios from one seed |
| `mock_erp` | OData V2 read service **plus** the approval state machine |
| `agent` | The tool-calling loop. Talks HTTP; imports none of the above |
| `evals` | Scoring and safety gates. The only reader of the ground truth |
| `review_ui` | The human queue. A client of the ERP, not part of it |

### Delivery order, and why

| # | Step | Why here |
| ---: | --- | --- |
| 1–2 | Skeleton, domain models | Line endings and `Decimal` are cheap now, expensive later |
| 3 | Generator | Nothing can be measured until there is labelled data |
| 4 | ERP read endpoints | The agent needs something to read |
| 5 | **Approval gate** | **Before the agent**: it shapes the data model, and retrofitting it means rewriting the agent |
| 6 | Agent loop | Now designed against a gate that already exists |
| 7 | Tracing and cost | A run you cannot inspect is a run you cannot debug |
| 8 | **Eval harness** | **Before polish**: a demo tells you about the cases you chose; an eval tells you about the ones you did not |
| 9 | Review UI | The human half of the gate |
| 10–11 | Case study, demo, deploy | Nobody can evaluate what they cannot run |

### Acceptance criteria

| Criterion | Status |
| --- | --- |
| 200 scenarios regenerate byte-identically, independent of hash seed | met |
| No ground-truth label reaches anything the agent sees | met — checked over HTTP *and* by AST |
| The agent has no capability to approve or apply | met — asserted from the registry and the routing table |
| A correction changes a document only after approval and a hash match | met — verified over real sockets |
| Every run is traced and priced | met |
| Safety gates fail the build | met — `proc-evals` exits 1 |
| One command starts the whole system | met — `docker compose up`, or `make erp` + `make ui` |
| Test suite green, lint clean | met — 433 tests, zero warnings |

### What is deliberately not claimed

- The manifests have never been applied to a live cluster.
- The ABAP/CDS in the architecture doc is illustrative and has not been
  activated on a real system.
- The rule baseline scores 100% because the dataset was generated by rules. It
  is the **floor**, not a result. See
  [the baseline section](03-architecture.md#8-evaluation-and-safety-gates).
- The mock is cleaner than production SAP, so the agent looks better here than
  it would in the wild.
