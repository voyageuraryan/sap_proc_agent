"""Tracing: the span tree, the backend choice, and the safety properties.

The load-bearing test in this file is
`test_tracing_does_not_change_the_run`. Everything else is about the span tree
being the right shape; that one is about instrumentation being *free* --
observability that alters behaviour is a heisenbug generator.
"""

import json
from decimal import Decimal

import pytest
from agent.cost import TokenCost
from agent.loop import StopReason, run_agent
from agent.schemas import Classification, Decision
from agent.settings import AgentSettings
from agent.tools import TERMINAL_TOOL
from agent.tracing import (
    LANGFUSE_ENV,
    SPAN_FIELDS,
    TRACED_SETTINGS,
    CompositeTracer,
    JsonlTracer,
    LangfuseTracer,
    NullTracer,
    build_tracer,
    langfuse_is_configured,
    settings_metadata,
)
from conftest import INVOICE, PO, RecordingTracer, ScriptedModel, calls, says
from conftest import call as tc

SUBMIT = {
    "classification": Classification.QUANTITY_EXCEEDS_RECEIPT.value,
    "decision": Decision.PROPOSE_CORRECTION.value,
    "reasoning": "Invoiced 14.000 against receipts of 13.000, outside a 5.0% tolerance.",
    "evidence": ["INV 5100000901 MENGE 14.000", "GR 5000000901 MENGE 13.000"],
    "correction": {
        "correction_type": "AMEND_INVOICE_QUANTITY",
        "invoice_number": INVOICE,
        "inv_item_number": "0001",
        "from_quantity": "14.000",
        "to_quantity": "13.000",
    },
}


def _script():
    return ScriptedModel(
        calls(tc("get_invoice", invoice_number=INVOICE)),
        calls(tc("get_goods_receipts", po_number=PO)),
        calls(tc(TERMINAL_TOOL, **SUBMIT)),
    )


# ---------------------------------------------------------------------------
# the property that matters
# ---------------------------------------------------------------------------


def test_tracing_does_not_change_the_run(erp_client, settings):
    """Same script, tracing off and on -> identical AgentRun.

    If instrumentation can change an outcome, every bug report becomes "does it
    still happen with tracing off?". Excluding only the trace fields
    themselves, the two runs must be byte-identical.
    """
    traced_settings = settings.model_copy(update={"tracing": True})

    untraced = run_agent(INVOICE, erp_client, settings, completion_fn=_script())
    traced = run_agent(
        INVOICE, erp_client, traced_settings, completion_fn=_script(), tracer=RecordingTracer()
    )

    drop = {"trace_backend", "trace_id", "trace_url"}
    a = _normalise(untraced.model_dump(mode="json", exclude=drop))
    b = _normalise(traced.model_dump(mode="json", exclude=drop))
    assert a == b


def _normalise(run: dict) -> dict:
    """Blank the two things that can never match between any two runs.

    Wall-clock durations, and the tool_call ids -- which a provider generates,
    so they are correctly different every time. Everything else must be equal.
    """
    for call in run["tool_calls"] + run["llm_calls"]:
        call["duration_ms"] = 0
    for message in run["messages"]:
        message.pop("tool_call_id", None)
        for call in message.get("tool_calls") or []:
            call.pop("id", None)
    return run


def test_a_failing_tracer_does_not_fail_the_run(erp_client, settings):
    """An observability tool that can take down the thing it observes is worse
    than no observability tool."""

    class ExplodingTracer(NullTracer):
        backend = "exploding"

        def span(self, name, *, kind, **fields):
            raise RuntimeError("backend on fire")

    with pytest.raises(RuntimeError):
        # The tracer itself is genuinely broken...
        run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=ExplodingTracer())

    # ...but the SUPPORTED failure mode -- a backend that errors on delivery --
    # is swallowed by the adapters. LangfuseTracer.span falls back to a no-op.
    class BrokenClient:
        def start_as_current_observation(self, **kwargs):
            raise RuntimeError("network down")

        def flush(self):
            raise RuntimeError("still down")

        def get_trace_url(self, trace_id):
            raise RuntimeError("down")

    tracer = LangfuseTracer(BrokenClient())
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    assert run.stop_reason is StopReason.SUBMITTED
    tracer.flush()  # must not raise
    assert tracer.trace_url is None


# ---------------------------------------------------------------------------
# the span tree
# ---------------------------------------------------------------------------


def test_the_span_tree_has_one_run_span_wrapping_everything(erp_client, settings, tracer):
    run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)

    assert len(tracer.of_kind("run")) == 1
    run_span = tracer.of_kind("run")[0]
    assert run_span["name"] == "agent.run"
    assert run_span["depth"] == 0
    assert all(s["depth"] >= 1 for s in tracer.spans[1:])


