"""The loader is the service's only door to data. These tests guard that door."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from mock_erp.store import ErpDataError, ErpStore, load_store


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "erp" / "purchase_orders.json").exists():
            return parent
    raise RuntimeError("repo root not found -- run `uv run generator` first")


@pytest.fixture(scope="module")
def store() -> ErpStore:
    return load_store(repo_root() / "data" / "erp")


def test_record_counts(store: ErpStore):
    """Counts follow from the taxonomy, so they are a real assertion.

    188 goods receipts, not 200: 16 GR_MISSING scenarios contribute none and 4
    CONFLICTING_RECEIPTS contribute two. 212 invoices, not 200: 12 DUP_INVOICE
    scenarios contribute a second one.
    """
    assert len(store.vendors) == 12
    assert len(store.materials) == 40
    assert len(store.plants) == 4
    assert len(store.purchase_orders) == 200
    assert sum(len(v) for v in store.grs_by_po.values()) == 188
    assert len(store.invoices) == 212


def test_purchase_orders_without_receipts_have_no_key(store: ErpStore):
    """16 POs must be ABSENT from grs_by_po, not present with an empty list.

    This is what lets the endpoint distinguish "no goods receipt was posted"
    (GR_MISSING -> 200 with an empty collection) from "no such PO" (404). Those
    demand completely different agent behaviour, so the store must not blur them.
    """
    assert len(store.grs_by_po) == 184
    missing = set(store.purchase_orders) - set(store.grs_by_po)
    assert len(missing) == 16
    assert not isinstance(store.grs_by_po, dict) or all(
        store.grs_by_po[po] for po in store.grs_by_po
    ), "no key may map to an empty list"


def test_grs_by_po_is_not_a_defaultdict(store: ErpStore):
    """A defaultdict would invent an empty list for a PO that does not exist."""
    with pytest.raises(KeyError):
        store.grs_by_po["4599999999"]


def test_duplicate_invoices_share_vendor_ref(store: ErpStore):
    """DUP_INVOICE is only solvable via the vendor's other invoice references."""
    refs: dict[tuple[str, str], int] = {}
    for vendor_id, docs in store.invoices_by_vendor.items():
        for doc in docs:
            refs[(vendor_id, doc.vendor_ref)] = refs.get((vendor_id, doc.vendor_ref), 0) + 1
    repeated = [k for k, n in refs.items() if n > 1]
    assert len(repeated) == 12, f"expected 12 duplicated vendor refs, got {len(repeated)}"


def test_dangling_po_line_is_tolerated(store: ErpStore):
    """AMBIGUOUS/DANGLING_PO_LINE bills a line the PO does not have.

    Intentional test data. If the loader ever gains an item-level integrity
    check the service will refuse to start -- which is why _check_references
    validates document references only.
    """
    dangling = [
        (inv.invoice_number, item.po_item_number)
        for inv in store.invoices.values()
        for item in inv.items
        if item.po_item_number
        not in {i.po_item_number for i in store.purchase_orders[item.po_number].items}
    ]
    assert len(dangling) == 4, f"expected 4 dangling line references, got {len(dangling)}"


def test_tolerance_lookup_and_fallback(store: ErpStore):
    assert store.tolerances.for_vendor("1000000006").price_pct == 2  # GRANITE
    assert store.tolerances.for_vendor("1000000010").price_pct == 10  # TIG
    assert store.tolerances.for_vendor("1000000001").price_pct == 5  # unlisted


def test_list_indexes_are_sorted(store: ErpStore):
    for rows in store.grs_by_po.values():
        numbers = [r.gr_number for r in rows]
        assert numbers == sorted(numbers)
    for docs in store.invoices_by_vendor.values():
        numbers = [d.invoice_number for d in docs]
        assert numbers == sorted(numbers)


def test_store_is_immutable(store: ErpStore):
    with pytest.raises(FrozenInstanceError):
        store.vendors = {}


def test_store_knows_nothing_about_labels(store: ErpStore):
    """No index may carry ground truth. Structural, not a convention."""
    assert not [f for f in store.__dataclass_fields__ if "label" in f or "split" in f]


def test_missing_file_raises_at_load(tmp_path: Path):
    """A service that starts with three of seven files is worse than one that refuses."""
    with pytest.raises(ErpDataError, match="vendors.json"):
        load_store(tmp_path)


def test_broken_reference_raises_at_load(tmp_path: Path):
    """Referential integrity is checked at startup, not discovered by a 500."""
    src = repo_root() / "data" / "erp"
    for name in src.glob("*.json"):
        (tmp_path / name.name).write_text(name.read_text(encoding="utf-8"), encoding="utf-8")

    grs = json.loads((tmp_path / "goods_receipts.json").read_text(encoding="utf-8"))
    grs[0]["EBELN"] = "4599999999"  # a PO that does not exist
    (tmp_path / "goods_receipts.json").write_text(json.dumps(grs), encoding="utf-8")

    with pytest.raises(ErpDataError, match="unknown PO"):
        load_store(tmp_path)
