"""Does each label's data actually match its label?

This is the test that protects the eval set. A label that disagrees with its
own data does not cost you one case -- it makes every number you publish
untrustworthy, including the cost-per-resolution table in the README.

Assertions are FLAT and PER-LABEL on purpose. Factoring out a shared
three_way_match() helper would put the answer key in the repo in executable
form, and the temptation to have the agent call it later is real.
"""

from __future__ import annotations

import collections
import json
from decimal import Decimal
from pathlib import Path

import pytest
from generator.cli import load_config, write_dataset


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> dict:
    """Generate once, load the raw JSON back the way the mock ERP will see it."""
    root = Path(__file__).resolve()
    for parent in root.parents:
        if (parent / "data" / "config" / "scenarios.yaml").exists():
            repo = parent
            break
    else:
        raise RuntimeError("repo root not found")

    base = tmp_path_factory.mktemp("taxonomy")
    cfg = load_config(repo / "data" / "config" / "scenarios.yaml")
    write_dataset(cfg, base / "erp", base / "labels")

    def load(p):
        return json.loads((base / p).read_text(encoding="utf-8"))

    pos = load("erp/purchase_orders.json")
    grs = load("erp/goods_receipts.json")
    invs = load("erp/invoices.json")

    gr_by_po = collections.defaultdict(list)
    for g in grs:
        gr_by_po[g["EBELN"]].append(g)
    inv_by_po = collections.defaultdict(list)
    for i in invs:
        inv_by_po[i["items"][0]["EBELN"]].append(i)

    # scenario_id SC-000N <-> PO 45.......N by construction
    po_by_scenario = {f"SC-{int(p['EBELN'][2:]):04d}": p for p in pos}

    return {
        "cfg": cfg,
        "labels": load("labels/labels.json"),
        "splits": load("labels/splits.json"),
        "tolerances": load("erp/tolerances.json"),
        "po_by_scenario": po_by_scenario,
        "gr_by_po": gr_by_po,
        "inv_by_po": inv_by_po,
    }


def _tolerance(tolerances: dict, vendor_id: str) -> Decimal:
    entry = tolerances["by_vendor"].get(vendor_id, tolerances["default"])
    return Decimal(entry["price_pct"])


