"""The review app end to end: two real FastAPI apps talking HTTP to each other.

The load-bearing tests are the ones that watch the invoice quantity through
the whole flow. A UI that can approve is a UI that can write, so the gate has
to be re-proved from this side and not assumed from the API tests.
"""

import pytest
from conftest import INVOICE, PO, QUANTITY_PAYLOAD, form_hash
from review_ui.client import ReviewClient, ReviewError
from review_ui.settings import ODATA_PREFIX

# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------


def test_the_root_redirects_to_the_queue(ui):
    response = ui.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/queue"


def test_an_empty_queue_says_what_to_do(ui):
    body = ui.get("/queue").text
    assert "Nothing here" in body
    assert "Run the agent" in body


def test_a_proposal_appears_in_the_queue(ui, propose):
    proposal_id = propose()
    body = ui.get("/queue").text
    assert proposal_id in body
    assert INVOICE in body
    assert "PROPOSED" in body


def test_the_queue_filters_by_status(ui, propose):
    proposal_id = propose()
    assert proposal_id in ui.get("/queue?status=PROPOSED").text
    assert proposal_id not in ui.get("/queue?status=APPLIED").text


def test_the_counts_stay_true_while_a_filter_is_on(ui, propose):
    """A count that lies about how much work is waiting is worse than no count."""
    propose()
    body = ui.get("/queue?status=APPLIED").text
    assert "Proposed (1)" in body
    assert "All (1)" in body


def test_a_nonsense_status_falls_back_to_everything(ui, propose):
    proposal_id = propose()
    assert proposal_id in ui.get("/queue?status=BANANA").text


def test_the_queue_renders_when_the_erp_is_unreachable(review_settings):
    """A reviewer needs to be told the ERP is down, not shown a 500."""
    from fastapi.testclient import TestClient
    from review_ui.app import create_app

    class Dead(ReviewClient):
        def __init__(self):
            pass

        def list_proposals(self, status=None):
            raise ReviewError("ERP_UNREACHABLE", "connection refused", 503)

    body = TestClient(create_app(review_settings, client=Dead())).get("/queue").text
    assert "ERP_UNREACHABLE" in body
    assert "connection refused" in body


# ---------------------------------------------------------------------------
# the detail page: can the reviewer disagree?
# ---------------------------------------------------------------------------


def test_the_detail_page_shows_the_change_and_the_evidence(ui, propose):
    body = ui.get(f"/proposals/{propose()}").text

    # the change
    assert "14.000" in body
    assert "13.000" in body
    # the agent's argument
    assert "Receipts total 13.000" in body
    # the evidence, unsummarised: PO, receipt document, tolerance, supplier
    assert PO in body
    assert "5000000901" in body
    assert "5.0%" in body
    assert "1000000010" in body
    assert "QUANTITY_VARIANCE" in body
    # and the audit trail
    assert "Payload hash" in body


def test_the_detail_page_offers_approve_and_reject_while_proposed(ui, propose):
    body = ui.get(f"/proposals/{propose()}").text
    assert "Approve" in body
    assert "Reject" in body
    assert "Apply to the invoice" not in body


def test_an_unknown_proposal_does_not_500(ui):
    body = ui.get("/proposals/PR-999999").text
    assert "PROPOSAL_NOT_FOUND" in body


def test_the_page_warns_when_the_document_has_moved(ui, propose, erp):
    """Applied by another route, so the from_quantity no longer matches."""
    first = propose()
    page = ui.get(f"/proposals/{first}")
    hash_ = form_hash(page.text)
    ui.post(f"/proposals/{first}/approve", data={"reviewer": "a@b.c", "payload_hash": hash_})
    ui.post(f"/proposals/{first}/apply", data={"payload_hash": hash_})

    stale = propose()  # same from_quantity 14.000, but the invoice is now 13.000
    body = ui.get(f"/proposals/{stale}").text
    assert "no longer holds" in body
    assert "no longer current" in body


# ---------------------------------------------------------------------------
# THE GATE, from the UI side
# ---------------------------------------------------------------------------


