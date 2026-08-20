"""Stratified dev / eval / golden assignment.

Holding data back is the defence against prompt overfitting: tuning against
failures you can see teaches you about those 160 cases, not about the 161st.
The gap between dev score and holdout score IS the overfitting, quantified.

Stratified per (label, variant), not a slice of the whole list: with 12
AMBIGUOUS at 20% a contiguous tail could hand you a holdout containing none of
them, and your headline number on the hardest category would rest on nothing.
"""

import math
from collections import defaultdict

from pydantic import BaseModel

from generator.config import GeneratorConfig


class Splits(BaseModel):
    eval_fraction: float
    dev: list[str]
    eval: list[str]
    golden: list[str]


def assign_splits(scenarios: list, cfg: GeneratorConfig) -> Splits:
    # Deliberately untyped: scenarios.py imports Splits, so importing
    # Scenario here would be a cycle. Only .scenario_id/.label/.variant used.
    groups: dict[tuple, list[str]] = defaultdict(list)
    for s in scenarios:
        groups[(s.label, s.variant)].append(s.scenario_id)

    dev: list[str] = []
    held: list[str] = []
    for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]))):
        ids = sorted(groups[key])
        n_eval = math.ceil(len(ids) * cfg.eval_fraction)
        held.extend(ids[-n_eval:])
        dev.extend(ids[:-n_eval] if n_eval else ids)

    dev, held = sorted(dev), sorted(held)

    # Fail loudly rather than shipping a broken holdout.
    if set(dev) & set(held):
        raise ValueError("dev and eval overlap")
    if len(dev) + len(held) != len(scenarios):
        raise ValueError("splits do not cover every scenario")
    stray = set(cfg.golden_ids) - set(dev)
    if stray:
        # Golden cases are hand-read for the demo, so they cannot also be holdout.
        raise ValueError(f"golden_ids must be in dev, these are not: {sorted(stray)}")

    return Splits(
        eval_fraction=cfg.eval_fraction,
        dev=dev,
        eval=held,
        golden=list(cfg.golden_ids),
    )
