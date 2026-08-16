# data-plan.md — Where the data comes from

Status: DRAFT v0.1 · Last updated: [date]

---

## 1. The problem

The agent needs linked triples: a purchase order, the goods receipts posted against
it, and the invoice that references it — with *known, labelled* defects so evals can
assert correct behaviour.

No public dataset contains this. Public procurement portals publish PO headers and
lines; goods receipts and invoice-matching outcomes live inside private ERP systems
and are never released. Even where PO data exists, it carries no exception labels.

**Decision: synthesise the triples. Borrow realism from public sources.**

This is a feature, not a compromise — it yields a reproducible, deterministic,
difficulty-controllable eval set, which no scraped dataset would provide.

## 2. Realism donors (what to borrow, not what to run on)

| Source | Borrow | URL |
|---|---|---|
| California Open Data — Purchase Order Data | Vendor names, UNSPSC commodity codes, realistic amount distributions | data.ca.gov |
| Northwind OData | Entity relationships, OData query semantics; usable as a stand-in backend | services.odata.org |
| SAP ES5 / GWSAMPLE_BASIC | SAP naming conventions, OData V2 response shapes, error payload format | sapes5.sapdevcenter.com (free registration) |
| Kaggle Procurement KPI dataset | Field ideas for the vendor-history tool | kaggle.com |

Note on ES5: it is sales-order shaped, not procurement, and it has intermittent
downtime. Use it to learn the dialect and copy conventions. Never let the demo
depend on it being up.

## 3. Exception taxonomy (the labels)

Every generated scenario carries a ground-truth label. This is the eval spine.

| Code | Name | Injected defect | Expected agent behaviour |
|---|---|---|---|
| `CLEAN` | No exception | none | Recognise as clean, propose release, no escalation |
| `PRICE_MINOR` | Price variance within tolerance | invoice unit price +1–3% | Release within tolerance, cite the tolerance rule |
| `PRICE_MAJOR` | Price variance outside tolerance | invoice unit price +8–25% | Do NOT release; propose price query to vendor |
| `QTY_OVER` | Invoiced qty > received qty | invoice qty exceeds GR sum | Propose partial payment to GR quantity |
| `GR_MISSING` | No goods receipt | GR absent, PO is GR-based | Insufficient info; escalate to requisitioner |
| `GR_PARTIAL` | Partial delivery | GR sum < PO qty, invoice matches GR | Correctly identify as valid partial — a trap case |
| `DUP_INVOICE` | Duplicate | second invoice, same vendor ref | Flag duplicate, propose reject — a trap case |
| `AMBIGUOUS` | Underdetermined | conflicting/missing evidence | Must say "I don't know" and escalate |

The trap cases matter most. `GR_PARTIAL` looks like `QTY_OVER` to a careless agent.
`AMBIGUOUS` tests whether it will admit uncertainty instead of confabulating a fix.
An agent that scores well on the easy cases and fails these is not shippable, and
saying that in the README is a stronger signal than a 100% score.

## 4. Generator design

```
generator/
  seed_master_data.py    # vendors, materials, plants — borrowed from CA/Northwind
  generate_scenarios.py  # emits labelled PO/GR/invoice triples
  scenarios.yaml         # distribution + seed, checked into git
```

Requirements:
- **Seeded.** Same seed → byte-identical output. Non-negotiable for CI.
- **Labels separate from data.** Ground truth lives in a sidecar the agent's tools
  cannot read. Leaking labels through the tool layer silently invalidates every eval.
- **Distribution configurable.** ~40% clean, ~60% spread across exception types.
  Real AP queues are mostly clean; an agent that cries wolf is useless.
- **SAP-flavoured field names.** `EBELN` (PO number), `EBELP` (PO item),
  `LIFNR` (vendor), `MATNR` (material), `MENGE` (quantity), `NETPR` (net price),
  `BELNR` (document number). Costs nothing, signals everything to an SAP reviewer.

## 5. Volume

- 200 scenarios total for the mock ERP
- 30–40 held out as the eval set, never used while developing prompts
- 8–10 hand-inspected "golden" cases you can walk through live in the demo

Resist generating 50,000 rows. Nobody is impressed by volume; they're impressed by
the eval set being adversarial and the trap cases being deliberate.

## 6. Open questions

- [ ] Model tolerances per vendor or globally? (Real SAP: per tolerance key.)
- [ ] Multi-line POs in v1, or single-line only? Multi-line makes matching much harder.
- [ ] Do we simulate currency differences? (Probably out of scope for v1.)