def test_one_llm_span_per_iteration_and_one_tool_span_per_call(erp_client, settings, tracer):
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)

    assert len(tracer.named("llm.completion")) == run.iterations == 3
    tool_spans = list(tracer.of_kind("tool"))
    assert [s["name"] for s in tool_spans] == [
        "tool.get_invoice",
        "tool.get_goods_receipts",
        f"tool.{TERMINAL_TOOL}",
    ]
    assert len(tool_spans) == len(run.tool_calls)


def test_llm_spans_are_generations_carrying_model_usage_and_cost(erp_client, tracer):
    priced = AgentSettings(
        model="anthropic/claude-sonnet-4-5", max_iterations=4, temperature=0.0, tracing=True
    )
    run_agent(INVOICE, erp_client, priced, completion_fn=_script(), tracer=tracer)

    for span in tracer.named("llm.completion"):
        assert span["kind"] == "llm"  # -> a Langfuse *generation*, not a plain span
        assert span["model"] == "anthropic/claude-sonnet-4-5"
        assert span["usage"] == {"input": 100, "output": 20}
        assert isinstance(span["cost"], TokenCost)
        assert span["cost"].priced


def test_llm_spans_record_provider_time_separately_from_span_time(erp_client, settings, tracer):
    """Span time minus provider_ms is the loop's own overhead.

    Without both numbers, bookkeeping done inside the span (usage accounting,
    the price lookup) is silently attributed to the model.
    """
    run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    spans = tracer.named("llm.completion")
    assert spans
    for span in spans:
        assert "provider_ms" in span["metadata"]
        assert span["metadata"]["provider_ms"] >= 0


def test_an_unpriced_model_still_reports_usage(erp_client, settings, tracer):
    """Tokens are always known; only the price may be missing."""
    run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    for span in tracer.named("llm.completion"):
        assert span["usage"]["input"] == 100
        assert not span["cost"].priced


def test_a_failed_tool_call_marks_its_span_with_the_error_code(erp_client, settings, tracer):
    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number="5199999999")),
        calls(tc(TERMINAL_TOOL, **SUBMIT)),
    )
    run_agent(INVOICE, erp_client, settings, completion_fn=model, tracer=tracer)

    failed = tracer.named("tool.get_invoice")[0]
    assert failed["error"] == "INVOICE_NOT_FOUND"
    # And the successful terminal call is not marked.
    assert tracer.named(f"tool.{TERMINAL_TOOL}")[0].get("error") is None


def test_a_rejected_resolution_marks_its_span(erp_client, settings, tracer):
    broken = dict(SUBMIT)
    broken.pop("correction")
    model = ScriptedModel(calls(tc(TERMINAL_TOOL, **broken)), calls(tc(TERMINAL_TOOL, **SUBMIT)))
    run_agent(INVOICE, erp_client, settings, completion_fn=model, tracer=tracer)

    spans = tracer.named(f"tool.{TERMINAL_TOOL}")
    assert spans[0]["error"] == "RESOLUTION_INVALID"
    assert spans[1].get("error") is None


def test_a_run_that_never_submitted_is_an_error_level_trace(erp_client, settings, tracer):
    """So it is findable in the UI without knowing what to search for."""
    model = ScriptedModel(says("thinking"), says("still thinking"))
    run_agent(INVOICE, erp_client, settings, completion_fn=model, tracer=tracer)
    assert tracer.of_kind("run")[0]["error"] == "NO_TOOL_CALL"


def test_a_submitted_run_carries_the_verdict_on_the_run_span(erp_client, settings, tracer):
    run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    run_span = tracer.of_kind("run")[0]
    assert run_span.get("error") is None
    assert run_span["output"]["decision"] == "PROPOSE_CORRECTION"
    assert run_span["output"]["classification"] == "QUANTITY_EXCEEDS_RECEIPT"
    assert run_span["metadata"]["iterations"] == 3
    assert run_span["metadata"]["tool_calls"] == 3


def test_the_trace_id_and_url_reach_the_agent_run(erp_client, settings, tracer):
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    assert run.trace_backend == "recording"
    assert run.trace_id == "trace-test"
    assert run.trace_url == "https://langfuse.test/t/trace-test"


def test_every_span_field_the_loop_sets_is_declared(erp_client, settings, tracer):
    """Fields not in SPAN_FIELDS are silently dropped by the backends, so a
    typo'd field name would vanish rather than fail. This catches it."""
    run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    for span in tracer.spans:
        unknown = set(span) - SPAN_FIELDS - {"kind", "depth"}
        assert unknown == set(), f"{span['name']} sets undeclared field(s) {unknown}"


# ---------------------------------------------------------------------------
# what is allowed into a span
# ---------------------------------------------------------------------------


