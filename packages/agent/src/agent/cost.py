"""What a run cost, in dollars.

Kept separate from tracing on purpose. Cost is a property of the run whether or
not anyone is watching: it goes into `AgentRun`, gets printed by the CLI, and
becomes a column in the Step 8 eval table. Tracing is one *consumer* of it.

Two sources of truth, in this order:

  1. LiteLLM's own price map, which is maintained upstream and covers every
     provider it fronts. This is the authority.
  2. LOCAL_PRICES below -- a small pinned table, for models LiteLLM's map does
     not know (self-hosted, proxied through a gateway, or newer than the
     pinned litellm) and as a drift detector for the one model this project
     actually uses.

If neither knows the model, the cost is None -- "unpriced" -- and that is
reported as such. A silent zero would be worse than a blank: it reads as free.

LiteLLM is kept for its price map ONLY. The LangChain rebuild moved every
provider call to LangChain's chat models; nothing here sends a request. The
map is kept because LangChain has no price table of its own, and a maintained
upstream table beats a hand-kept one for every model this project does not
pin. Langfuse prices generations server-side from its own table as well, so
the trace and the AgentRun are two independent readings of the same run.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

#: Costs are fractions of a cent, so eight places. Quantised at the boundary,
#: never accumulated as float: the upstream price is a float, but everything
#: downstream of this module is Decimal.
COST_PRECISION = Decimal("0.00000001")

#: One million. Prices are quoted per million tokens everywhere.
MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, plus when the figure was last looked at.

    `checked_on` is a required field rather than a comment because a price with
    no date is indistinguishable from a guess. A test asserts every entry has
    one.
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    checked_on: str
    source: str


#: Pinned prices. NOTE: these were read from LiteLLM's own map on the date
#: shown -- they are NOT independently verified against the provider's pricing
#: page. They exist so that a change becomes *visible* (see
#: test_pinned_prices_have_not_drifted_from_litellm), not because they are an
#: authority. Re-check before quoting a number to anyone.
LOCAL_PRICES: dict[str, ModelPrice] = {
    "anthropic/claude-sonnet-4-5": ModelPrice(
        input_per_mtok=Decimal("6.00"),
        output_per_mtok=Decimal("22.50"),
        checked_on="2026-08-25",
        source="litellm 1.98.0 model_cost map",
    ),
}


@dataclass(frozen=True)
class TokenCost:
    """The cost of one LLM call, split so the ratio is visible.

    Output tokens are several times the price of input tokens, so a run that
    looks expensive is usually one where the model wrote too much, not one
    where it read too much. Splitting the two makes that diagnosable.
    """

    input_usd: Decimal | None
    output_usd: Decimal | None

    @property
    def priced(self) -> bool:
        return self.input_usd is not None and self.output_usd is not None

    @property
    def total_usd(self) -> Decimal | None:
        if not self.priced:
            return None
        return self.input_usd + self.output_usd

    def as_langfuse_cost_details(self) -> dict[str, float] | None:
        """Langfuse wants floats keyed by usage type, or nothing at all."""
        if not self.priced:
            return None
        return {
            "input": float(self.input_usd),
            "output": float(self.output_usd),
            "total": float(self.total_usd),
        }


UNPRICED = TokenCost(input_usd=None, output_usd=None)

#: Quantised zero, so a free run reads as $0.000000 rather than as unknown.
ZERO_USD = Decimal("0").quantize(COST_PRECISION)

#: A cost function takes (model, prompt_tokens, completion_tokens) and returns
#: (input_usd, output_usd) or None if it does not know the model.
CostFn = Callable[[str, int, int], "tuple[Decimal, Decimal] | None"]


def _quantise(value: float | Decimal) -> Decimal:
    # str() first: Decimal(float) would carry the float's binary error into a
    # type chosen specifically to avoid it.
    return Decimal(str(value)).quantize(COST_PRECISION)


def litellm_cost(model: str, prompt_tokens: int, completion_tokens: int):
    """Ask LiteLLM's price map. Returns None for a model it does not know.

    Imported lazily, and its chatter is swallowed: on an unknown model litellm
    prints a provider list to stdout/stderr before raising, which would corrupt
    `--json` output.
    """
    try:
        from litellm import cost_per_token
    except ImportError:  # pragma: no cover - litellm is a hard dependency
        return None

    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            prompt_cost, completion_cost = cost_per_token(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
    except Exception:  # noqa: BLE001 - any failure means "unpriced", never a crash
        return None
    return _quantise(prompt_cost), _quantise(completion_cost)


def local_cost(model: str, prompt_tokens: int, completion_tokens: int):
    """Ask the pinned table. The escape hatch for models LiteLLM has never heard of."""
    price = LOCAL_PRICES.get(model)
    if price is None:
        return None
    return (
        _quantise(price.input_per_mtok * Decimal(prompt_tokens) / MTOK),
        _quantise(price.output_per_mtok * Decimal(completion_tokens) / MTOK),
    )


def pricing_key(model: str) -> str:
    """The id both price tables are keyed by: LiteLLM's "provider/model".

    Settings now carry LangChain's "provider:model" spelling. Only the first
    colon is the provider boundary, so a fine-tune id that contains colons
    keeps them.
    """
    head = model.split("/", 1)[0]
    if ":" in head:
        provider, _, rest = model.partition(":")
        return f"{provider}/{rest}"
    return model


def cost_for(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cost_fns: list[CostFn] | None = None,
) -> TokenCost:
    """Price one LLM call. Never raises; unknown models come back UNPRICED.

    `cost_fns` is injectable so tests can price a fake model without teaching
    LiteLLM about it, and so a deployment can put a gateway's own price
    endpoint first.
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts cannot be negative")
    # Zero tokens cost zero at any rate, so this needs no price. Without it a
    # run that consumed nothing -- the rule-based eval baseline, a cached
    # no-op -- would report "unpriced" and read as unknown rather than free.
    if prompt_tokens == 0 and completion_tokens == 0:
        return TokenCost(input_usd=ZERO_USD, output_usd=ZERO_USD)
    if not model:
        return UNPRICED

    key = pricing_key(model)
    for fn in cost_fns if cost_fns is not None else (litellm_cost, local_cost):
        result = fn(key, prompt_tokens, completion_tokens)
        if result is not None:
            return TokenCost(input_usd=result[0], output_usd=result[1])
    return UNPRICED


def total_cost(costs: list[TokenCost]) -> TokenCost:
    """Sum a run's calls. Unpriced anywhere means unpriced overall.

    Deliberately not "sum what we can and ignore the rest": a partial total
    presented as a total is a wrong number, and a wrong number is worse than a
    blank.
    """
    if not costs:
        return TokenCost(input_usd=ZERO_USD, output_usd=ZERO_USD)
    if any(not c.priced for c in costs):
        return UNPRICED
    return TokenCost(
        input_usd=sum((c.input_usd for c in costs), Decimal(0)).quantize(COST_PRECISION),
        output_usd=sum((c.output_usd for c in costs), Decimal(0)).quantize(COST_PRECISION),
    )


def format_usd(value: Decimal | None) -> str:
    """Six places: a single run of this agent costs a fraction of a cent."""
    return "unpriced" if value is None else f"${value:.6f}"
