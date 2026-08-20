"""Typed view of data/config/scenarios.yaml.

Config is read once at the edge (cli.py) and passed inward as this object.
No module below the CLI touches the filesystem -- that is what keeps the
generator a pure function of (config, seed), and what makes it testable from
any working directory.

`extra="forbid"` means a typo'd or stale YAML key fails at load with a clear
message, instead of being silently ignored.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from generator.labels import AmbiguousVariant, ExceptionLabel
from generator.tolerances import ToleranceBook


class GeneratorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int
    count: int = Field(gt=0)
    items: int = Field(gt=0)
    vendors: int = Field(gt=0)
    materials: int = Field(gt=0)

    # All dates derive from this, never from the clock.
    epoch_date: date

    # tuple[int, int] rather than list[int]: free validation that the range has
    # exactly two elements, so rng.randint(*range) cannot blow up at runtime.
    lead_time_days: tuple[int, int]
    invoice_lag_days: tuple[int, int]

    tolerances: ToleranceBook

    # Typed keys: a typo'd label like PRICE_MAJORR fails at load. extra="forbid"
    # only guards top-level keys, so without this you would silently generate
    # zero of that label and 24 fewer scenarios than you think.
    distribution: dict[ExceptionLabel, float]
    ambiguous_variants: list[AmbiguousVariant]

    # Holdout: never opened while tuning prompts. See splits.py.
    eval_fraction: float = Field(gt=0, lt=1)
    golden_ids: list[str] = Field(default_factory=list)

    @field_validator("distribution")
    @classmethod
    def _sums_to_one(cls, v: dict) -> dict:
        total = sum(v.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"distribution must sum to 1.0, got {total}")
        return v