def test_approving_changes_nothing(ui, propose, invoice_quantity):
    proposal_id = propose()
    assert invoice_quantity() == "14.000"

    page = ui.get(f"/proposals/{proposal_id}")
    response = ui.post(
        f"/proposals/{proposal_id}/approve",
        data={"reviewer": "ap.supervisor@example.com", "payload_hash": form_hash(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303  # POST-then-redirect
    assert invoice_quantity() == "14.000"
    assert "APPROVED" in ui.get(f"/proposals/{proposal_id}").text


def test_only_applying_changes_the_document(ui, propose, invoice_quantity):
    proposal_id = propose()
    page = ui.get(f"/proposals/{proposal_id}")
    hash_ = form_hash(page.text)

    ui.post(
        f"/proposals/{proposal_id}/approve",
        data={"reviewer": "ap.supervisor@example.com", "payload_hash": hash_},
    )
    assert invoice_quantity() == "14.000"

    ui.post(f"/proposals/{proposal_id}/apply", data={"payload_hash": hash_})
    assert invoice_quantity() == "13.000"

    final = ui.get(f"/proposals/{proposal_id}").text
    assert "APPLIED" in final
    assert "ap.supervisor@example.com" in final


def test_applying_before_approval_is_refused_and_says_why(ui, propose, invoice_quantity):
    proposal_id = propose()
    page = ui.get(f"/proposals/{proposal_id}")
    response = ui.post(
        f"/proposals/{proposal_id}/apply",
        data={"payload_hash": form_hash(page.text)},
        follow_redirects=False,
    )
    assert "ILLEGAL_TRANSITION" in response.headers["location"]
    assert invoice_quantity() == "14.000"
    # The error travels in the redirect target, so follow it: re-fetching the
    # bare URL would drop it, which is also the correct behaviour -- the
    # message belongs to that one attempt, not to the proposal.
    landed = ui.get(response.headers["location"])
    assert "ILLEGAL_TRANSITION" in landed.text
    assert "PROPOSED" in landed.text


def test_rejecting_is_terminal_and_touches_nothing(ui, propose, invoice_quantity):
    proposal_id = propose()
    page = ui.get(f"/proposals/{proposal_id}")
    ui.post(
        f"/proposals/{proposal_id}/reject",
        data={
            "reason": "The receipt is being chased with the supplier.",
            "payload_hash": form_hash(page.text),
        },
    )
    body = ui.get(f"/proposals/{proposal_id}").text
    assert "REJECTED" in body
    assert "being chased" in body
    assert "Approve" not in body  # no way back
    assert invoice_quantity() == "14.000"


def test_a_page_drawn_before_the_proposal_changed_cannot_act(ui, propose, invoice_quantity):
    """You approved what you were shown.

    Without this, a reviewer with a stale tab could approve a payload they
    never read -- which is exactly the failure the whole gate exists to
    prevent, arriving through the human instead of the agent.
    """
    proposal_id = propose()
    response = ui.post(
        f"/proposals/{proposal_id}/approve",
        data={"reviewer": "a@b.c", "payload_hash": "0" * 64},
        follow_redirects=False,
    )
    assert "STALE_PAGE" in response.headers["location"]
    assert invoice_quantity() == "14.000"
    assert "PROPOSED" in ui.get(f"/proposals/{proposal_id}").text


@pytest.mark.parametrize("action", ["approve", "reject", "apply"])
def test_every_action_checks_the_hash(ui, propose, action):
    proposal_id = propose()
    data = {"payload_hash": "0" * 64, "reviewer": "a@b.c", "reason": "no"}
    response = ui.post(f"/proposals/{proposal_id}/{action}", data=data, follow_redirects=False)
    assert "STALE_PAGE" in response.headers["location"]


def test_the_ui_sends_the_payload_back_for_the_erp_to_hash_check(ui, propose, erp):
    """This app gets no shortcut past the ERP's own check. If it invented a
    payload, ApplyCorrection would refuse it."""
    proposal_id = propose()
    hash_ = form_hash(ui.get(f"/proposals/{proposal_id}").text)
    ui.post(f"/proposals/{proposal_id}/approve", data={"reviewer": "a@b.c", "payload_hash": hash_})

    tampered = dict(QUANTITY_PAYLOAD, to_quantity="1.000")
    refused = erp.post(
        f"{ODATA_PREFIX}/ApplyCorrection",
        json={"proposal_id": proposal_id, "payload": tampered},
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "PAYLOAD_MISMATCH"


# ---------------------------------------------------------------------------
# separation of powers
# ---------------------------------------------------------------------------


def test_the_agents_client_still_cannot_approve():
    """The review UI needed approve/reject/apply, so it got its OWN client.

    Bolting them onto ErpClient would have been less code and would have
    erased the distinction this whole project demonstrates.
    """
    from agent.erp_client import ErpClient

    agent_methods = {name for name in dir(ErpClient) if not name.startswith("_")}
    review_methods = {name for name in dir(ReviewClient) if not name.startswith("_")}

    human_only = {"approve", "reject", "apply"}
    assert human_only <= review_methods
    assert not (human_only & agent_methods)


def test_the_agent_has_no_tool_for_anything_the_ui_does():
    """Checked from the tool registry, which is what the model actually sees."""
    from agent.erp_client import ErpClient
    from agent.tools import build_tools

    tools = set(build_tools(ErpClient("http://unused")))
    for verb in ("approve", "reject", "apply"):
        assert not any(verb in name for name in tools), verb


def test_the_review_app_exposes_no_odata_read_path_of_its_own(ui):
    """It is a client of the ERP, not a second door into it."""
    paths = set(ui.app.openapi()["paths"])
    assert not any(path.startswith(ODATA_PREFIX) for path in paths)
    assert paths == {
        "/healthz",
        "/queue",
        "/proposals/{proposal_id}",
        "/proposals/{proposal_id}/approve",
        "/proposals/{proposal_id}/reject",
        "/proposals/{proposal_id}/apply",
    }


def test_health_reports_the_erp_not_merely_itself(ui):
    """A green light that only proves the UI booted sends someone hunting in
    the wrong place."""
    assert ui.get("/healthz").json() == {"ok": True, "erp": "reachable"}


def test_health_goes_red_when_the_erp_is_down(review_settings):
    from fastapi.testclient import TestClient
    from review_ui.app import create_app

    class Dead(ReviewClient):
        def __init__(self):
            pass

        def list_proposals(self, status=None):
            raise ReviewError("ERP_UNREACHABLE", "connection refused", 503)

    body = TestClient(create_app(review_settings, client=Dead())).get("/healthz").json()
    assert body == {"ok": False, "erp": "unreachable", "code": "ERP_UNREACHABLE"}


def test_the_page_says_it_is_not_authentication(ui, propose):
    """The identity here is whatever the form said. Saying so on screen is
    the honest thing; a login box that checks nothing would be worse."""
    assert "not authenticated" in ui.get("/queue").text


def test_no_javascript_is_served(ui, propose):
    """Zero JS means no build step, no CDN, and it renders offline."""
    body = ui.get(f"/proposals/{propose()}").text
    assert "<script" not in body.lower()
    assert "src=" not in body.lower()
