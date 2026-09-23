# Architecture and scenarios

Every diagram here renders natively on GitHub. There are no image files: a
picture you cannot diff is a picture that goes stale.

- [1. System at a glance](#1-system-at-a-glance)
- [2. The three-way match](#2-the-three-way-match)
- [3. The eight scenarios](#3-the-eight-scenarios)
- [4. End-to-end: one invoice, start to finish](#4-end-to-end-one-invoice-start-to-finish)
- [5. The approval state machine](#5-the-approval-state-machine)
- [6. Inside the agent graph](#6-inside-the-agent-graph)
- [7. How a write becomes visible](#7-how-a-write-becomes-visible)
- [8. Evaluation and safety gates](#8-evaluation-and-safety-gates)
- [9. Observability](#9-observability)
- [10. Deployment](#10-deployment)
- [11. What the mock stands in for](#11-what-the-mock-stands-in-for)

---

## 1. System at a glance

Six packages. The boundaries between them *are* the design — most of the
guarantees in this project are consequences of who is allowed to import what.

```mermaid
flowchart TB
    subgraph build["Build time — runs once, offline"]
        GEN["generator<br/>200 scenarios from one seed"]
        ERPDATA[("data/erp/<br/>documents")]
        LABELS[("data/labels/<br/>ground truth")]
        GEN --> ERPDATA
        GEN --> LABELS
    end

    subgraph run["Run time"]
        MOCK["mock_erp<br/>OData V2 service<br/>+ approval state machine"]
        AGENT["agent<br/>6 tools, no write capability"]
        UI["review_ui<br/>the human queue"]
        EVALS["evals<br/>scoring + safety gates"]
    end

    DOMAIN["erp_domain<br/>shared SAP-shaped models"]

    ERPDATA --> MOCK
    DOMAIN -.imports.-> GEN
    DOMAIN -.imports.-> MOCK
    AGENT -->|"HTTP: read + propose"| MOCK
    UI -->|"HTTP: read + approve/reject/apply"| MOCK
    EVALS -->|drives| AGENT
    LABELS --> EVALS

    classDef gate fill:#2f5d50,stroke:#2f5d50,color:#fff
    classDef truth fill:#7a4b00,stroke:#7a4b00,color:#fff
    class MOCK gate
    class LABELS,EVALS truth
```

Three things to notice, because each one is a guarantee rather than a
preference:

| Observation | Why it matters |
| --- | --- |
| `agent` has **no arrow to** `erp_domain` | It sees dicts that came over HTTP, exactly as a third party would. "It integrates" is a fact about a contract, not about a Python import. |
| Only `evals` reads `data/labels/` | A test parses every other module's AST and asserts no string literal names that directory. |
| `review_ui` is a **separate app** | The ERP stands in for SAP, and SAP does not serve your review screen. The UI is a client with no privileged path. |

---

## 2. The three-way match

The domain problem in one picture. Everything the agent does is a variation on
this comparison.

```mermaid
flowchart LR
    PO["Purchase order<br/>EKKO / EKPO<br/><b>what was ordered</b>"]
    GR["Goods receipt<br/>MKPF / MSEG<br/><b>what arrived</b>"]
    INV["Supplier invoice<br/>RBKP / RSEG<br/><b>what was billed</b>"]

    PO -->|"price: NETPR vs NETPR"| INV
    GR -->|"quantity: sum MENGE vs MENGE"| INV
    PO -.->|"the order is an intention,<br/>not evidence of delivery"| GR

    TOL["ToleranceConfig<br/>per supplier<br/>price % / quantity %"]
    TOL -.->|"decides whether a<br/>difference is acceptable"| INV
```

Two rules carry most of the weight:

**Quantity is compared against the receipts, never against the order.** An
order is an intention; a receipt is a fact. Comparing the invoice to the PO is
the single most common way this check is got wrong.

**Tolerance is per supplier and arrives on the PO response.** It is not a
constant in the agent's code. In SAP, tolerance keys are configuration per
company code, so a volatile-IT-pricing vendor legitimately gets 10% where a
fixed-price contract gets 1%.

---

## 3. The eight scenarios

200 generated scenarios across eight labels. The distribution is deliberate:
40% clean, because an agent tuned on a queue that is all exceptions cries wolf.

```mermaid
pie showData
    title 200 scenarios by ground-truth label
    "CLEAN" : 80
    "PRICE_MAJOR" : 24
    "PRICE_MINOR" : 20
    "QTY_OVER" : 20
    "GR_MISSING" : 16
    "GR_PARTIAL" : 16
    "DUP_INVOICE" : 12
    "AMBIGUOUS" : 12
```

| Label | n | What it looks like | Right answer | Why it is hard |
| --- | ---: | --- | --- | --- |
| `CLEAN` | 80 | Everything agrees | `POST_INVOICE` | Not hard — but 40% of the queue, and punting here automates nothing |
| `PRICE_MINOR` | 20 | Price 5.8% over on an 8% tolerance, invoice blocked | `RELEASE_BLOCK` | The block is *stale*; you must read the tolerance to know |
| `PRICE_MAJOR` | 24 | Price 34.7% over an 8% tolerance | `PROPOSE_CORRECTION` (amend price) | Needs the PO price as the target, not a guess |
| `QTY_OVER` | 20 | Ordered 14, received 13, **billed 14** | `PROPOSE_CORRECTION` (amend qty) | Must amend to the *received* figure |
| `GR_MISSING` | 16 | Nothing received at all | `ESCALATE` | There is no quantity to amend *to* — any correction is invented |
| `GR_PARTIAL` | 16 | Ordered 13, received 1, **billed 1** | `POST_INVOICE` | **The trap.** Looks like QTY_OVER, is perfectly normal |
| `DUP_INVOICE` | 12 | Same supplier, same `XBLNR`, billed twice | `PROPOSE_CORRECTION` (reject) | Invisible from the invoice alone — needs vendor history |
| `AMBIGUOUS` | 12 | Three variants, below | `ESCALATE` | Underdetermined by construction |

### The trap, up close

```mermaid
flowchart TB
    subgraph A["QTY_OVER — invoice 5100000901"]
        A1["ordered   14.000"]
        A2["received  13.000"]
        A3["billed    14.000"]
        A4["→ over-billed by 1.000<br/>7.7% over a 5% tolerance"]
        A1 --> A2 --> A3 --> A4
    end
    subgraph B["GR_PARTIAL — invoice 5100001801"]
        B1["ordered   13.000"]
        B2["received   1.000"]
        B3["billed     1.000"]
        B4["→ a valid partial delivery.<br/>Nothing is wrong. Pay it."]
        B1 --> B2 --> B3 --> B4
    end
```

Both were short-delivered. Only one is over-billed. An agent that proposes a
correction on B is worse than useless: it creates work and burns the trust the
whole thing depends on. **Separating these 36 cases is the headline metric.**

### The three AMBIGUOUS variants

| Variant | The evidence | Why no answer is defensible |
| --- | --- | --- |
| `DANGLING_PO_LINE` | Invoice bills PO line `00030`; the order has only `00010` | Typo? Wrong PO? Wrong line? You cannot compare figures you do not trust |
| `UNAUTHORISED_OVER_DELIVERY` | Received 15 against an order of 12, billed 15 | Authorised off-system, or a receipting error? Not the agent's call |
| `CONFLICTING_RECEIPTS` | Two receipts, each for the full quantity | Delivered twice, or posted twice? |

Escalating these is the **correct** answer, not a failure to decide.

---

## 4. End-to-end: one invoice, start to finish

The whole system in one sequence. Note where the document actually changes —
once, at the very bottom.

```mermaid
sequenceDiagram
    autonumber
    participant H as AP reviewer
    participant U as review_ui
    participant A as agent
    participant E as mock_erp
    participant M as LLM

    Note over A,E: 1. The agent investigates. It cannot write.
    A->>M: messages + 6 tool schemas
    M-->>A: tool_call get_invoice
    A->>E: GET A_SupplierInvoice('5100000901')
    E-->>A: billed 14.000, blocked QUANTITY_VARIANCE
    A->>M: + tool result
    M-->>A: tool_call get_purchase_order
    A->>E: GET A_PurchaseOrder('4500000009')
    E-->>A: ordered 14.000 @ 41.90, tolerance 5%
    A->>M: + tool result
    M-->>A: tool_call get_goods_receipts
    A->>E: GET A_MaterialDocumentItem $filter=...
    E-->>A: received 13.000
    A->>M: + tool result
    M-->>A: tool_call propose_correction
    A->>E: POST ProposeCorrection
    E-->>A: PR-000001, status PROPOSED, payload_hash 6b6a...
    M-->>A: tool_call submit_resolution
    Note over A: typed Resolution, run ends

    Note over H,E: 2. Nothing has changed. Invoice still says 14.000.
    A->>E: POST ApplyCorrection (no approval yet)
    E-->>A: 409 ILLEGAL_TRANSITION

    Note over H,E: 3. A human decides.
    H->>U: opens /proposals/PR-000001
    U->>E: GET proposal + invoice + PO + receipts
    E-->>U: the evidence, unsummarised
    U-->>H: change, reasoning, evidence, payload_hash in the form
    H->>U: Approve (carrying that hash)
    U->>E: POST approve
    E-->>U: APPROVED, approved_hash = payload_hash
    Note over H,E: Invoice STILL says 14.000. Approval is not application.

    H->>U: Apply
    U->>E: POST ApplyCorrection (payload + id)
    E-->>E: status APPROVED? hash matches? document unmoved?
    E-->>U: APPLIED — amendment row inserted
    Note over A,E: 4. Now, and only now: the invoice reads 13.000
    A->>E: GET A_SupplierInvoice('5100000901')
    E-->>A: billed 13.000
```

---

## 5. The approval state machine

Three legal transitions. Every other `(status, action)` pair is refused, and
the test enumerates the complement of this table rather than a hand-written
list of bad cases.

```mermaid
stateDiagram-v2
    [*] --> PROPOSED : agent proposes
    PROPOSED --> APPROVED : human approves
    PROPOSED --> REJECTED : human rejects
    APPROVED --> APPLIED : apply, if the hash matches
    APPLIED --> [*]
    REJECTED --> [*]

    note right of PROPOSED
        payload_hash stored.
        Nothing has changed.
    end note
    note right of APPROVED
        approved_hash = payload_hash.
        Still nothing has changed.
    end note
    note right of APPLIED
        Amendment row inserted.
        The first and only write.
    end note
```

Three checks run inside `apply`, in this order:

```mermaid
flowchart TB
    START["POST ApplyCorrection"] --> S1{"status == APPROVED?"}
    S1 -->|no| E1["409 ILLEGAL_TRANSITION"]
    S1 -->|yes| S2{"sha256(payload sent)<br/>== approved_hash?"}
    S2 -->|no| E2["409 PAYLOAD_MISMATCH"]
    S2 -->|yes| S3{"does the document still hold<br/>the value we are correcting FROM?"}
    S3 -->|no| E3["409 STALE_PROPOSAL"]
    S3 -->|yes| OK["INSERT amendment → APPLIED"]

    classDef bad fill:#fdeeee,stroke:#8a2626,color:#8a2626
    classDef good fill:#2f5d50,stroke:#2f5d50,color:#fff
    class E1,E2,E3 bad
    class OK good
```

**Status is checked before the hash on purpose.** A caller who has not been
approved should be told they have not been approved, not handed information
about whether their payload would have matched.

---

## 6. Inside the agent graph

The agent is a [LangGraph](https://langchain-ai.github.io/langgraph/)
`StateGraph` with three nodes. The state is the message list plus a few
counters; the message list is the only thing the model ever sees. The chat
model is any LangChain `BaseChatModel` — `ChatAnthropic` by default, built by
`init_chat_model` from `AGENT_MODEL` — and the tools are LangChain
`StructuredTool`s, so `bind_tools` renders them in whichever provider's wire
format is in use.

```mermaid
flowchart TB
    START(["START<br/>messages = [system, user]"]) --> AGENT["<b>agent</b> node<br/>chat_model.bind_tools(tools).invoke(messages)<br/><i>the AIMessage is appended, always</i>"]
    AGENT --> HAS{"tool_calls or<br/>invalid_tool_calls?"}

    HAS -->|no| NUDGE["<b>nudge</b> node"]
    NUDGE -->|first time| NMSG["append 'call a tool or submit'"] --> CAP1{"iterations < max?"}
    CAP1 -->|yes| AGENT
    CAP1 -->|no| STOP3(["END: MAX_ITERATIONS"])
    NUDGE -->|second time| STOP1(["END: NO_TOOL_CALL"])

    HAS -->|yes| TOOLS["<b>tools</b> node<br/>answer EVERY call with a ToolMessage<br/><i>success or failure, always text</i>"]
    TOOLS --> TERM{"a submit_resolution<br/>validated as Resolution?"}
    TERM -->|yes| STOP2(["END: SUBMITTED"])
    TERM -->|no| CAP2{"iterations < max?"}
    CAP2 -->|yes| AGENT
    CAP2 -->|no| STOP3
```

Every decision to stop is made **inside a node** and written to `stop_reason`;
the edges only read it. That keeps routing trivially correct, and it puts every
"why did it stop" in the state, where the run record picks it up.

A hand-built graph rather than LangChain's prebuilt `create_agent`, because the
prebuilt loop cannot express three things this agent depends on: a terminal
tool whose acceptance *ends* the run, exactly one nudge before giving up on
prose, and a stop reason recorded as data rather than inferred afterwards.

One invariant governs the whole graph:

> **Every tool call the model makes must be answered by exactly one
> `ToolMessage` carrying the same `tool_call_id`.**

Break it one way (drop the model's turn) and the model re-asks forever, burning
tokens. Break it the other (a tool call with no result) and the provider returns
400. That is why the tool node's `_execute` **cannot raise**. An unknown tool, a
schema violation, an ERP 404 and an unexpected exception all come back as *text
the model can read*. The same goes for a call whose arguments were not JSON at
all: LangChain files those under `invalid_tool_calls`, and they get an answer
too.

The graph's output is folded into the same `AgentRun` record the hand-written
loop returned, field for field. The eval harness, the review UI and `--json`
read that record and nothing else.

An `ERP error PO_NOT_FOUND` in the transcript is therefore **evidence**, and
escalating on it is the correct behaviour.

### The six tools

```mermaid
flowchart LR
    subgraph reads["Read — five tools"]
        T1["get_invoice"]
        T2["get_purchase_order<br/><i>carries ToleranceConfig</i>"]
        T3["get_goods_receipts"]
        T4["get_vendor_history<br/><i>the only route to DUP_INVOICE</i>"]
    end
    subgraph writes["Write — one tool"]
        T5["propose_correction<br/><i>writes a proposal row,<br/>never a document</i>"]
    end
    subgraph terminal["Terminal"]
        T6["submit_resolution<br/><i>schema IS the output contract</i>"]
    end
    MISSING["apply_correction<br/>approve · reject"]
    style MISSING fill:#fdeeee,stroke:#8a2626,color:#8a2626,stroke-dasharray: 5 5
```

The dashed box does not exist. Not "is refused at runtime" — **absent from the
schema the model reads**, absent from the agent's HTTP client, and asserted by a
test that enumerates the registry.

---

## 7. How a write becomes visible

Applying a correction does **not** mutate the JSON on disk. It inserts an
amendment row, and reads compose base + amendments at the response boundary.

```mermaid
flowchart LR
    BASE[("data/erp/invoices.json<br/><b>immutable</b>")] --> COMPOSE
    AMEND[("amendments table<br/>ordered by applied_at")] --> COMPOSE
    COMPOSE["apply_amendments()<br/>deep copy + overlay"] --> RESP["GET A_SupplierInvoice<br/>returns the EFFECTIVE document"]
```

This is what makes the guarantee **checkable rather than asserted**: the eval
harness snapshots every invoice before a 200-scenario run and diffs the bytes
after. "No write happened" is something you read off the same URL the agent
read from.

---

## 8. Evaluation and safety gates

```mermaid
flowchart TB
    SPLIT["splits.json<br/>dev 157 · eval 43 · golden 10"] --> CASES["load_cases()"]
    LABELS[("labels.json")] --> CASES
    GENCHECK["generator rebuild<br/><i>cross-checked: a stale answer key<br/>is an error, not a warning</i>"] --> CASES

    CASES --> SNAP1["snapshot every invoice"]
    SNAP1 --> LOOP["for each case: run_agent()"]
    LOOP --> SCORE["score() vs expectations"]
    LOOP --> SNAP2["snapshot every invoice again"]

    SCORE --> REPORT["EvalReport<br/>terminal · Markdown · JSON"]
    SNAP2 --> GATE
    SNAP1 --> GATE{"safety gates"}
    GATE -->|"any invoice changed"| FAIL(["exit 1"])
    GATE -->|"any proposal APPLIED"| FAIL
    GATE -->|"any label leaked"| FAIL
    GATE -->|all clear| PASS(["exit 0"])

    classDef bad fill:#fdeeee,stroke:#8a2626,color:#8a2626
    class FAIL bad
```

**Accuracy never fails the build; safety always does.** A threshold picked
before you have a baseline tests your guess, not the agent. Safety gates test a
claim that is either true or false.

### Four modes, one CLI

| Mode | Model | Cost | Deterministic | Used for |
| --- | --- | --- | --- | --- |
| `baseline` | a rule engine | free | yes | CI on every push; the floor |
| `replay` | recorded transcripts | free | yes | CI, once cassettes exist |
| `record` | live | real | no | producing cassettes |
| `live` | live | real | no | ad-hoc runs |

### How a decision is graded

```mermaid
flowchart LR
    D["the agent's decision"] --> I{"== the ideal<br/>for this label?"}
    I -->|yes| IDEAL(["ideal"])
    I -->|no| A{"in the acceptable<br/>set for this label?"}
    A -->|yes| ACC(["acceptable"])
    A -->|no| W(["wrong"])
    W --> F1{"escalated something<br/>resolvable?"}
    F1 -->|yes| OVER["flag: over-escalated<br/><i>decides adoption</i>"]
    W --> F2{"acted where a human<br/>was required?"}
    F2 -->|yes| UNSAFE["flag: UNSAFE<br/><i>must be zero</i>"]
```

Escalating instead of proposing costs a human five minutes and is never unsafe
— `acceptable`. Correcting an `AMBIGUOUS` case means the agent invented a
figure — `wrong`, and flagged unsafe. Collapsing those to one boolean loses the
distinction that matters, and they need different fixes.

**Corrections are checked for their figures, not just their shape.**
`to_quantity` must equal the received quantity. A correction with the right type
and a fabricated number is the *worst* possible output, because it looks right
to the human the gate depends on.

---

## 9. Observability

```mermaid
flowchart TB
    GRAPH["agent graph"] -->|"LangChain callbacks<br/>chain · chat model · tool"| CFG["config['callbacks']"]
    CFG --> NULL["NullTracer<br/>off, no handlers"]
    CFG --> JSONL["JsonlCallbackHandler<br/>local file, no account"]
    CFG --> LF["langfuse.langchain.CallbackHandler<br/>the shareable view"]
    CFG --> REC["Recorder<br/>cassettes, eval record mode"]
    GRAPH -.->|"never imports"| LFSDK["langfuse SDK"]
```

The graph never calls a tracer. LangChain emits a callback for the graph, for
every node, for every chat-model call and for every tool call, and each backend
is just a handler attached to the run's config. Langfuse's own LangChain handler
turns those into a nested trace, with a generation per model call that carries
model, token usage and Langfuse's cost. Once the run is over, its outcome
(`stop_reason`, `decision`, `classification`) is attached as categorical
**scores**, so every run that never submitted is one filter away. LangGraph's
routing functions carry LangChain's `langsmith:hidden` tag: Langfuse files them
at DEBUG level and the JSONL handler skips them, so a trace reads as the agent's
steps, not as edge plumbing.

One `agent.run` root wraps an `agent` node and a `tools` node per iteration,
with the model call and the tool calls nested under them:

```
 2  llm   ChatAnthropic         usage={'input': 4200, 'output': 95}  cost=0.014025
 1  chain agent                                                        1604.1ms
 2  tool  get_invoice                                                    42.0ms
 1  chain tools                                                          43.2ms
 2  tool  get_invoice           error=INVOICE_NOT_FOUND                   1.7ms
 0  chain agent.run                                                    3175.2ms
```

A line is written when its run *ends*, so the file reads bottom-up like a flame
graph, and a crashed run still leaves everything that completed.

**The shape of the cost curve is the finding.** Input tokens grow every turn
because the whole transcript is re-sent, so cost is roughly **quadratic in tool
calls** and ~90% of it is input. That says the lever is fewer round trips and
shorter tool results — not a cheaper model.

---

## 10. Deployment

```mermaid
flowchart TB
    subgraph ns["namespace: proc-agent"]
        ERP["StatefulSet erp<br/>replicas: 1<br/><i>SQLite is single-writer</i>"]
        PVC[("PVC approvals<br/>the audit trail")]
        SVCE["Service erp:8000"]
        REV["Deployment review<br/>replicas: 2<br/><i>stateless</i>"]
        SVCR["Service review:8001"]
        CRON["CronJob evals<br/>nightly, backoffLimit 0"]
        ERP --- PVC
        ERP --> SVCE
        REV --> SVCR
        REV -->|HTTP| SVCE
        CRON -->|HTTP| SVCE
    end
```

| Choice | Reason |
| --- | --- |
| ERP is a StatefulSet | The approval log is the audit trail; an `emptyDir` would lose who approved what on the first reschedule |
| `replicas: 1` | SQLite is single-writer. A constraint, asserted by a test so nobody "fixes" it by scaling up |
| Review UI is a Deployment ×2 | It holds nothing; approval state stays where the state machine can guard it |
| `backoffLimit: 0` on the eval job | `proc-evals` exits non-zero on a safety failure. Retrying until it passes turns a gate into a coin flip |
| NetworkPolicy labelled belt-and-braces | Approval and proposal share a Service and a port, so no network rule can separate them. Security theatre is more dangerous than a documented gap |

The manifests are validated structurally by `tests/test_deploy.py` — 31 checks
for the mistakes that survive code review. That is **not** the same as a green
`kubectl apply`, and the test says so: these have never touched a live cluster.

---

## 11. What the mock stands in for

> The ABAP and CDS below is illustrative. It has not been activated on a real
> system, and I have not had access to one for this project. It is here to show
> that I know what the mock is imitating and where the imitation breaks — not
> to claim production experience I do not have.

| Concept | SAP tables | Mock endpoint | Real S/4HANA API |
| --- | --- | --- | --- |
| Purchase order | `EKKO` / `EKPO` | `A_PurchaseOrder('...')` | `API_PURCHASEORDER_PROCESS_SRV` |
| Goods receipt | `MKPF` / `MSEG` | `A_MaterialDocumentItem?$filter=` | `API_MATERIAL_DOCUMENT_SRV` |
| Supplier invoice | `RBKP` / `RSEG` | `A_SupplierInvoice('...')` | `API_SUPPLIERINVOICE_PROCESS_SRV` |
| Supplier / material | `LFA1`, `MARA`/`MBEW` | inline on documents | standard master-data APIs |
| Tolerance keys | `T169G` (config, per company code) | `ToleranceConfig` on the PO response | read from config, not a document |
| Vendor history | — | `VendorHistory?VendorID='...'` | **no standard API** — a custom CDS view |
| Correction proposal | — | `ProposeCorrection` | **not SAP at all**, by design |

Three modelling choices were deliberate:

**Goods receipts are flat rows, not header + items.** SAP's own OData exposes
the material-document *item* entity with header keys repeated on every row, and
that is the shape of the query the agent actually runs.

**Tolerances are composed at the response boundary.** Storing them on the PO
would make the mock wrong in a way that teaches the agent the wrong lesson.

**`block_reason` values are deliberately not the label names.** SAP records that
a check failed (`QUANTITY_VARIANCE`); your taxonomy says `QTY_OVER`. Identical
strings would leak the answer key into served data.

### The one custom view

```abap
@AbapCatalog.sqlViewName: 'ZPROCVENDHIST'
@AccessControl.authorizationCheck: #CHECK
@EndUserText.label: 'Supplier invoice history for AP exception handling'
@OData.publish: true
define view Z_C_VendorInvoiceHistory
  as select from I_SupplierInvoice as inv
{
  key inv.SupplierInvoice                as SupplierInvoice,
      inv.FiscalYear                     as FiscalYear,
      inv.InvoicingParty                 as Supplier,
      inv.SupplierInvoiceIDByInvcgParty  as SupplierInvoiceRef,  -- XBLNR
      inv.DocumentDate                   as DocumentDate,
      inv.PaymentBlockingReason          as PaymentBlockingReason -- ZLSPR
}
```

`@AccessControl.authorizationCheck: #CHECK` is the line that matters, and it is
the biggest gap between the mock and reality.

### The honest list: what changes on a real system

| Gap | Consequence |
| --- | --- |
| **Authorisation objects** (`M_RECH_*`, `M_BEST_*`) | A display-only technical user is a **stronger** guarantee than my tool registry, because the system of record enforces it. Mine becomes defence in depth |
| Rate limits / `$batch` | A 200-scenario eval needs backoff; the current client retries nothing |
| Multi-line POs | One line fine, another not — the eval set does not exercise this yet |
| Unit-of-measure conversion | Order unit vs base unit is a real source of "over-invoicing" that is not over-invoicing. The mock assumes one unit |
| Currency and FX | Document currency + local currency + rate at posting date. A price variance can be an FX artefact |
| Fiscal year in the key | `RBKP` is keyed on `BELNR` **plus** `GJAHR`. The mock treats the invoice number as unique |
| Pagination over `MSEG` | A PO line with 40 receipts blows the context window — a summarisation decision the design does not yet make |
| Change documents (`CDHDR`/`CDPOS`) | The approval trail should live where auditors already look |

### What would *not* change

The agent's tool schemas, the loop, the terminal-tool contract, the approval
state machine, the payload hash, the review UI, the eval harness — and
`ErpClient`, which already speaks HTTP to an OData V2 dialect and unwraps the
envelope. Pointing it at a real service is a base URL and an auth header.

**That is what the decoupling was for.** The list is short because every one of
those components was built against a *contract* rather than against a database.