def test_settings_metadata_is_an_allow_list():
    """Blocklisting secrets means being right forever; allow-listing means
    being right once."""
    settings = AgentSettings(model="anthropic/claude-sonnet-4-5")
    metadata = settings_metadata(settings)
    assert set(metadata) <= set(TRACED_SETTINGS)
    assert "trace_release" not in metadata


def test_no_credential_shaped_field_can_reach_a_span():
    """A key added to AgentSettings later must not leak by default."""
    for name in TRACED_SETTINGS:
        assert not any(
            word in name for word in ("key", "secret", "token", "password", "credential")
        ), name
    assert "api_key" not in AgentSettings.model_fields  # secrets never enter settings


def test_payloads_can_be_redacted(erp_client, tracer):
    """The ERP data here is synthetic. It will not always be."""
    redacting = AgentSettings(
        model="test/scripted", max_iterations=4, tracing=True, trace_payloads=False
    )
    run = run_agent(INVOICE, erp_client, redacting, completion_fn=_script(), tracer=tracer)

    for span in tracer.of_kind("llm") + tracer.of_kind("tool"):
        assert span.get("input") == "<redacted>", span["name"]
        # "accepted" is a literal this code wrote, not data from anywhere, so
        # it is not redacted. Everything data-derived is.
        assert span.get("output") in ("<redacted>", "accepted"), span["name"]
    assert any(s.get("output") == "<redacted>" for s in tracer.of_kind("tool"))
    # Redaction affects the TRACE only. The transcript the model saw is intact.
    assert any("MENGE" in m.get("content", "") for m in run.messages if m.get("role") == "tool")


def test_the_ground_truth_label_never_reaches_a_span(erp_client, settings, tracer):
    run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)
    blob = json.dumps(tracer.spans, default=str)
    for label in ("PRICE_MINOR", "PRICE_MAJOR", "QTY_OVER", "GR_PARTIAL", "DUP_INVOICE"):
        assert label not in blob


# ---------------------------------------------------------------------------
# choosing a backend
# ---------------------------------------------------------------------------


def test_tracing_off_gives_a_null_tracer():
    tracer = build_tracer(AgentSettings(tracing=False), environ={})
    assert isinstance(tracer, NullTracer)
    assert tracer.backend == "none"
    assert tracer.trace_id is None


def test_a_null_span_swallows_everything():
    with NullTracer().span("x", kind="run", input={"a": 1}) as span:
        span.update(output="anything", nonsense=object())
        assert span.trace_id is None


def test_a_trace_file_gives_a_jsonl_tracer(tmp_path):
    tracer = build_tracer(AgentSettings(tracing=True, trace_file=tmp_path / "t.jsonl"), environ={})
    assert isinstance(tracer, JsonlTracer)
    assert tracer.backend == "jsonl"


def test_langfuse_needs_both_keys():
    assert not langfuse_is_configured({})
    assert not langfuse_is_configured({"LANGFUSE_PUBLIC_KEY": "pk"})
    assert langfuse_is_configured(dict.fromkeys(LANGFUSE_ENV, "x"))


def test_no_backend_configured_falls_back_to_null(tmp_path):
    assert isinstance(build_tracer(AgentSettings(tracing=True), environ={}), NullTracer)


def test_two_backends_compose(tmp_path, monkeypatch):
    both = build_tracer(
        AgentSettings(tracing=True, trace_file=tmp_path / "t.jsonl"),
        environ=dict.fromkeys(LANGFUSE_ENV, "x"),
    )
    # Langfuse may or may not construct here depending on the SDK's own checks;
    # either way the jsonl backend must be present and nothing may raise.
    assert "jsonl" in both.backend
    if isinstance(both, CompositeTracer):
        assert "langfuse" in both.backend


# ---------------------------------------------------------------------------
# the local file backend
# ---------------------------------------------------------------------------


def test_the_jsonl_file_holds_one_object_per_span(erp_client, tmp_path):
    path = tmp_path / "traces" / "run.jsonl"
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True, trace_file=path)
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script())

    assert run.trace_backend == "jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 + run.iterations + len(run.tool_calls)
    assert all("duration_ms" in line for line in lines)


def test_the_jsonl_file_closes_children_before_parents(erp_client, tmp_path):
    """Written on close, so the file reads bottom-up like a flame graph, and a
    crashed run still leaves everything that completed."""
    path = tmp_path / "run.jsonl"
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True, trace_file=path)
    run_agent(INVOICE, erp_client, settings, completion_fn=_script())

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["name"] == "agent.run"
    assert lines[-1]["depth"] == 0
    assert lines[0]["depth"] == 1