def test_label_matches_data(generated):
    t = generated["tolerances"]
    checked = collections.Counter()

    for scenario_id, meta in generated["labels"].items():
        label = meta["label"]
        po = generated["po_by_scenario"][scenario_id]
        ebeln = po["EBELN"]
        line = po["items"][0]
        grs = generated["gr_by_po"][ebeln]
        invs = generated["inv_by_po"][ebeln]

        po_qty = Decimal(line["MENGE"])
        po_price = Decimal(line["NETPR"])
        gr_sum = sum((Decimal(g["MENGE"]) for g in grs), Decimal(0))
        where = f"{scenario_id} ({label})"

        if label == "AMBIGUOUS":
            variant = meta["variant"]
            assert variant, f"{where}: AMBIGUOUS must record its variant"
            if variant == "DANGLING_PO_LINE":
                billed = invs[0]["items"][0]["EBELP"]
                assert billed not in [i["EBELP"] for i in po["items"]], (
                    f"{where}: billed line {billed} exists on the PO"
                )
            elif variant == "UNAUTHORISED_OVER_DELIVERY":
                assert gr_sum > po_qty, f"{where}: received {gr_sum} not > ordered {po_qty}"
            else:
                assert len(grs) == 2, f"{where}: expected 2 GR rows, got {len(grs)}"
            checked[label] += 1
            continue

        inv = invs[0]
        inv_line = inv["items"][0]
        inv_qty = Decimal(inv_line["MENGE"])
        inv_price = Decimal(inv_line["NETPR"])

        if label == "CLEAN":
            assert inv_qty == gr_sum == po_qty, f"{where}: quantities disagree"
            assert inv_price == po_price, f"{where}: prices disagree"
            assert inv["block_reason"] is None, f"{where}: should not be blocked"
            assert len(invs) == 1, f"{where}: should have exactly one invoice"

        elif label in ("PRICE_MINOR", "PRICE_MAJOR"):
            tol = _tolerance(t, po["LIFNR"])
            variance = (inv_price - po_price) / po_price * 100
            if label == "PRICE_MINOR":
                assert variance <= tol, f"{where}: {variance:.2f}% exceeds tolerance {tol}%"
            else:
                assert variance > tol, f"{where}: {variance:.2f}% within tolerance {tol}%"
            # Both must look identical to the agent: SAP says the price check
            # failed, not whether the variance is acceptable.
            assert inv["block_reason"] == "PRICE_VARIANCE", f"{where}: block_reason must be PRICE_VARIANCE"

        elif label == "QTY_OVER":
            assert inv_qty > gr_sum, f"{where}: invoiced {inv_qty} not > received {gr_sum}"
            # Must NOT exceed the PO, or an agent comparing invoice-to-PO would
            # catch it by accident and this label would test nothing.
            assert inv_qty <= po_qty, f"{where}: invoiced {inv_qty} exceeds ordered {po_qty}"
            assert inv["block_reason"] == "QUANTITY_VARIANCE", f"{where}: block_reason must be QUANTITY_VARIANCE"

        elif label == "GR_MISSING":
            assert not grs, f"{where}: expected no GR rows, got {len(grs)}"
            assert inv["block_reason"] == "MISSING_GR", f"{where}: block_reason must be MISSING_GR"

        elif label == "GR_PARTIAL":
            assert gr_sum < po_qty, f"{where}: received {gr_sum} not < ordered {po_qty}"
            assert inv_qty == gr_sum, f"{where}: invoiced {inv_qty} != received {gr_sum}"
            # The trap: nothing is wrong, so nothing is blocked.
            assert inv["block_reason"] is None, f"{where}: valid partial must not be blocked"

        elif label == "DUP_INVOICE":
            assert len(invs) == 2, f"{where}: expected 2 invoices, got {len(invs)}"
            assert invs[0]["XBLNR"] == invs[1]["XBLNR"], f"{where}: XBLNR must match"
            assert invs[0]["LIFNR"] == invs[1]["LIFNR"], f"{where}: vendor must match"
            assert invs[0]["BELNR"] != invs[1]["BELNR"], f"{where}: BELNR must differ"

        else:
            pytest.fail(f"{where}: no assertion defined for this label")

        checked[label] += 1

    assert sum(checked.values()) == generated["cfg"].count


def test_distribution_exact(generated):
    cfg = generated["cfg"]
    actual = collections.Counter(v["label"] for v in generated["labels"].values())
    for label, weight in cfg.distribution.items():
        assert actual[str(label)] == round(cfg.count * weight), (
            f"{label}: expected {round(cfg.count * weight)}, got {actual[str(label)]}"
        )


def test_splits_valid(generated):
    s = generated["splits"]
    dev, held, golden = set(s["dev"]), set(s["eval"]), set(s["golden"])
    labels = generated["labels"]

    assert not dev & held, "dev and eval overlap"
    assert dev | held == set(labels), "splits do not cover every scenario"
    assert golden <= dev, "golden cases must be in dev, never holdout"

    # Stratified: every label must appear on both sides, or the holdout number
    # for a missing category would be based on nothing.
    for side, name in ((dev, "dev"), (held, "eval")):
        present = {labels[i]["label"] for i in side}
        assert present == {str(k) for k in generated["cfg"].distribution}, (
            f"{name} is missing labels: { {str(k) for k in generated['cfg'].distribution} - present}"
        )


def test_price_labels_are_tolerance_relative(generated):
    """A hardcoded percentage would mislabel tight-tolerance vendors.

    Recorded variance must scale with the vendor's tolerance, not be constant.
    """
    by_tol = collections.defaultdict(list)
    for meta in generated["labels"].values():
        if meta["label"] == "PRICE_MAJOR":
            by_tol[meta["detail"]["tolerance_pct"]].append(
                Decimal(meta["detail"]["variance_pct"])
            )

    assert len(by_tol) > 1, "no vendor tolerance variety in PRICE_MAJOR scenarios"
    for tol, variances in by_tol.items():
        for v in variances:
            assert v > Decimal(tol), f"variance {v} not above tolerance {tol}"
