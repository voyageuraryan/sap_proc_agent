# What the mock stands in for

Every endpoint in `mock_erp` is a stand-in for something real. This document
says what, how faithful the stand-in is, and — the part that matters — **what
would have to change if you pointed the agent at an actual S/4HANA system**.

> Honest framing up front: the ABAP and CDS below is illustrative. It has not
> been activated on a real system and I have not had access to one for this
> project. It is written to show that I know what the mock is imitating and
> where the imitation breaks, not to claim production experience I do not have.

---

## 1. Tables

The generator's models are shaped after SAP MM's actual tables, and the field
names on the wire are the real ones.

| Concept | SAP tables | Key fields the mock uses |
| --- | --- | --- |
| Purchase order | `EKKO` header, `EKPO` items | `EBELN`, `EBELP`, `LIFNR`, `WERKS`, `WAERS`, `AEDAT`, `MATNR`, `MENGE`, `NETPR` |
| Goods receipt | `MKPF` header, `MSEG` items | `MBLNR`, `BUDAT`, `EBELN`, `EBELP`, `MENGE` |
| Supplier invoice | `RBKP` header, `RSEG` items | `BELNR`, `BUZEI`, `LIFNR`, `XBLNR`, `BLDAT` |
| Supplier master | `LFA1` | `LIFNR`, `NAME1`, `LAND1` |
| Material master | `MARA`, valuation `MBEW` | `MATNR`, `MEINS`, `STPRS` |
| Tolerance keys | `T169G` (config, per company code) | price % / quantity %, by supplier |

Three modelling choices in the mock were deliberate rather than lazy:

**Goods receipts are flat rows, not header + items.** SAP's own OData exposes
the material-document *item* entity with the header keys repeated on every row,
and that is also the shape of the query the agent actually runs — "sum what was
received against this PO line". Modelling `MKPF` faithfully would have added a
join the agent never needs.

**Tolerances are composed at the response boundary, not stored on the PO.** In
SAP, tolerance keys are configuration per company code (transaction `OMR6`),
not fields on a document. Storing them on the PO would have made the mock
*wrong* in a way that taught the agent the wrong lesson — and worse, it would
have let the eval pass with an agent that never learned tolerance is
per-supplier.

**`block_reason` is a flattened invented field.** Real blocking is
`RBKP-ZLSPR` (a payment block key) plus the `RSEG` `SPGR*` indicators —
several fields, several meanings. The mock collapses them to one string, and
the values are deliberately *not* the eval's label names: SAP records that a
check failed (`QUANTITY_VARIANCE`), not your taxonomy (`QTY_OVER`). Making them
identical would have leaked the answer key into served data.

---

## 2. Endpoints

| Mock endpoint | Real S/4HANA equivalent | Faithful? |
| --- | --- | --- |
| `GET /A_PurchaseOrder('...')` | `API_PURCHASEORDER_PROCESS_SRV`, entity `A_PurchaseOrder` / `A_PurchaseOrderItem` | Entity set and key syntax match; the mock returns items inline rather than requiring `$expand` |
| `GET /A_MaterialDocumentItem?$filter=...` | `API_MATERIAL_DOCUMENT_SRV`, entity `A_MaterialDocumentItem` | Shape matches; the mock supports exactly one `$filter` form and rejects everything else explicitly |
| `GET /A_SupplierInvoice('...')` | `API_SUPPLIERINVOICE_PROCESS_SRV` | Entity matches; `block_reason` is invented (above) |
| `GET /VendorHistory?VendorID='...'` | **No standard API.** A custom CDS view + service | The honest one: this is bespoke, and the doc says so |
| `POST /ProposeCorrection` | **Not SAP at all** — a side table | Deliberate; see §4 |
| `POST /ApplyCorrection` | `BAPI_INCOMINGINVOICE_CHANGE`, or an invoice-park/post flow | The mock's amendment overlay stands in for a real posting |

The OData V2 dialect is imitated on purpose: `{"d": {...}}` and
`{"d": {"results": [...]}}` envelopes, and errors as
`{"error": {"code": ..., "message": {"lang": "en", "value": ...}}}`. That
envelope is exactly the thing that breaks a naively written client, so the
agent's `ErpClient` had to learn to unwrap it — which is the point.

Unimplemented query options are **rejected explicitly** rather than ignored.
`$top`, `$skip`, `$orderby` return `400 UNSUPPORTED_OPTION`. Silently ignoring
an option a caller sent is how an integration produces confidently wrong
results.

---

## 3. The one custom view

`VendorHistory` has no standard API, and it is the only route by which a
duplicate invoice is solvable — a duplicate is invisible from the invoice in
front of you. In a real system it would be a CDS view over `RBKP`, something
like:

```abap
@AbapCatalog.sqlViewName: 'ZPROCVENDHIST'
@AccessControl.authorizationCheck: #CHECK
@EndUserText.label: 'Supplier invoice history for AP exception handling'
@OData.publish: true
define view Z_C_VendorInvoiceHistory
  as select from I_SupplierInvoice as inv
  association [0..1] to I_Supplier as _Supplier
    on $projection.Supplier = _Supplier.Supplier
{
  key inv.SupplierInvoice           as SupplierInvoice,
      inv.FiscalYear                as FiscalYear,
      inv.InvoicingParty            as Supplier,
      inv.SupplierInvoiceIDByInvcgParty as SupplierInvoiceRef,   -- XBLNR
      inv.DocumentDate              as DocumentDate,
      inv.PaymentBlockingReason     as PaymentBlockingReason,    -- ZLSPR
      _Supplier
}
```

with a projection/service definition exposing it:

```abap
@EndUserText.label: 'Procurement exception service'
define service ZPROC_SRV {
  expose Z_C_VendorInvoiceHistory as VendorHistory;
  expose I_PurchaseOrderAPI01     as A_PurchaseOrder;
  expose I_MaterialDocumentItem   as A_MaterialDocumentItem;
  expose I_SupplierInvoiceAPI01   as A_SupplierInvoice;
}
```

**`@AccessControl.authorizationCheck: #CHECK` is the line that matters**, and it
is the single biggest gap between the mock and reality — see §5.

---

## 4. The proposal table is not SAP, and should not be

`ProposeCorrection` writes to a SQLite table this project owns. That is not a
limitation of the mock; it is the design.

A proposal is **not a business document**. It is a record that a machine
suggested something and a named human either agreed or did not. Putting it in
SAP would mean either abusing a standard object (parking an invoice you have no
intention of posting) or a Z-table with its own transport, authorisation object
and lifecycle — for data that belongs to the *agent platform*, not to the ERP.

Keeping it outside also means the guarantee survives the integration: the agent
can write to a table that has no path to a ledger, and the only thing that
crosses into SAP is a call a human already approved.

When the correction is finally applied, *that* is a real SAP write, and it
would be `BAPI_INCOMINGINVOICE_CHANGE` (or the invoice park/post flow) rather
than an amendment row. Everything upstream of it stays where it is.

---

## 5. What changes on a real system — the honest list

Ordered by how much they would hurt.

**Authorisation is the whole ballgame.** The mock has no auth at all. Real SAP
has authorisation objects (`M_RECH_*` for invoice verification, `M_BEST_*` for
purchasing), and the agent would need a technical user whose profile grants
*display only*. That is the real-world form of "the agent cannot write" — and
it is stronger than mine, because it is enforced by the system of record rather
than by my tool registry. My registry check would become defence in depth
rather than the mechanism.

**Rate limits and quotas.** S/4HANA Cloud APIs are throttled per tenant. A
200-scenario eval would need backoff and probably a batch (`$batch`) request
shape. The current client retries nothing.

**Data is far messier.** Multi-line POs where one line is fine and another is
not. Partial deliveries spread across five receipts, some reversed. Free-text
supplier names with three spellings. Unit-of-measure conversions between order
unit and base unit — which the mock does not model at all and which is a
genuine source of "over-invoicing" that is not over-invoicing. My tolerance
comparison assumes one unit.

**Currency.** The mock is USD-only, header-level. Real invoices carry document
currency plus local currency plus an exchange rate at posting date, and a price
variance can be an FX artefact rather than a supplier error.

**Fiscal year is part of the key.** `RBKP` is keyed on `BELNR` *plus* `GJAHR`.
The mock treats the invoice number as unique, which is fine for 212 synthetic
documents and wrong for a real system at year end.

**Latency and pagination.** A `$filter` over a real `MSEG` returns pages, not a
list. The agent's `get_goods_receipts` would need to follow `__next` links, and
a PO line with 40 receipts would blow through the context window — which turns
into a summarisation decision the current design does not make.

**Change documents.** Real SAP records who changed what (`CDHDR`/`CDPOS`). A
production version of this should write its approval trail there too, so the
audit is in the place auditors already look rather than in a side table only
this system knows about.

---

## 6. What would *not* change

Worth stating, because it is the argument for the architecture:

- The agent's tool schemas. They describe documents, not tables.
- The loop, the terminal-tool contract, the iteration cap.
- The approval state machine, the payload hash, the review UI.
- The eval harness, the scoring tables, the safety gates.
- `ErpClient` — it already speaks HTTP to an OData V2 dialect and unwraps the
  envelope. Pointing it at a real service is a base URL and an auth header.

That is what the decoupling was for. The agent does not import `erp_domain`
precisely so that this list is short — and it is short because every one of
those components was built against a *contract* rather than against a database.