def test_the_jsonl_file_serialises_cost_as_plain_numbers(erp_client, tmp_path):
    path = tmp_path / "run.jsonl"
    settings = AgentSettings(
        model="anthropic/claude-sonnet-4-5", max_iterations=4, tracing=True, trace_file=path
    )
    run_agent(INVOICE, erp_client, settings, completion_fn=_script())

    llm = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "llm"
    ]
    assert llm
    assert set(llm[0]["cost"]) == {"input", "output", "total"}
    assert llm[0]["cost"]["total"] > 0


def test_a_directory_that_cannot_be_written_does_not_fail_the_run(erp_client, tmp_path):
    """Losing a trace line is the correct thing to lose."""
    path = tmp_path / "run.jsonl"
    tracer = JsonlTracer(path)
    path.mkdir()  # now opening it as a file raises OSError
    with tracer.span("x", kind="run") as span:
        span.update(output="y")
    # no exception


# ---------------------------------------------------------------------------
# the cost table on the run
# ---------------------------------------------------------------------------


def test_the_run_carries_one_llm_record_per_iteration(erp_client, settings):
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script())
    assert [c.iteration for c in run.llm_calls] == [1, 2, 3]
    assert [c.tool_calls_requested for c in run.llm_calls] == [1, 1, 1]
    assert sum(c.prompt_tokens for c in run.llm_calls) == run.prompt_tokens
    assert sum(c.completion_tokens for c in run.llm_calls) == run.completion_tokens


def test_a_priced_model_totals_the_per_call_costs(erp_client):
    priced = AgentSettings(model="anthropic/claude-sonnet-4-5", max_iterations=4, tracing=False)
    run = run_agent(INVOICE, erp_client, priced, completion_fn=_script())
    assert run.total_usd is not None
    assert run.total_usd == sum(c.total_usd for c in run.llm_calls)
    assert run.total_usd == run.input_usd + run.output_usd


def test_an_unpriced_model_reports_none_not_zero(erp_client, settings):
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script())
    assert run.total_usd is None
    assert all(c.total_usd is None for c in run.llm_calls)


def test_cost_survives_json_round_trip(erp_client):
    """Decimal serialises as a string, like every other money field in the repo."""
    priced = AgentSettings(model="anthropic/claude-sonnet-4-5", max_iterations=4, tracing=False)
    run = run_agent(INVOICE, erp_client, priced, completion_fn=_script())
    reloaded = json.loads(run.model_dump_json())
    assert isinstance(reloaded["total_usd"], str)
    assert reloaded["llm_calls"][0]["prompt_tokens"] == 100


# ---------------------------------------------------------------------------
# the Langfuse adapter, against the real SDK
# ---------------------------------------------------------------------------


@pytest.fixture
def langfuse_client(monkeypatch):
    """A real Langfuse client pointed at a dead host.

    The point is to exercise the ACTUAL SDK surface -- start_as_current_
    observation, as_type, usage_details, cost_details -- rather than my idea of
    it. Delivery fails in a background exporter thread, which is exactly the
    failure mode a laptop with no network has, and must be harmless.
    """
    langfuse = pytest.importorskip("langfuse")
    for name in LANGFUSE_ENV:
        monkeypatch.setenv(name, "test-key")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:1")
    client = langfuse.Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        host="http://127.0.0.1:1",
        environment="pytest",
        flush_at=512,  # large enough that nothing leaves the process mid-test
    )
    yield client
    client.shutdown()


def test_the_langfuse_adapter_matches_the_real_sdk(erp_client, langfuse_client):
    """An unreachable backend must produce a complete run and a real trace id."""
    tracer = LangfuseTracer(langfuse_client)
    settings = AgentSettings(model="anthropic/claude-sonnet-4-5", max_iterations=4, tracing=True)
    run = run_agent(INVOICE, erp_client, settings, completion_fn=_script(), tracer=tracer)

    assert run.stop_reason is StopReason.SUBMITTED
    assert run.trace_backend == "langfuse"
    assert run.trace_id
    assert len(run.trace_id) == 32
    # The URL needs the project id, which the SDK resolves over the API. With
    # the backend unreachable it is legitimately None -- and that must not
    # break the run or the CLI's printing.
    assert run.trace_url is None or run.trace_id in run.trace_url


def test_langfuse_generations_accept_usage_and_cost(langfuse_client):
    """Guards the field names: usage_details / cost_details are SDK spellings.

    A typo here would be silently dropped by the SDK and the cost column in
    the Langfuse UI would just be empty, with nothing failing anywhere.
    """
    tracer = LangfuseTracer(langfuse_client)
    cost = TokenCost(input_usd=Decimal("0.01"), output_usd=Decimal("0.02"))
    with tracer.span("llm.completion", kind="llm", model="anthropic/claude-sonnet-4-5") as span:
        span.update(
            output={"role": "assistant"},
            usage={"input": 10, "output": 5},
            cost=cost,
            metadata={"iteration": 1},
        )
    assert tracer.trace_id
