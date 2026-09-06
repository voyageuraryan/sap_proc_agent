"""Cost arithmetic, and the honesty properties around it.

The interesting tests here are not the multiplications. They are:
  * an unknown model is UNPRICED, never zero
  * a partial total is UNPRICED, never a partial sum presented as a total
  * the pinned local price has not drifted from LiteLLM's map
"""

from decimal import Decimal

import pytest
from agent.cost import (
    COST_PRECISION,
    LOCAL_PRICES,
    UNPRICED,
    ModelPrice,
    TokenCost,
    cost_for,
    format_usd,
    litellm_cost,
    local_cost,
    total_cost,
)

MODEL = "anthropic/claude-sonnet-4-5"


def _fake_cost_fn(inp: str, out: str):
    def fn(model, prompt_tokens, completion_tokens):
        return Decimal(inp), Decimal(out)

    return fn


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------


def test_a_known_model_is_priced_from_the_local_table():
    price = LOCAL_PRICES[MODEL]
    cost = local_cost(MODEL, 1_000_000, 1_000_000)
    assert cost == (price.input_per_mtok, price.output_per_mtok)


def test_prices_scale_linearly_with_tokens():
    whole = local_cost(MODEL, 1_000_000, 1_000_000)
    tenth = local_cost(MODEL, 100_000, 100_000)
    assert tenth[0] * 10 == whole[0]
    assert tenth[1] * 10 == whole[1]


def test_costs_are_decimal_not_float():
    """The upstream price is a float; nothing downstream of cost.py is."""
    cost = cost_for(MODEL, 1000, 500)
    assert isinstance(cost.input_usd, Decimal)
    assert isinstance(cost.output_usd, Decimal)
    assert cost.input_usd == cost.input_usd.quantize(COST_PRECISION)


def test_input_and_output_are_kept_separate():
    """Output tokens cost multiples of input tokens; the split is the diagnosis."""
    cost = cost_for(MODEL, 1_000_000, 1_000_000)
    assert cost.output_usd > cost.input_usd


def test_zero_tokens_is_priced_at_zero_not_unpriced():
    cost = cost_for(MODEL, 0, 0)
    assert cost.priced
    assert cost.total_usd == Decimal("0").quantize(COST_PRECISION)


def test_negative_tokens_is_a_programming_error():
    with pytest.raises(ValueError):
        cost_for(MODEL, -1, 0)


# ---------------------------------------------------------------------------
# honesty
# ---------------------------------------------------------------------------


def test_zero_tokens_costs_zero_even_for_an_unknown_model():
    """Zero tokens cost zero at any rate, so no price is needed.

    Without this the rule-based eval baseline -- which consumes nothing --
    would report "unpriced", which reads as unknown rather than as free.
    """
    cost = cost_for("baseline/rules", 0, 0)
    assert cost.priced
    assert cost.total_usd == Decimal("0").quantize(COST_PRECISION)


def test_an_unknown_model_is_unpriced_not_free():
    """A silent zero would read as free. None reads as 'we do not know'."""
    cost = cost_for("test/scripted", 5000, 900)
    assert cost is UNPRICED or not cost.priced
    assert cost.total_usd is None
    assert format_usd(cost.total_usd) == "unpriced"


def test_an_empty_model_name_is_unpriced():
    assert not cost_for("", 100, 100).priced


def test_a_total_containing_an_unpriced_call_is_unpriced():
    """Summing what we can and ignoring the rest produces a WRONG number.

    A blank is a missing answer; a partial sum labelled 'total' is a false one.
    """
    priced = cost_for(MODEL, 1000, 1000)
    assert priced.priced
    assert not total_cost([priced, UNPRICED]).priced


def test_a_total_of_no_calls_is_zero():
    assert total_cost([]).total_usd == Decimal("0").quantize(COST_PRECISION)


def test_a_total_adds_up():
    one = TokenCost(input_usd=Decimal("0.01000000"), output_usd=Decimal("0.02000000"))
    assert total_cost([one, one, one]).total_usd == Decimal("0.09000000")


def test_the_first_source_that_answers_wins():
    """Injectable sources, so a gateway's own price endpoint can go first."""
    cost = cost_for("whatever", 1, 1, cost_fns=[_fake_cost_fn("1", "2"), _fake_cost_fn("9", "9")])
    assert (cost.input_usd, cost.output_usd) == (Decimal("1"), Decimal("2"))


def test_a_source_that_declines_falls_through_to_the_next():
    cost = cost_for("whatever", 1, 1, cost_fns=[lambda *_: None, _fake_cost_fn("3", "4")])
    assert (cost.input_usd, cost.output_usd) == (Decimal("3"), Decimal("4"))


def test_litellm_never_raises_on_an_unknown_model():
    """litellm prints a provider list and raises; that must not reach --json."""
    assert litellm_cost("no/such/model", 100, 100) is None


def test_litellm_chatter_does_not_reach_stdout(capsys):
    litellm_cost("no/such/model", 100, 100)
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------


def test_every_pinned_price_records_when_it_was_checked():
    """A price with no date is indistinguishable from a guess."""
    for model, price in LOCAL_PRICES.items():
        assert isinstance(price, ModelPrice), model
        assert price.checked_on, model
        assert price.source, model
        assert price.input_per_mtok > 0, model
        assert price.output_per_mtok > 0, model


@pytest.mark.parametrize("model", sorted(LOCAL_PRICES))
def test_pinned_prices_have_not_drifted_from_litellm(model):
    """A drift detector, deliberately exact.

    If this fails, provider pricing (or LiteLLM's map) changed. That SHOULD
    break the build of a system that reports cost to a human: re-check the
    provider's pricing page, update LOCAL_PRICES, and move `checked_on`.
    """
    upstream = litellm_cost(model, 1_000_000, 1_000_000)
    if upstream is None:
        pytest.skip(f"litellm does not know {model}; the pin is the only source")
    price = LOCAL_PRICES[model]
    assert upstream == (price.input_per_mtok, price.output_per_mtok), (
        f"{model}: litellm now says {upstream}, the pin says "
        f"({price.input_per_mtok}, {price.output_per_mtok}) as of {price.checked_on}"
    )


def test_litellm_is_consulted_before_the_local_pin():
    """The maintained source is the authority; the pin only fills gaps."""
    from agent import cost as cost_module

    order = []

    def spy(fn, label):
        def wrapped(*args):
            order.append(label)
            return fn(*args)

        return wrapped

    cost_for(
        MODEL,
        100,
        100,
        cost_fns=[
            spy(cost_module.litellm_cost, "litellm"),
            spy(cost_module.local_cost, "local"),
        ],
    )
    assert order[0] == "litellm"


def test_formatting_shows_six_places():
    assert format_usd(Decimal("0.00004800")) == "$0.000048"
    assert format_usd(None) == "unpriced"
