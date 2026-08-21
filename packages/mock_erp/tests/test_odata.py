"""Unit tests for the OData dialect layer.

odata.py imports nothing from the project, so these tests need no data, no app
and no HTTP client. That is the payoff of keeping the dialect free of domain
knowledge -- the trickiest logic in the service is also the cheapest to test.
"""

from __future__ import annotations

import pytest
from mock_erp.odata import (
    ODataError,
    collection,
    entity,
    error_body,
    parse_po_filter,
    reject_unsupported_options,
)

# --------------------------------------------------------------------------
# envelopes
# --------------------------------------------------------------------------


def test_entity_wraps_in_d():
    assert entity({"EBELN": "4500000001"}) == {"d": {"EBELN": "4500000001"}}


def test_collection_wraps_in_d_results():
    assert collection([{"MBLNR": "5000000101"}]) == {
        "d": {"results": [{"MBLNR": "5000000101"}]}
    }


def test_collection_accepts_empty_list():
    """An empty collection is a valid answer, not an error.

    16 scenarios are GR_MISSING: the PO exists, no receipts were posted. That
    must serialise as results: [] -- see test_api.py.
    """
    assert collection([]) == {"d": {"results": []}}


# --------------------------------------------------------------------------
# error shape
# --------------------------------------------------------------------------


def test_error_body_matches_sap_odata_v2_shape():
    """SAP nests the message as an object with a language tag, not a string.

    {"error": {"code": ..., "message": {"lang": "en", "value": ...}}}

    It is the single cheapest detail that makes the mock read as authentic to
    someone who has used a real SAP Gateway service.
    """
    body = error_body("PO_NOT_FOUND", "Purchase order 4599999999 does not exist")
    assert set(body) == {"error"}
    assert body["error"]["code"] == "PO_NOT_FOUND"
    assert body["error"]["message"] == {
        "lang": "en",
        "value": "Purchase order 4599999999 does not exist",
    }


def test_odata_error_carries_code_message_status():
    exc = ODataError(code="X", message="y", status=404)
    assert (exc.code, exc.message, exc.status) == ("X", "y", 404)


def test_odata_error_defaults_to_400():
    """A malformed request is the common case, so 400 is the sensible default."""
    exc = ODataError(code="X", message="y")
    assert exc.status == 400


# --------------------------------------------------------------------------
# $filter parsing -- the only real logic in the module
# --------------------------------------------------------------------------


def test_parse_po_filter_extracts_the_key():
    """Returns the PO number as a plain string, not a regex Match object."""
    result = parse_po_filter("PurchaseOrder eq '4500000047'")
    assert result == "4500000047"
    assert isinstance(result, str)


@pytest.mark.parametrize(
    "raw",
    [
        "PurchaseOrder eq '4500000047'",
        "  PurchaseOrder eq '4500000047'  ",
        "PurchaseOrder  eq  '4500000047'",
    ],
)
def test_parse_po_filter_tolerates_whitespace(raw: str):
    """Clients pad query strings. Rejecting on whitespace is a pointless 400."""
    assert parse_po_filter(raw) == "4500000047"


def test_parse_po_filter_requires_a_filter():
    """Without a filter the caller would silently receive all 188 GR rows."""
    with pytest.raises(ODataError) as exc:
        parse_po_filter(None)
    assert exc.value.code == "FILTER_REQUIRED"
    assert exc.value.status == 400
    # The message must name the supported form, or the caller cannot recover.
    assert "PurchaseOrder eq" in exc.value.message


@pytest.mark.parametrize(
    "raw",
    [
        "Supplier eq '1000000001'",          # wrong property
        "PurchaseOrder gt '4500000047'",     # wrong operator
        "PurchaseOrder eq 4500000047",       # unquoted
        "nonsense",
    ],
)
def test_parse_po_filter_rejects_unsupported_shapes(raw: str):
    with pytest.raises(ODataError) as exc:
        parse_po_filter(raw)
    assert exc.value.code == "UNSUPPORTED_FILTER"
    assert exc.value.status == 400


@pytest.mark.parametrize(
    "raw",
    [
        "PurchaseOrder eq '5100000101'",   # right length, wrong range (invoice)
        "PurchaseOrder eq '450000'",       # too short
        "PurchaseOrder eq '45000000470'",  # too long
    ],
)
def test_parse_po_filter_rejects_bad_keys(raw: str):
    """Shape is fine, the value is not a PO number. Distinct error code.

    A separate code from UNSUPPORTED_FILTER matters: one means "I don't speak
    that dialect", the other means "that isn't a purchase order". A client can
    act on the difference.
    """
    with pytest.raises(ODataError) as exc:
        parse_po_filter(raw)
    assert exc.value.code == "INVALID_KEY"
    assert exc.value.status == 400


# --------------------------------------------------------------------------
# query option guard
# --------------------------------------------------------------------------


def test_allowed_options_pass():
    reject_unsupported_options({"$filter": "x", "$format": "json"}, {"$filter", "$format"})


def test_non_dollar_params_are_ignored():
    """Only OData system query options are policed; VendorID is a normal param."""
    reject_unsupported_options({"VendorID": "'1000000001'"}, {"$filter"})


@pytest.mark.parametrize("option", ["$top", "$skip", "$expand", "$select", "$orderby"])
def test_unsupported_options_are_rejected_not_ignored(option: str):
    """Silently ignoring an option the caller believed applied is the classic
    integration bug: the client asks for 5 rows, gets 188, and nothing says why.
    """
    with pytest.raises(ODataError) as exc:
        reject_unsupported_options({"$filter": "x", option: "5"}, {"$filter"})
    assert exc.value.code == "UNSUPPORTED_OPTION"
    assert option in exc.value.message
