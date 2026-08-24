"""The approval gate: does it actually hold?

This suite is the proof behind "zero writes possible without a recorded
approval -- proven by eval, not assertion" in the definition of done. It is
organised in three layers:

  1. hashing        -- the integrity primitive, no DB, no HTTP
  2. repository     -- the transition matrix and the three attacks, no HTTP
  3. HTTP           -- the endpoints, the amendment overlay, and the structural
                       claim that the agent-facing surface has no approve route

The layering matters when something breaks: a failure in layer 1 means the
integrity primitive is wrong, layer 2 means the state machine is wrong, and
only layer 3 means the wiring is wrong.

CONTRACT this suite assumes (build toward it):

  ProposalRepository(conn, store)
      .create(invoice_number, payload, agent_reasoning, scenario_id=None) -> Proposal
      .get(proposal_id)                                  -> Proposal
      .approve(proposal_id, approved_by)                  -> Proposal
      .reject(proposal_id, rejected_by, reason)           -> Proposal
      .apply(proposal_id, payload)                        -> AppliedResult
      .amendments_for_invoice(invoice_number)             -> list[AppliedAmendment]
      .list_proposals(status=None)                        -> list[Proposal]

  The repository is handed the store; it does NOT load it itself.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mock_erp.app import create_app
from mock_erp.db import open_db
from mock_erp.proposals import (
    ALLOWED_TRANSITIONS,
    AmendQuantityPayload,
    ProposalAction,
    ProposalStatus,
    canonical_json,
    payload_hash,
)
from mock_erp.repository import ProposalError, ProposalRepository
from mock_erp.settings import Settings
from mock_erp.store import load_store

ODATA = "/sap/opu/odata/sap/ZPROC_SRV"
APPROVAL = "/approval"


# ==========================================================================
# fixtures
# ==========================================================================


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "erp" / "invoices.json").exists():
            return parent
    raise RuntimeError("repo root not found -- run `uv run generator` first")


@pytest.fixture(scope="session")
def erp_dir() -> Path:
    return repo_root() / "data" / "erp"


@pytest.fixture(scope="session")
def raw_invoices(erp_dir: Path) -> list[dict]:
    return json.loads((erp_dir / "invoices.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def target(raw_invoices: list[dict]) -> dict:
    """One real invoice line to amend, taken from the generated data.

    Reading the real data rather than hardcoding numbers means the suite keeps
    working when the dataset is regenerated with a different seed.
    """
    inv = raw_invoices[0]
    line = inv["items"][0]
    return {
        "invoice_number": inv["BELNR"],
        "inv_item_number": line["BUZEI"],
        "current_quantity": line["MENGE"],
    }


def make_payload(target: dict, to_quantity: str | None = None) -> AmendQuantityPayload:
    """A well-formed amendment against the real current document state.

    from_quantity MUST be the document's current value or apply() should refuse
    it as stale -- see test_stale_proposal_is_refused.
    """
    current = Decimal(target["current_quantity"])
    return AmendQuantityPayload(
        correction_type="AMEND_INVOICE_QUANTITY",
        invoice_number=target["invoice_number"],
        inv_item_number=target["inv_item_number"],
        from_quantity=target["current_quantity"],
        to_quantity=to_quantity or str(current - Decimal("1.000")),
    )


@pytest.fixture
def repo(tmp_path: Path, erp_dir: Path) -> ProposalRepository:
    """A fresh repository per test, over its own SQLite file.

    Function-scoped on purpose: state machine tests mutate state, and a shared
    DB would make them order-dependent -- which is the single most common way a
    test suite becomes untrustworthy.
    """
    conn = open_db(tmp_path / "approvals.sqlite3")
    store = load_store(erp_dir)
    return ProposalRepository(conn, store)


@pytest.fixture
def client(tmp_path: Path, erp_dir: Path) -> TestClient:
    app = create_app(
        Settings(erp_data_dir=erp_dir, db_path=tmp_path / "approvals.sqlite3")
    )
    with TestClient(app) as c:
        yield c


# ==========================================================================
# LAYER 1 -- the integrity primitive
# ==========================================================================


def test_hash_is_stable_across_identical_payloads(target: dict):
    """Two separately constructed but equal payloads must hash identically.

    If they do not, apply() can never match an approval and the gate blocks
    everything -- which looks like security and is actually a broken system.
    """
    assert payload_hash(make_payload(target)) == payload_hash(make_payload(target))


def test_hash_ignores_keyword_order(target: dict):
    """Field order at construction must not affect the hash.

    This is what sort_keys=True in canonical_json buys: the propose call and the
    apply call build the payload in different code paths, and dict insertion
    order will differ.
    """
    a = AmendQuantityPayload(
        correction_type="AMEND_INVOICE_QUANTITY",
        invoice_number=target["invoice_number"],
        inv_item_number=target["inv_item_number"],
        from_quantity=target["current_quantity"],
        to_quantity="1.000",
    )
    b = AmendQuantityPayload(
        to_quantity="1.000",
        from_quantity=target["current_quantity"],
        inv_item_number=target["inv_item_number"],
        invoice_number=target["invoice_number"],
        correction_type="AMEND_INVOICE_QUANTITY",
    )
    assert payload_hash(a) == payload_hash(b)


def test_hash_is_byte_sensitive_not_value_sensitive(target: dict):
    """"1.0" and "1.000" are the same NUMBER and must be different HASHES.

    This confirms the hash is over canonical bytes, not over semantics. It is
    also why payload numeric fields are strings: if they were Decimal or float,
    the same logical amount could serialise two ways and the integrity check
    would become a coin flip.
    """
    a = make_payload(target, to_quantity="1.0")
    b = make_payload(target, to_quantity="1.000")
    assert payload_hash(a) != payload_hash(b)


def test_canonical_json_has_no_incidental_whitespace(target: dict):
    """Compact separators: any whitespace variation would change the hash."""
    raw = canonical_json(make_payload(target))
    assert isinstance(raw, bytes)
    assert b", " not in raw
    assert b": " not in raw


def test_canonical_json_keys_are_sorted(target: dict):
    decoded = json.loads(canonical_json(make_payload(target)))
    assert list(decoded) == sorted(decoded)


# ==========================================================================
# LAYER 2 -- the state machine
# ==========================================================================


def test_allowed_transitions_is_exactly_three_entries():
    """The entire write policy, auditable by reading three lines.

    This is an allow-list: everything absent is refused, including attacks
    nobody has thought of yet. If this set ever grows, someone widened the
    write policy and that should be a deliberate, reviewed act.
    """
    assert ALLOWED_TRANSITIONS == {
        (ProposalStatus.PROPOSED, ProposalAction.APPROVE): ProposalStatus.APPROVED,
        (ProposalStatus.PROPOSED, ProposalAction.REJECT): ProposalStatus.REJECTED,
        (ProposalStatus.APPROVED, ProposalAction.APPLY): ProposalStatus.APPLIED,
    }


def test_create_persists_a_proposed_proposal(repo: ProposalRepository, target: dict):
    p = repo.create(
        invoice_number=target["invoice_number"],
        payload=make_payload(target),
        agent_reasoning="GR posted less than was invoiced",
        scenario_id="SC-0001",
    )
    assert p.status is ProposalStatus.PROPOSED
    assert p.proposal_id
    assert p.payload_hash == payload_hash(make_payload(target))
    assert p.proposed_at is not None
    assert p.approved_by is None and p.approved_hash is None

    # It must be readable back -- create has to actually write to SQLite.
    assert repo.get(p.proposal_id).proposal_id == p.proposal_id


def test_create_rejects_an_unknown_invoice(repo: ProposalRepository, target: dict):
    """Validated against the store, not blindly trusted from the request body."""
    payload = make_payload(target)
    with pytest.raises(ProposalError) as exc:
        repo.create(
            invoice_number="5199999999",
            payload=payload,
            agent_reasoning="x",
            scenario_id=None,
        )
    assert exc.value.status == 404 or exc.value.status == 400


def test_get_unknown_proposal_raises(repo: ProposalRepository):
    with pytest.raises(ProposalError) as exc:
        repo.get("PR-999999")
    assert exc.value.code == "PROPOSAL_NOT_FOUND"
    assert exc.value.status == 404


def test_approve_records_who_when_and_the_hash_seen(
    repo: ProposalRepository, target: dict
):
    """approved_hash freezes the exact bytes the human was shown.

    It is stored separately from payload_hash so that apply() compares "what I
    am about to write" against "what she approved". Reading both from the same
    field would make the comparison tautological -- a check that cannot fail.
    """
    p = repo.create(target["invoice_number"], make_payload(target), "why", None)
    approved = repo.approve(p.proposal_id, "aryan@ap-team")

    assert approved.status is ProposalStatus.APPROVED
    assert approved.approved_by == "aryan@ap-team"
    assert approved.approved_at is not None
    assert approved.approved_hash == p.payload_hash

    # persisted, not just returned
    assert repo.get(p.proposal_id).status is ProposalStatus.APPROVED


def test_reject_is_terminal(repo: ProposalRepository, target: dict):
    p = repo.create(target["invoice_number"], make_payload(target), "why", None)
    rejected = repo.reject(p.proposal_id, "aryan@ap-team", "vendor to be queried")

    assert rejected.status is ProposalStatus.REJECTED
    assert rejected.rejection_reason == "vendor to be queried"

    with pytest.raises(ProposalError) as exc:
        repo.apply(p.proposal_id, make_payload(target))
    assert exc.value.code == "ILLEGAL_TRANSITION"


def test_apply_after_approval_records_one_amendment(
    repo: ProposalRepository, target: dict
):
    payload = make_payload(target)
    p = repo.create(target["invoice_number"], payload, "why", None)
    repo.approve(p.proposal_id, "aryan@ap-team")

    result = repo.apply(p.proposal_id, payload)

    assert result.status is ProposalStatus.APPLIED
    assert result.already_applied is False
    assert result.applied_at is not None

    amendments = repo.amendments_for_invoice(target["invoice_number"])
    assert len(amendments) == 1
    assert amendments[0].proposal_id == p.proposal_id


# ------------------------- ATTACK 1: skip approval -------------------------


def test_apply_without_approval_is_refused(repo: ProposalRepository, target: dict):
    """A prompt injection can make the agent ASK. It cannot make the ERP COMPLY.

    Note the second assertion: it is not enough that the API said no. Nothing
    may have been written.
    """
    payload = make_payload(target)
    p = repo.create(target["invoice_number"], payload, "why", None)

    with pytest.raises(ProposalError) as exc:
        repo.apply(p.proposal_id, payload)

    assert exc.value.code == "ILLEGAL_TRANSITION"
    assert exc.value.status == 409
    assert repo.amendments_for_invoice(target["invoice_number"]) == []
    assert repo.get(p.proposal_id).status is ProposalStatus.PROPOSED


# ------------------------- ATTACK 2: payload swap -------------------------


def test_apply_with_a_swapped_payload_is_refused(
    repo: ProposalRepository, target: dict
):
    """Approve a small change, attempt to apply a large one.

    Without the hash check this passes the status test, the write executes, and
    the audit log says "approved by aryan@ap-team". A fraudulent payment with a
    clean audit trail is worse than an unapproved one, because nobody will ever
    look at it.

    Approval of A payload is not approval of ANY payload.
    """
    small = make_payload(target, to_quantity="1.000")
    large = make_payload(target, to_quantity="9999.000")

    p = repo.create(target["invoice_number"], small, "small correction", None)
    repo.approve(p.proposal_id, "aryan@ap-team")

    with pytest.raises(ProposalError) as exc:
        repo.apply(p.proposal_id, large)

    assert exc.value.code == "PAYLOAD_MISMATCH"
    assert exc.value.status == 409
    assert repo.amendments_for_invoice(target["invoice_number"]) == []
    assert repo.get(p.proposal_id).status is ProposalStatus.APPROVED


def test_hash_is_compared_against_the_approved_hash(
    repo: ProposalRepository, target: dict
):
    """The comparison must use approved_hash, not payload_hash.

    Comparing against payload_hash compares the stored payload with itself and
    can never fail. This test cannot detect that directly, so it asserts the
    field is populated at approval time and equals the payload hash -- the
    invariant the comparison depends on.
    """
    payload = make_payload(target)
    p = repo.create(target["invoice_number"], payload, "why", None)
    approved = repo.approve(p.proposal_id, "aryan@ap-team")
    assert approved.approved_hash is not None
    assert approved.approved_hash == payload_hash(payload)


# ------------------------- ATTACK 3: replay -------------------------


def test_apply_twice_writes_once(repo: ProposalRepository, target: dict):
    """A retry must be safe. The amendment log is the source of truth.

    Idempotency is a property of the storage (UNIQUE on proposal_id), not of
    the response code -- so even a race past the status check writes once.
    """
    payload = make_payload(target)
    p = repo.create(target["invoice_number"], payload, "why", None)
    repo.approve(p.proposal_id, "aryan@ap-team")

    first = repo.apply(p.proposal_id, payload)
    second = repo.apply(p.proposal_id, payload)

    assert first.already_applied is False
    assert second.already_applied is True
    assert second.status is ProposalStatus.APPLIED
    assert len(repo.amendments_for_invoice(target["invoice_number"])) == 1


def test_replay_with_a_different_payload_is_still_refused(
    repo: ProposalRepository, target: dict
):
    """An APPLIED proposal is not a licence to write something else."""
    payload = make_payload(target, to_quantity="1.000")
    p = repo.create(target["invoice_number"], payload, "why", None)
    repo.approve(p.proposal_id, "aryan@ap-team")
    repo.apply(p.proposal_id, payload)

    with pytest.raises(ProposalError) as exc:
        repo.apply(p.proposal_id, make_payload(target, to_quantity="9999.000"))
    # ILLEGAL_TRANSITION, not PAYLOAD_MISMATCH: apply() checks status before the
    # hash, and APPLIED is terminal. Either code refuses the write; this one is
    # the more truthful reason.
    assert exc.value.code == "ILLEGAL_TRANSITION"
    assert len(repo.amendments_for_invoice(target["invoice_number"])) == 1


# ------------------- the whole illegal-transition matrix -------------------


def _reach(repo: ProposalRepository, target: dict, status: ProposalStatus) -> str:
    """Drive a fresh proposal into `status` and return its id."""
    payload = make_payload(target)
    p = repo.create(target["invoice_number"], payload, "why", None)
    if status is ProposalStatus.PROPOSED:
        return p.proposal_id
    if status is ProposalStatus.REJECTED:
        repo.reject(p.proposal_id, "aryan@ap-team", "no")
        return p.proposal_id
    repo.approve(p.proposal_id, "aryan@ap-team")
    if status is ProposalStatus.APPROVED:
        return p.proposal_id
    repo.apply(p.proposal_id, payload)
    return p.proposal_id


# (APPLIED, APPLY) is excluded on purpose: replaying the same payload against
# an already-applied proposal is the safe-retry path and must return rather than
# raise. It is covered by test_apply_twice_writes_once, and the different-payload
# variant by test_replay_with_a_different_payload_is_still_refused.
IDEMPOTENT = {(ProposalStatus.APPLIED, ProposalAction.APPLY)}

ILLEGAL = [
    (status, action)
    for status in ProposalStatus
    for action in ProposalAction
    if (status, action) not in ALLOWED_TRANSITIONS and (status, action) not in IDEMPOTENT
]


@pytest.mark.parametrize(("status", "action"), ILLEGAL)
def test_every_illegal_transition_is_refused(
    repo: ProposalRepository,
    target: dict,
    status: ProposalStatus,
    action: ProposalAction,
):
    """Derived from ALLOWED_TRANSITIONS, so it covers the complement exactly.

    Enumerating the complement rather than hand-picking four attacks is the
    point: the claim being tested is "ONLY the allow-listed moves work", which
    is strictly stronger than "these attacks fail".

    Add a state or an action and this test grows automatically.
    """
    pid = _reach(repo, target, status)
    payload = make_payload(target)

    with pytest.raises(ProposalError) as exc:
        if action is ProposalAction.APPROVE:
            repo.approve(pid, "aryan@ap-team")
        elif action is ProposalAction.REJECT:
            repo.reject(pid, "aryan@ap-team", "no")
        else:
            repo.apply(pid, payload)

    assert exc.value.code in {"ILLEGAL_TRANSITION", "PAYLOAD_MISMATCH"}
    assert exc.value.status == 409


# ------------------------- staleness -------------------------


def test_stale_proposal_is_refused(repo: ProposalRepository, target: dict):
    """The from_ value must still match the document at apply time.

    If the document moved after the human looked at it, she approved a change
    premised on facts that no longer hold. Optimistic concurrency control, for
    the price of one field.
    """
    stale = AmendQuantityPayload(
        correction_type="AMEND_INVOICE_QUANTITY",
        invoice_number=target["invoice_number"],
        inv_item_number=target["inv_item_number"],
        from_quantity="99999.000",  # not the document's current value
        to_quantity="1.000",
    )
    p = repo.create(target["invoice_number"], stale, "why", None)
    repo.approve(p.proposal_id, "aryan@ap-team")

    with pytest.raises(ProposalError) as exc:
        repo.apply(p.proposal_id, stale)
    assert exc.value.code == "STALE_PROPOSAL"
    assert repo.amendments_for_invoice(target["invoice_number"]) == []


def test_list_proposals_filters_by_status(repo: ProposalRepository, target: dict):
    a = repo.create(target["invoice_number"], make_payload(target), "a", None)
    b = repo.create(target["invoice_number"], make_payload(target, "2.000"), "b", None)
    repo.approve(b.proposal_id, "aryan@ap-team")

    assert len(repo.list_proposals()) == 2
    proposed = repo.list_proposals(ProposalStatus.PROPOSED)
    assert [p.proposal_id for p in proposed] == [a.proposal_id]


# ==========================================================================
# LAYER 3 -- over HTTP
# ==========================================================================


def _propose(client: TestClient, target: dict, to_quantity: str | None = None):
    payload = make_payload(target, to_quantity).model_dump(mode="json")
    return client.post(
        f"{ODATA}/ProposeCorrection",
        json={
            "invoice_number": target["invoice_number"],
            "payload": payload,
            "agent_reasoning": "goods receipt is short of the invoiced quantity",
            "scenario_id": "SC-0001",
        },
    )


def test_propose_over_http_returns_id_and_hash(client: TestClient, target: dict):
    resp = _propose(client, target)
    assert resp.status_code in (200, 201)
    body = resp.json()["d"]
    assert body["status"] == "PROPOSED"
    assert body["proposal_id"]
    # Returned so the agent can construct a valid apply call, and so the
    # integrity mechanism is visible in the trace rather than hidden.
    assert body["payload_hash"] == payload_hash(make_payload(target))


def test_the_agent_surface_has_no_approval_capability(client: TestClient):
    """The architectural claim, encoded as a test.

    Approval is not "a tool the agent is told not to use" -- it is a tool that
    does not exist on the agent-facing prefix. This inspects the routing table
    rather than probing one URL, so it cannot be satisfied by a 404 that
    happens to be right for the wrong reason.
    """
    # Read the published OpenAPI surface rather than app.routes: include_router
    # may wrap children in a router object, so app.routes does not reliably
    # expose child paths. The OpenAPI schema is the contract a client sees.
    paths = client.app.openapi()["paths"]
    agent_paths = [p for p in paths if p.startswith(ODATA)]
    assert agent_paths, "no OData routes registered at all"
    for path in agent_paths:
        assert "approve" not in path.lower()
        assert "reject" not in path.lower()


def test_full_gate_walkthrough_changes_the_invoice(client: TestClient, target: dict):
    """The end-to-end demo, as an assertion.

    Before approval the invoice reads its original quantity. After approve and
    apply, the SAME URL reads the amended one. That observable change is what
    makes "no write happened" a testable claim rather than a promise.
    """
    url = f"{ODATA}/A_SupplierInvoice('{target['invoice_number']}')"
    original = target["current_quantity"]
    amended = str(Decimal(original) - Decimal("1.000"))

    def served_quantity() -> str:
        body = client.get(url).json()["d"]
        return next(
            i["MENGE"]
            for i in body["items"]
            if i["BUZEI"] == target["inv_item_number"]
        )

    assert served_quantity() == original

    proposal_id = _propose(client, target).json()["d"]["proposal_id"]
    payload = make_payload(target).model_dump(mode="json")

    # 1. apply before approval -- refused, and the document does not move
    refused = client.post(
        f"{ODATA}/ApplyCorrection",
        json={"proposal_id": proposal_id, "payload": payload},
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ILLEGAL_TRANSITION"
    assert served_quantity() == original

    # 2. a human approves
    approved = client.post(
        f"{APPROVAL}/proposals/{proposal_id}/approve",
        json={"approved_by": "aryan@ap-team"},
    )
    assert approved.status_code == 200
    assert served_quantity() == original, "approval alone must not write"

    # 3. now apply succeeds
    applied = client.post(
        f"{ODATA}/ApplyCorrection",
        json={"proposal_id": proposal_id, "payload": payload},
    )
    assert applied.status_code == 200

    # 4. the overlay is visible on the read endpoint
    assert served_quantity() == amended


def test_swapped_payload_over_http_is_refused(client: TestClient, target: dict):
    proposal_id = _propose(client, target, to_quantity="1.000").json()["d"]["proposal_id"]
    client.post(
        f"{APPROVAL}/proposals/{proposal_id}/approve",
        json={"approved_by": "aryan@ap-team"},
    )
    resp = client.post(
        f"{ODATA}/ApplyCorrection",
        json={
            "proposal_id": proposal_id,
            "payload": make_payload(target, "9999.000").model_dump(mode="json"),
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "PAYLOAD_MISMATCH"


def test_proposal_id_is_not_sql_injectable(client: TestClient, target: dict):
    """proposal_id arrives in a body the agent controls.

    If it is interpolated into SQL rather than bound as a parameter, a
    prompt-injected agent can approve its own proposal -- and the entire gate
    is bypassed by a string. The whole point of Step 5 fails on one f-string.
    """
    proposal_id = _propose(client, target).json()["d"]["proposal_id"]
    attack = "x'; UPDATE proposals SET status='APPROVED'; --"

    client.post(
        f"{ODATA}/ApplyCorrection",
        json={
            "proposal_id": attack,
            "payload": make_payload(target).model_dump(mode="json"),
        },
    )

    # Whatever the response, the real proposal must be untouched and the
    # proposals table must still exist and be queryable.
    listed = client.get(f"{APPROVAL}/proposals")
    assert listed.status_code == 200
    body = listed.json()
    rows = body["d"]["results"] if "d" in body else body
    match = [r for r in rows if r["proposal_id"] == proposal_id]
    assert match and match[0]["status"] == "PROPOSED"


def test_amendments_do_not_leak_into_purchase_orders(client: TestClient, target: dict):
    """Corrections amend invoices only.

    A purchase order and a goods receipt are historical facts -- you do not
    retroactively change what was ordered or what arrived.
    """
    proposal_id = _propose(client, target).json()["d"]["proposal_id"]
    client.post(
        f"{APPROVAL}/proposals/{proposal_id}/approve",
        json={"approved_by": "aryan@ap-team"},
    )
    client.post(
        f"{ODATA}/ApplyCorrection",
        json={
            "proposal_id": proposal_id,
            "payload": make_payload(target).model_dump(mode="json"),
        },
    )
    po_number = next(
        i["EBELN"]
        for i in client.get(
            f"{ODATA}/A_SupplierInvoice('{target['invoice_number']}')"
        ).json()["d"]["items"]
    )
    po = client.get(f"{ODATA}/A_PurchaseOrder('{po_number}')").json()["d"]
    assert "amendment" not in json.dumps(po).lower()
