"""Command-line entry point -- the only module that touches the filesystem.

Layering:
    main()                                  argparse, reads YAML, delegates
    write_dataset(cfg, erp_dir, labels_dir) all the work, paths as arguments
    build_dataset(cfg)                      pure: config+seed -> Dataset
    dump_json(erp_dir, labels_dir, dataset) serialisation only

write_dataset exists so the output location is a parameter. Without it the
determinism test could not generate into two temp directories and compare.
"""

import argparse
import json
import random
from pathlib import Path

import yaml

from generator.config import GeneratorConfig
from generator.injectors import InjectorContext, apply_injection
from generator.master_data import build_materials, build_plants, build_vendors
from generator.planner import plan_labels
from generator.scenarios import Dataset, Scenario, build_scenario
from generator.splits import assign_splits

DEFAULT_CONFIG = Path("data/config/scenarios.yaml")
DEFAULT_ERP_DIR = Path("data/erp")
DEFAULT_LABELS_DIR = Path("data/labels")


def load_config(path: Path) -> GeneratorConfig:
    """Read the YAML and validate it into a typed object."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return GeneratorConfig(**raw)


def build_dataset(cfg: GeneratorConfig) -> Dataset:
    """Pure function of (config, seed). No I/O, no clock, no globals."""
    vendors = build_vendors(cfg.vendors)
    materials = build_materials(cfg.materials)
    plants = build_plants()

    plan = plan_labels(cfg)

    scenarios: list[Scenario] = []
    for i, (label, variant) in enumerate(plan):
        # One RNG PER SCENARIO, seeded from (seed, index). With a single shared
        # stream, injectors consuming different numbers of draws would shift
        # every later scenario -- so changing the distribution by one line would
        # rewrite all 200 records and make yesterday's eval incomparable.
        rng = random.Random(f"{cfg.seed}-{i}")
        base = build_scenario(i, rng, vendors, materials, plants, cfg)
        ctx = InjectorContext(tolerances=cfg.tolerances, variant=variant)
        scenarios.append(apply_injection(base, label, rng, ctx))

    return Dataset(
        vendors=vendors,
        materials=materials,
        plants=plants,
        scenarios=scenarios,
        tolerances=cfg.tolerances,
        splits=assign_splits(scenarios, cfg),
    )


def _write_json(path: Path, payload) -> None:
    """Single write path for every output file.

    encoding + newline are explicit on purpose. Text mode on Windows would use
    the locale encoding and translate '\\n' to '\\r\\n', so the same seed would
    produce different bytes locally and in Linux CI -- and the determinism test
    would pass on both while disagreeing between them.

    sort_keys keeps object key order stable regardless of insertion order.
    """
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def _as_json(models) -> list[dict]:
    return [m.model_dump(by_alias=True, mode="json") for m in models]


def dump_json(erp_dir: Path, labels_dir: Path, dataset: Dataset) -> None:
    """Write master data and documents to erp_dir, ground truth to labels_dir."""
    _write_json(erp_dir / "vendors.json", _as_json(dataset.vendors))
    _write_json(erp_dir / "materials.json", _as_json(dataset.materials))
    _write_json(erp_dir / "plants.json", _as_json(dataset.plants))

    _write_json(
        erp_dir / "purchase_orders.json",
        _as_json(s.po for s in dataset.scenarios),
    )

    # Flattened: one row per GR line across all scenarios. A scenario may
    # contribute zero rows (GR_MISSING) or several (partial deliveries).
    goods_receipts = [gr for s in dataset.scenarios for gr in s.goods_receipts]
    _write_json(erp_dir / "goods_receipts.json", _as_json(goods_receipts))

    # extend, not append: DUP_INVOICE contributes two invoices.
    invoices = [inv for s in dataset.scenarios for inv in s.invoices]
    _write_json(erp_dir / "invoices.json", _as_json(invoices))

    # Tolerances are CONFIG in SAP (per company code), not a field on EKKO, so
    # they get their own file rather than riding on the purchase order.
    _write_json(
        erp_dir / "tolerances.json",
        dataset.tolerances.model_dump(mode="json"),
    )

    # The sidecar. One object keyed by scenario_id -- never a document number,
    # because DUP_INVOICE will put two invoices in one scenario.
    # An object per scenario, not a bare string: recording the injected
    # magnitude is what later lets you say "unreliable just outside tolerance"
    # instead of only "82% pass".
    _write_json(
        labels_dir / "labels.json",
        {
            s.scenario_id: {
                "label": s.label,
                "variant": s.variant,
                "detail": s.detail,
            }
            for s in dataset.scenarios
        },
    )

    # Which split a case belongs to is a channel the agent must not see, so
    # this lives in labels/, never erp/.
    _write_json(labels_dir / "splits.json", dataset.splits.model_dump(mode="json"))


def write_dataset(cfg: GeneratorConfig, erp_dir: Path, labels_dir: Path) -> None:
    """Generate and write. The seam the determinism test calls."""
    erp_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)
    dump_json(erp_dir, labels_dir, build_dataset(cfg))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate labelled procurement scenarios for the mock ERP."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--erp-dir", type=Path, default=DEFAULT_ERP_DIR)
    parser.add_argument("--labels-dir", type=Path, default=DEFAULT_LABELS_DIR)
    args = parser.parse_args()

    cfg = load_config(args.config)
    write_dataset(cfg, args.erp_dir, args.labels_dir)
    print(
        f"wrote {cfg.count} scenarios (seed {cfg.seed}) "
        f"to {args.erp_dir} and {args.labels_dir}"
    )


if __name__ == "__main__":
    main()
