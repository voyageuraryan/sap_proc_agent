# spec.md — ERP Copilot: Procurement Exception Agent

Status: DRAFT v0.1 · Owner: [you] · Last updated: [date]

---

## 1. The one-sentence version

An agent that takes a blocked purchase order or a mismatched invoice, investigates it
across PO / goods receipt / invoice / vendor-history data, explains what went wrong,
and drafts a correction that a human must approve before anything is written back.

## 2. Why this project exists (the portfolio thesis)

Most enterprise AI pilots die on integration, permissions, and trust — not on model
quality. This project deliberately optimises for the boring, hard parts: typed tool
contracts against a real protocol (OData), a hard human-approval gate on every write,
full tracing, and evals in CI. The model is almost the least interesting component,
and the README says so out loud.

## 3. Primary user

An Accounts Payable clerk who works a queue of blocked invoices. Today they open
several transactions, eyeball a PO against a receipt against an invoice, decide
whether the difference is acceptable, and either release the block or chase someone.
They are not technical. They are measured on throughput and on not paying wrong amounts.

> TODO: replace this with what you learn in discovery.md. If you can talk to even one
> real AP or procurement person, this section should quote them.

## 4. v1 scope — what ships

### 4.1 Exception types handled
Exactly three. Not five, not ten.
1. **Price variance** — invoice unit price differs from PO unit price.
2. **Quantity variance** — invoiced quantity exceeds received quantity.
3. **Missing goods receipt** — invoice arrived, no GR posted, PO requires one.

### 4.2 The agent loop
Read exception → call tools to gather evidence → classify against the exception
taxonomy → decide: within tolerance / outside tolerance / insufficient information →
produce a resolution proposal → if the proposal implies a write, stop and request
human approval.

### 4.3 Tools (read-only unless marked)
| Tool | Returns |
|---|---|
| `get_purchase_order(po_number)` | Header + line items + tolerance config |
| `get_goods_receipts(po_number)` | All GR postings against the PO |
| `get_invoice(invoice_id)` | Header + line items + block reason code |
| `get_vendor_history(vendor_id)` | Prior exception counts, on-time rate, open disputes |
| `propose_correction(...)` **[WRITE — GATED]** | Stages a change; returns proposal ID |
| `apply_correction(proposal_id)` **[WRITE — GATED]** | Executes ONLY after approval |

### 4.4 The approval gate — the non-negotiable
No tool that mutates state may execute without an explicit human approval event
recorded with: who approved, when, what exact payload, and the agent's stated
reasoning at the time. The gate is a state machine in the API layer, not a prompt
instruction. A jailbroken prompt must not be able to cause a write. This is a
testable property and there is an eval for it.

### 4.5 Surfaces
- FastAPI backend with the tool endpoints and the approval state machine.
- A deliberately plain review UI: exception, evidence trail, proposed fix,
  Approve / Reject / Edit. Ugly is fine. The trace being legible is the point.

### 4.6 Observability & evals
- Every LLM call and tool call traced (Langfuse), with cost per resolution.
- promptfoo suite in CI covering the taxonomy plus adversarial gate-bypass cases.
- A published cost table: cost per exception resolved, by model.

## 5. Explicitly OUT of v1

Write these down so you can point at the list when you're tempted.

- ABAP explainer (stretch — only after everything above works end to end)
- Kubernetes (Docker Compose is sufficient until the agent works; K8s is a late,
  optional flourish and should never be the reason v1 is unfinished)
- Multi-agent / agent handoff architectures
- Fine-tuning anything
- Real SAP connectivity (mock endpoints that *speak SAP's dialect* are the point)
- Authentication beyond a hardcoded demo user
- More than three exception types
- A polished frontend

## 6. Definition of done for v1

- [ ] Agent resolves all three exception types on the seeded scenario set
- [ ] Zero writes possible without a recorded approval (proven by eval, not assertion)
- [ ] Every run traced with a per-resolution cost figure
- [ ] promptfoo suite runs green in GitHub Actions
- [ ] `docker compose up` gives a working demo from a clean clone
- [ ] Case-study README complete, including a real "what broke" section
- [ ] 3–5 minute demo video

## 7. Build order (do not reorder)

1. Mock ERP data + generator (`data-plan.md`)
2. FastAPI read-only tool endpoints
3. Approval state machine — *before* the agent, because it shapes the data model
4. Agent loop against the tools
5. Tracing
6. Evals
7. Review UI
8. README + video
9. *Only now:* K8s, ABAP explainer
