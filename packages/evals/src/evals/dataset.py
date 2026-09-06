"""The eval set: which scenarios, which invoice, and what the right answer is.

This module is the ONLY place in the repo that reads `data/labels/`. Everything
downstream of the mock ERP is structurally unable to see ground truth; the eval
harness is the one component that is supposed to, and keeping that in one file
is what makes the claim checkable by grep rather than by argument.

Two sources are combined on purpose:

  * `data/labels/labels.json` and `splits.json` -- the artefacts of record,
    written by the generator, committed, and diffable.
  * a fresh in-memory rebuild of the dataset via `generator.cli.build_dataset`
    -- the only authority on which INVOICE belongs to which scenario, because
    the sidecar is keyed on scenario_id and deliberately carries no document
    numbers.

They are cross-checked against each other. If the committed sidecar and the
generator disagree, the dataset on disk is stale and every number the eval
produces would be measured against the wrong answer key -- so that is an
error, not a warning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from generator.cli import DEFAULT_CONFIG, build_dataset, load_config

#: Split names the harness understands. `dev` is for tuning, `eval` is the
#: holdout that must not be opened while tuning, `golden` is the small
#: hand-picked set the live run targets.
SPLITS = ("dev", "eval", "golden", "all")


@dataclass(frozen=True)
class EvalCase:
    """One scenario, resolved down to what the harness needs to run and score."""

    scenario_id: str
    #: The invoice the agent is asked to verify. For DUP_INVOICE this is the
    #: SECOND invoice -- the duplicate -- because the first one is legitimate
    #: and rejecting it would be the wrong answer.
    invoice_number: str
    label: str
    variant: str | None = None
    #: The injected magnitudes, as strings. Used to check that a proposed
    #: correction carries the RIGHT numbers, not merely the right shape.
    detail: dict[str, str] = field(default_factory=dict)
    #: Every invoice in the scenario. The safety check snapshots all of them,
    #: because a write to the invoice the agent did NOT target still counts.
    all_invoice_numbers: tuple[str, ...] = ()


class DatasetError(RuntimeError):
    """The committed dataset and the generator disagree, or a split is unusable."""


def repo_root(start: Path | None = None) -> Path:
    """Walk up until the data directory appears.

    So the harness runs from any working directory -- which matters because CI
    and a developer's shell rarely agree on where they start.
    """
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "data" / "labels" / "labels.json").exists():
            return parent
    raise DatasetError("could not locate the repo root (no data/labels/labels.json above me)")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"{path} is missing; run `uv run python -m generator.cli`") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path} is not valid JSON: {exc}") from exc


def _rebuild_index(root: Path) -> dict[str, list[str]]:
    """scenario_id -> its invoice numbers, in the order the generator emitted them."""
    cfg = load_config(root / DEFAULT_CONFIG)
    dataset = build_dataset(cfg)
    return {s.scenario_id: [inv.invoice_number for inv in s.invoices] for s in dataset.scenarios}


def _rebuild_labels(root: Path) -> dict[str, str]:
    cfg = load_config(root / DEFAULT_CONFIG)
    return {s.scenario_id: str(s.label) for s in build_dataset(cfg).scenarios}


def load_cases(
    split: str = "golden",
    *,
    root: Path | None = None,
    limit: int | None = None,
) -> list[EvalCase]:
    """Resolve a split into runnable, scoreable cases.

    Ordered by scenario_id so a truncated run (`--limit`) is reproducible and
    two runs of the same split are comparable line for line.
    """
    if split not in SPLITS:
        raise DatasetError(f"unknown split {split!r}; expected one of {', '.join(SPLITS)}")

    root = root or repo_root()
    labels = _load_json(root / "data" / "labels" / "labels.json")
    splits = _load_json(root / "data" / "labels" / "splits.json")

    if split == "all":
        scenario_ids = sorted(labels)
    else:
        if split not in splits:
            raise DatasetError(f"splits.json has no {split!r} key")
        scenario_ids = sorted(splits[split])

    if not scenario_ids:
        raise DatasetError(
            f"the {split!r} split is empty. "
            f"For 'golden', set golden_ids in data/config/scenarios.yaml and regenerate."
        )

    index = _rebuild_index(root)

    cases: list[EvalCase] = []
    for scenario_id in scenario_ids:
        if scenario_id not in labels:
            raise DatasetError(f"{scenario_id} is in the {split} split but not in labels.json")
        if scenario_id not in index:
            raise DatasetError(
                f"{scenario_id} is in labels.json but the generator does not produce it; "
                f"data/ is stale -- rerun `uv run python -m generator.cli`"
            )

        entry = labels[scenario_id]
        invoices = index[scenario_id]
        if not invoices:
            raise DatasetError(f"{scenario_id} has no invoice")

        cases.append(
            EvalCase(
                scenario_id=scenario_id,
                # The last one: for DUP_INVOICE that is the duplicate, which is
                # the invoice a human would actually be looking at.
                invoice_number=invoices[-1],
                label=str(entry["label"]),
                variant=entry.get("variant"),
                detail=dict(entry.get("detail") or {}),
                all_invoice_numbers=tuple(invoices),
            )
        )

    _assert_sidecar_matches_generator(root, cases)
    return cases[:limit] if limit else cases


def _assert_sidecar_matches_generator(root: Path, cases: list[EvalCase]) -> None:
    """The committed answer key must describe the dataset the generator makes.

    Without this, editing labels.json by hand (or forgetting to regenerate
    after a config change) would silently score every run against the wrong
    truth -- and the eval would still print a confident number.
    """
    generated = _rebuild_labels(root)
    for case in cases:
        expected = generated.get(case.scenario_id)
        if expected != case.label:
            raise DatasetError(
                f"{case.scenario_id}: labels.json says {case.label!r} but the generator "
                f"produces {expected!r}. data/ is stale -- rerun the generator."
            )


def label_counts(cases: list[EvalCase]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.label] = counts.get(case.label, 0) + 1
    return dict(sorted(counts.items()))
