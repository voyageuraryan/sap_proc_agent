"""Decides which label each scenario index gets.

Exact counts, not sampling: rng.choices over 200 trials at 6% could hand you 7
AMBIGUOUS instead of 12, and your published distribution would be a lie.

The shuffle uses its own RNG so it never draws from any scenario's stream.
"""

from random import Random

from generator.config import GeneratorConfig
from generator.labels import AmbiguousVariant, ExceptionLabel


def plan_labels(
    cfg: GeneratorConfig,
) -> list[tuple[ExceptionLabel, AmbiguousVariant | None]]:
    counts = {
        label: round(cfg.count * weight) for label, weight in cfg.distribution.items()
    }

    # Rounding leaves us off by +/-1 or 2. Absorb it in the largest bucket so the
    # small buckets stay exactly as configured -- a +/-1 on AMBIGUOUS is 8% of
    # that category.
    remainder = cfg.count - sum(counts.values())
    if remainder:
        biggest = max(counts, key=lambda k: counts[k])
        counts[biggest] += remainder

    out: list[tuple[ExceptionLabel, AmbiguousVariant | None]] = []
    for label, n in counts.items():
        if label is ExceptionLabel.AMBIGUOUS:
            variants = cfg.ambiguous_variants
            base, extra = divmod(n, len(variants))
            for i, variant in enumerate(variants):
                out.extend([(label, variant)] * (base + (1 if i < extra else 0)))
        else:
            out.extend([(label, None)] * n)

    Random(f"{cfg.seed}-labels").shuffle(out)
    return out
