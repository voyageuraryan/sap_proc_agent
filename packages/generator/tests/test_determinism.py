"""Determinism and label-isolation tests for the scenario generator.

These protect one property: the generator is a pure function of (config, seed).

Without it, every eval number downstream is an opinion. If the data can drift
between runs, a change in the agent's score cannot be attributed to a change in
the agent -- which means you cannot measure improvement, only guess at it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from generator.cli import load_config, write_dataset
from generator.config import GeneratorConfig

# Every ground-truth label. None of these may appear in data/erp/.
LABEL_VALUES = (
    "CLEAN",
    "PRICE_MINOR",
    "PRICE_MAJOR",
    "QTY_OVER",
    "GR_MISSING",
    "GR_PARTIAL",
    "DUP_INVOICE",
    "AMBIGUOUS",
)

# Keys that belong to the generator or the eval harness, never to the ERP.
FORBIDDEN_KEYS = ("label", "scenario_id", "ground_truth")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def repo_root() -> Path:
    """Walk up from this file until we find the repo's config directory.

    Tests must not depend on the working directory pytest was invoked from.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "config" / "scenarios.yaml").exists():
            return parent
    raise RuntimeError("repo root not found: no data/config/scenarios.yaml above this file")


def hash_tree(root: Path) -> dict[str, str]:
    """Map each file under `root` to the SHA-256 of its raw bytes.

    Raw bytes, deliberately. Comparing parsed JSON would pass while key order
    shifted, or 50.0 became 50.00, or '\\n' became '\\r\\n' on Windows. The
    claim being tested is byte-identical, so the test compares bytes.
    """
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def generate_into(cfg: GeneratorConfig, base: Path) -> Path:
    """Run the generator into base/erp and base/labels. Returns base."""
    write_dataset(cfg, base / "erp", base / "labels")
    return base


def run_generator_subprocess(config_path: Path, base: Path, hash_seed: int) -> None:
    """Invoke the generator as a fresh process with a specific PYTHONHASHSEED."""
    env = dict(os.environ, PYTHONHASHSEED=str(hash_seed))
    result = subprocess.run(
        [
            sys.executable, "-m", "generator.cli",
            "--config", str(config_path),
            "--erp-dir", str(base / "erp"),
            "--labels-dir", str(base / "labels"),
        ],
        env=env,
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,  # we assert on returncode below with the output attached
    )
    assert result.returncode == 0, (
        f"generator exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def assert_trees_identical(a: Path, b: Path, hint: str = "") -> None:
    left, right = hash_tree(a), hash_tree(b)

    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    assert not only_left and not only_right, (
        f"different files produced between runs.\n"
        f"only in first: {only_left}\nonly in second: {only_right}"
    )

    drifted = sorted(k for k in left if left[k] != right[k])
    assert not drifted, f"byte-level drift in {drifted}. {hint}"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def config_path() -> Path:
    return repo_root() / "data" / "config" / "scenarios.yaml"


@pytest.fixture(scope="session")
def config(config_path: Path) -> GeneratorConfig:
    """The real project config, validated -- same object main() builds."""
    return load_config(config_path)


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------

def test_deterministic_in_process(config: GeneratorConfig, tmp_path: Path) -> None:
    """Same config, same seed, two runs -> identical bytes.

    Catches: datetime.now(), uuid4(), date.today(), an unseeded or
    module-level random, anything reading the clock or the environment.
    """
    first = generate_into(config, tmp_path / "run_a")
    second = generate_into(config, tmp_path / "run_b")

    assert_trees_identical(
        first,
        second,
        hint="Look for a clock read, a uuid, or use of the global `random` module.",
    )


def test_deterministic_across_processes(config_path: Path, tmp_path: Path) -> None:
    """Two separate processes with different PYTHONHASHSEED -> identical bytes.

    This is the test the in-process one cannot replace. Python's string hash
    salt is fixed for the lifetime of a process, so two runs inside one pytest
    process agree with each other while both being wrong. Only a fresh process
    with a different seed exposes a dependency on hash() or on set iteration
    order -- the bug class that passes locally and fails in CI.
    """
    first = tmp_path / "proc_a"
    second = tmp_path / "proc_b"

    run_generator_subprocess(config_path, first, hash_seed=0)
    run_generator_subprocess(config_path, second, hash_seed=1)

    assert_trees_identical(
        first,
        second,
        hint="Output depends on PYTHONHASHSEED: look for hash() on a string, "
             "or iteration over a set/dict built from unsorted input.",
    )


# --------------------------------------------------------------------------
# label isolation
# --------------------------------------------------------------------------

def test_no_label_leak(config: GeneratorConfig, tmp_path: Path) -> None:
    """No ground-truth label may appear in anything the mock ERP serves.

    If a label reaches the agent's tool layer, every eval silently becomes
    trivial and the scores mean nothing -- and nothing looks broken.
    """
    erp_dir = generate_into(config, tmp_path) / "erp"

    served = sorted(erp_dir.rglob("*.json"))
    assert served, "generator wrote nothing to erp/"

    for path in served:
        text = path.read_text(encoding="utf-8")

        # Quote both sides so we match a JSON *value*, not a substring.
        # Bare "CLEAN" would false-positive on the material description
        # "CLEANER, DEGREASER, 5GAL PAIL".
        for label in LABEL_VALUES:
            assert f'"{label}"' not in text, f"label {label!r} leaked into {path.name}"

        for key in FORBIDDEN_KEYS:
            assert f'"{key}"' not in text, f"key {key!r} must not be served ({path.name})"


def test_labels_written_separately(config: GeneratorConfig, tmp_path: Path) -> None:
    """The sidecar exists, is one object keyed by scenario, and is complete."""
    labels_file = generate_into(config, tmp_path) / "labels" / "labels.json"
    assert labels_file.exists(), "no labels.json written"

    labels = json.loads(labels_file.read_text(encoding="utf-8"))

    assert isinstance(labels, dict), (
        "labels.json must be a single object mapping scenario_id -> label, "
        "not a list of one-key dicts"
    )
    assert len(labels) == config.count, (
        f"expected {config.count} labels, got {len(labels)}"
    )
    # Each value is an object: label + variant + injected detail. The detail is
    # what later lets you report "unreliable just outside tolerance" rather than
    # only a pass percentage.
    for scenario_id, meta in labels.items():
        assert set(meta) == {"label", "variant", "detail"}, (
            f"{scenario_id}: unexpected sidecar keys {sorted(meta)}"
        )
        assert meta["label"] in LABEL_VALUES, (
            f"{scenario_id}: unknown label {meta['label']!r}"
        )
        if meta["label"] == "AMBIGUOUS":
            assert meta["variant"], f"{scenario_id}: AMBIGUOUS must record a variant"
        else:
            assert meta["variant"] is None, (
                f"{scenario_id}: only AMBIGUOUS may carry a variant"
            )