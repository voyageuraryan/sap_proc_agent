"""The eval set, and the integrity checks around it.

The load-bearing test here is
`test_only_the_eval_package_reads_the_ground_truth_labels`. Everything else in
this repo claims that the agent cannot see the answers; this is the test that
makes the claim checkable rather than argued.
"""

import ast
import json
from pathlib import Path

import pytest
from evals.dataset import SPLITS, DatasetError, EvalCase, label_counts, load_cases, repo_root

ALL_LABELS = {
    "CLEAN",
    "PRICE_MINOR",
    "PRICE_MAJOR",
    "QTY_OVER",
    "GR_MISSING",
    "GR_PARTIAL",
    "DUP_INVOICE",
    "AMBIGUOUS",
}


def test_every_split_loads(root):
    for split in SPLITS:
        cases = load_cases(split, root=root)
        assert cases, split
        assert all(isinstance(c, EvalCase) for c in cases)


def test_the_golden_split_covers_every_label_and_variant(root):
    """A live-run set that misses a label measures nothing about that label."""
    cases = load_cases("golden", root=root)
    assert set(label_counts(cases)) == ALL_LABELS
    variants = {c.variant for c in cases if c.label == "AMBIGUOUS"}
    assert variants == {
        "DANGLING_PO_LINE",
        "UNAUTHORISED_OVER_DELIVERY",
        "CONFLICTING_RECEIPTS",
    }


def test_golden_is_drawn_from_dev_not_the_holdout(root):
    """Golden cases get read by eye, so they cannot also be the holdout."""
    golden = {c.scenario_id for c in load_cases("golden", root=root)}
    dev = {c.scenario_id for c in load_cases("dev", root=root)}
    held = {c.scenario_id for c in load_cases("eval", root=root)}
    assert golden <= dev
    assert not (golden & held)


def test_dev_and_eval_are_disjoint_and_cover_everything(root):
    dev = {c.scenario_id for c in load_cases("dev", root=root)}
    held = {c.scenario_id for c in load_cases("eval", root=root)}
    everything = {c.scenario_id for c in load_cases("all", root=root)}
    assert not (dev & held)
    assert dev | held == everything


def test_the_holdout_is_stratified(root):
    """A contiguous tail could hand you a holdout with no AMBIGUOUS in it."""
    assert set(label_counts(load_cases("eval", root=root))) == ALL_LABELS


def test_a_duplicate_scenario_targets_the_duplicate_not_the_original(root):
    """The first invoice is legitimate; rejecting it would be the wrong answer."""
    dup = next(c for c in load_cases("all", root=root) if c.label == "DUP_INVOICE")
    assert len(dup.all_invoice_numbers) == 2
    assert dup.invoice_number == dup.all_invoice_numbers[-1]
    assert dup.invoice_number.endswith("02")
    assert dup.detail["duplicate_of"] == dup.all_invoice_numbers[0]


def test_every_other_scenario_has_exactly_one_invoice(root):
    for case in load_cases("all", root=root):
        expected = 2 if case.label == "DUP_INVOICE" else 1
        assert len(case.all_invoice_numbers) == expected, case.scenario_id


def test_cases_are_ordered_so_a_truncated_run_is_reproducible(root):
    ids = [c.scenario_id for c in load_cases("eval", root=root)]
    assert ids == sorted(ids)
    assert [c.scenario_id for c in load_cases("eval", root=root, limit=5)] == ids[:5]


def test_an_unknown_split_is_refused(root):
    with pytest.raises(DatasetError, match="unknown split"):
        load_cases("holdout", root=root)


def test_an_empty_split_says_how_to_fix_it(tmp_path, root):
    """The failure mode this actually had: golden_ids was never filled in."""
    labels = root / "data" / "labels"
    fake = tmp_path / "data" / "labels"
    fake.mkdir(parents=True)
    (tmp_path / "data" / "config").mkdir(parents=True)
    (fake / "labels.json").write_text((labels / "labels.json").read_text())
    splits = json.loads((labels / "splits.json").read_text())
    splits["golden"] = []
    (fake / "splits.json").write_text(json.dumps(splits))
    (tmp_path / "data" / "config" / "scenarios.yaml").write_text(
        (root / "data" / "config" / "scenarios.yaml").read_text()
    )
    with pytest.raises(DatasetError, match="golden_ids"):
        load_cases("golden", root=tmp_path)


def test_a_hand_edited_answer_key_is_caught(tmp_path, root):
    """Editing labels.json by hand would score every run against a lie.

    The generator is the authority on what each scenario IS; the sidecar is
    an artefact of it. If they disagree, the data on disk is stale.
    """
    for sub in ("labels", "config"):
        (tmp_path / "data" / sub).mkdir(parents=True)
    src = root / "data"
    (tmp_path / "data" / "config" / "scenarios.yaml").write_text(
        (src / "config" / "scenarios.yaml").read_text()
    )
    (tmp_path / "data" / "labels" / "splits.json").write_text(
        (src / "labels" / "splits.json").read_text()
    )
    labels = json.loads((src / "labels" / "labels.json").read_text())
    labels["SC-0001"]["label"] = "QTY_OVER"  # it is CLEAN
    (tmp_path / "data" / "labels" / "labels.json").write_text(json.dumps(labels))

    with pytest.raises(DatasetError, match="stale"):
        load_cases("golden", root=tmp_path)


def test_repo_root_is_found_from_anywhere(root):
    assert (root / "data" / "labels" / "labels.json").exists()
    assert repo_root(root / "packages" / "evals" / "src") == root


def _string_literals(path) -> list[str]:
    """Every string in the file EXCEPT docstrings and comments.

    Scanning raw text would flag mock_erp/settings.py, whose docstring says it
    has "no code path pointing at data/labels/" -- prose asserting the very
    property under test. A test that punishes you for documenting the rule is
    a bad test, so this looks at code.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_only_the_eval_package_reads_the_ground_truth_labels(root):
    """The structural claim, made checkable.

    Nothing outside the eval harness and the generator that WRITES the labels
    may name the labels directory in code. If this fails, some module has
    grown a path to the answer key and every number the eval produces is
    suspect.
    """
    allowed = (
        root / "packages" / "evals",
        root / "packages" / "generator",
    )
    offenders = []
    for path in (root / "packages").rglob("*.py"):
        if any(str(path).startswith(str(a)) for a in allowed) or "__pycache__" in str(path):
            continue
        for literal in _string_literals(path):
            if "data/labels" in literal or "labels.json" in literal:
                offenders.append(f"{path.relative_to(root)}: {literal!r}")
    assert offenders == [], f"these reach for ground truth in code: {offenders}"


def test_the_mock_erp_has_no_setting_pointing_at_labels(root):
    """Not merely un-read: unreachable. There is no configuration for it."""
    from mock_erp.settings import Settings

    for name, field in Settings.model_fields.items():
        assert "label" not in name
        assert "labels" not in str(field.default)
