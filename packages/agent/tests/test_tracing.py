"""Tracing: the callback stream, the backend choice, and the safety properties.

The load-bearing test in this file is
`test_tracing_does_not_change_the_run`. Everything else is about the trace
being the right shape; that one is about instrumentation being *free* --
observability that alters behaviour is a heisenbug generator.

Since the LangChain rebuild, "a span" is a LangChain run: the graph, each
node, each chat-model call and each tool call emit callbacks, and a backend
is just a handler. These tests read that stream through an in-memory handler
(conftest.RecordingHandler), and exercise the real Langfuse handler against
an unreachable host.
"""

import json

import pytest
from agent.erp_client import ErpError
from agent.graph import RUN_NAME
from agent.loop import StopReason, run_agent
from agent.schemas import Classification, Decision
from agent.settings import AgentSettings
from agent.tools import TERMINAL_TOOL, tool_error_code
from agent.tracing import (
    LANGFUSE_ENV,
    REDACTED,
    TRACED_SETTINGS,
    CompositeTracer,
    JsonlTracer,
    LangfuseTracer,
    NullTracer,
    _redact,
    build_tracer,
    langfuse_is_configured,
    settings_metadata,
)
from conftest import INVOICE, PO, RecordingTracer, ScriptedModel, calls, says
from conftest import call as tc
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import ValidationError

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

PRICED = "anthropic:claude-sonnet-4-5"


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

    untraced = run_agent(INVOICE, erp_client, settings, chat_model=_script())
    traced = run_agent(
        INVOICE, erp_client, traced_settings, chat_model=_script(), tracer=RecordingTracer()
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


def test_a_failing_handler_does_not_fail_the_run(erp_client, settings):
    """An observability tool that can take down the thing it observes is worse
    than no observability tool. LangChain's callback manager isolates handler
    errors; this pins that we never opted out of it."""

    class ExplodingHandler(BaseCallbackHandler):
        def on_chain_start(self, *args, **kwargs):
            raise RuntimeError("backend on fire")

        def on_chat_model_start(self, *args, **kwargs):
            raise RuntimeError("backend on fire")

        def on_tool_start(self, *args, **kwargs):
            raise RuntimeError("backend on fire")

    class ExplodingTracer(NullTracer):
        backend = "exploding"

        def callbacks(self):
            return [ExplodingHandler()]

    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=ExplodingTracer())
    assert run.stop_reason is StopReason.SUBMITTED


def test_an_unreachable_langfuse_does_not_fail_the_run(erp_client, settings):
    """The SUPPORTED failure mode -- a backend that errors on delivery -- is
    swallowed: outcome scores, the URL lookup and the flush all degrade."""

    class BrokenClient:
        def create_score(self, **kwargs):
            raise RuntimeError("network down")

        def flush(self):
            raise RuntimeError("still down")

        def get_trace_url(self, trace_id):
            raise RuntimeError("down")

    class DeadHandler(BaseCallbackHandler):
        last_trace_id = "a" * 32

    tracer = LangfuseTracer(BrokenClient(), handler=DeadHandler())
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    assert run.stop_reason is StopReason.SUBMITTED
    tracer.flush()  # must not raise
    assert tracer.trace_url is None
    assert run.trace_id == "a" * 32


# ---------------------------------------------------------------------------
# the trace tree
# ---------------------------------------------------------------------------


def test_the_trace_has_one_root_run_wrapping_everything(erp_client, settings, tracer):
    run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)

    roots = [r for r in tracer.runs if r["depth"] == 0]
    assert len(roots) == 1
    assert roots[0]["name"] == RUN_NAME
    assert roots[0]["kind"] == "chain"
    assert tracer.runs[0] is roots[0]


def test_one_llm_run_per_iteration_and_one_tool_run_per_call(erp_client, settings, tracer):
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)

    assert len(tracer.of_kind("llm")) == run.iterations == 3
    assert [r["name"] for r in tracer.of_kind("tool")] == [
        "get_invoice",
        "get_goods_receipts",
        TERMINAL_TOOL,
    ]
    assert len(tracer.of_kind("tool")) == len(run.tool_calls)


def test_the_graph_nodes_are_the_steps_and_the_routing_is_hidden(erp_client, settings, tracer):
    """The trace reads as agent -> tools -> agent ..., not as edge plumbing."""
    run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    nodes = [r["name"] for r in tracer.of_kind("chain") if r["depth"] == 1]
    assert nodes == ["agent", "tools"] * 3
    assert not tracer.named("after_agent")
    assert not tracer.named("continue_or_stop")


def test_llm_runs_nest_under_the_agent_node_and_tools_under_the_tool_node(
    erp_client, settings, tracer
):
    run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    assert all(r["depth"] == 2 for r in tracer.of_kind("llm"))
    assert all(r["depth"] == 2 for r in tracer.of_kind("tool"))


def test_llm_runs_carry_token_usage(erp_client, settings, tracer):
    """Tokens are always known; only the price may be missing."""
    run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    for llm in tracer.of_kind("llm"):
        assert llm["usage"]["input_tokens"] == 100
        assert llm["usage"]["output_tokens"] == 20


def test_a_failed_tool_call_marks_its_run_with_the_error(erp_client, settings, tracer):
    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number="5199999999")),
        calls(tc(TERMINAL_TOOL, **SUBMIT)),
    )
    run_agent(INVOICE, erp_client, settings, chat_model=model, tracer=tracer)

    failed = tracer.named("get_invoice")[0]
    assert isinstance(failed["error"], ErpError)
    assert tool_error_code("get_invoice", failed["error"]) == "INVOICE_NOT_FOUND"
    # And the successful terminal call is not marked.
    assert "error" not in tracer.named(TERMINAL_TOOL)[0]


def test_a_rejected_resolution_marks_its_run(erp_client, settings, tracer):
    broken = dict(SUBMIT)
    broken.pop("correction")
    model = ScriptedModel(calls(tc(TERMINAL_TOOL, **broken)), calls(tc(TERMINAL_TOOL, **SUBMIT)))
    run_agent(INVOICE, erp_client, settings, chat_model=model, tracer=tracer)

    runs = tracer.named(TERMINAL_TOOL)
    assert isinstance(runs[0]["error"], ValidationError)
    assert tool_error_code(TERMINAL_TOOL, runs[0]["error"]) == "RESOLUTION_INVALID"
    assert "error" not in runs[1]


def test_the_outcome_is_handed_to_the_tracer_once_the_run_is_over(erp_client, settings, tracer):
    """Langfuse gets it as scores, so a run that never submitted is a filter."""
    model = ScriptedModel(says("thinking"), says("still thinking"))
    run = run_agent(INVOICE, erp_client, settings, chat_model=model, tracer=tracer)
    assert tracer.outcomes == [run]
    assert tracer.outcomes[0].stop_reason is StopReason.NO_TOOL_CALL


def test_the_trace_id_and_url_reach_the_agent_run(erp_client, settings, tracer):
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    assert run.trace_backend == "recording"
    assert run.trace_id == "trace-test"
    assert run.trace_url == "https://langfuse.test/t/trace-test"


def test_the_root_run_carries_scenario_and_allow_listed_settings(erp_client, settings, tracer):
    run_agent(
        INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer, scenario_id="SC-0009"
    )
    metadata = tracer.runs[0]["metadata"]
    assert metadata["scenario_id"] == "SC-0009"
    assert metadata["invoice_number"] == INVOICE
    assert metadata["model"] == settings.model
    assert "trace_release" not in metadata


# ---------------------------------------------------------------------------
# what is allowed into a trace
# ---------------------------------------------------------------------------


def test_settings_metadata_is_an_allow_list():
    """Blocklisting secrets means being right forever; allow-listing means
    being right once."""
    settings = AgentSettings(model=PRICED)
    metadata = settings_metadata(settings)
    assert set(metadata) <= set(TRACED_SETTINGS)
    assert "trace_release" not in metadata


def test_no_credential_shaped_field_can_reach_a_trace():
    """A key added to AgentSettings later must not leak by default."""
    for name in TRACED_SETTINGS:
        assert not any(
            word in name for word in ("key", "secret", "token", "password", "credential")
        ), name
    assert "api_key" not in AgentSettings.model_fields  # secrets never enter settings


def test_payloads_can_be_redacted_in_the_jsonl_trace(erp_client, tmp_path):
    """The ERP data here is synthetic. It will not always be."""
    path = tmp_path / "run.jsonl"
    redacting = AgentSettings(
        model="test/scripted", max_iterations=4, tracing=True, trace_payloads=False, trace_file=path
    )
    run = run_agent(INVOICE, erp_client, redacting, chat_model=_script())

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for line in (entry for entry in lines if entry["kind"] in ("llm", "tool")):
        assert line["input"] == REDACTED, line["name"]
        assert line["output"] == REDACTED, line["name"]
    assert "MENGE" not in path.read_text(encoding="utf-8")
    # Redaction affects the TRACE only. The transcript the model saw is intact.
    assert any("MENGE" in m.get("content", "") for m in run.messages if m.get("role") == "tool")


def test_langfuse_is_given_a_mask_when_payloads_are_off(monkeypatch):
    """Langfuse redaction is the SDK's own mask hook, so it covers every
    observation the handler creates -- not only the ones this code touches."""
    import langfuse

    seen: dict = {}

    class FakeLangfuse:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(langfuse, "Langfuse", FakeLangfuse)
    monkeypatch.setattr(
        "langfuse.langchain.CallbackHandler", lambda public_key=None: BaseCallbackHandler()
    )
    build_tracer(
        AgentSettings(tracing=True, trace_payloads=False),
        environ=dict.fromkeys(LANGFUSE_ENV, "x"),
    )
    assert seen["mask"] is _redact
    assert _redact(data={"prompt": "MENGE 14.000"}) == REDACTED

    seen.clear()
    build_tracer(AgentSettings(tracing=True), environ=dict.fromkeys(LANGFUSE_ENV, "x"))
    assert seen["mask"] is None


def test_the_ground_truth_label_never_reaches_a_trace(erp_client, tmp_path):
    path = tmp_path / "run.jsonl"
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True, trace_file=path)
    run_agent(INVOICE, erp_client, settings, chat_model=_script())
    blob = path.read_text(encoding="utf-8")
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
    assert tracer.callbacks() == []


def test_a_trace_file_gives_a_jsonl_tracer(tmp_path):
    tracer = build_tracer(AgentSettings(tracing=True, trace_file=tmp_path / "t.jsonl"), environ={})
    assert isinstance(tracer, JsonlTracer)
    assert tracer.backend == "jsonl"
    assert len(tracer.callbacks()) == 1


def test_langfuse_needs_both_keys():
    assert not langfuse_is_configured({})
    assert not langfuse_is_configured({"LANGFUSE_PUBLIC_KEY": "pk"})
    assert langfuse_is_configured(dict.fromkeys(LANGFUSE_ENV, "x"))


def test_no_backend_configured_falls_back_to_null():
    assert isinstance(build_tracer(AgentSettings(tracing=True), environ={}), NullTracer)


def test_two_backends_compose(tmp_path):
    both = build_tracer(
        AgentSettings(tracing=True, trace_file=tmp_path / "t.jsonl"),
        environ=dict.fromkeys(LANGFUSE_ENV, "x"),
    )
    # Langfuse may or may not construct here depending on the SDK's own checks;
    # either way the jsonl backend must be present and nothing may raise.
    assert "jsonl" in both.backend
    if isinstance(both, CompositeTracer):
        assert "langfuse" in both.backend
        assert len(both.callbacks()) == 2


# ---------------------------------------------------------------------------
# the local file backend
# ---------------------------------------------------------------------------


def test_the_jsonl_file_holds_one_object_per_run(erp_client, tmp_path):
    path = tmp_path / "traces" / "run.jsonl"
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True, trace_file=path)
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script())

    assert run.trace_backend == "jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kinds = [line["kind"] for line in lines]
    assert kinds.count("llm") == run.iterations
    assert kinds.count("tool") == len(run.tool_calls)
    # the root, plus one agent node and one tools node per iteration
    assert kinds.count("chain") == 1 + 2 * run.iterations
    assert all("duration_ms" in line for line in lines)


def test_the_jsonl_file_closes_children_before_parents(erp_client, tmp_path):
    """Written on close, so the file reads bottom-up like a flame graph, and a
    crashed run still leaves everything that completed."""
    path = tmp_path / "run.jsonl"
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True, trace_file=path)
    run_agent(INVOICE, erp_client, settings, chat_model=_script())

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["name"] == RUN_NAME
    assert lines[-1]["depth"] == 0
    assert lines[0]["depth"] == 2


def test_the_jsonl_file_records_tool_errors_by_code(erp_client, tmp_path):
    path = tmp_path / "run.jsonl"
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True, trace_file=path)
    model = ScriptedModel(
        calls(tc("get_invoice", invoice_number="5199999999")),
        calls(tc(TERMINAL_TOOL, **SUBMIT)),
    )
    run_agent(INVOICE, erp_client, settings, chat_model=model)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    tool = next(line for line in lines if line["name"] == "get_invoice")
    assert tool["error"] == "INVOICE_NOT_FOUND"


def test_the_jsonl_file_serialises_cost_as_plain_numbers(erp_client, tmp_path):
    path = tmp_path / "run.jsonl"
    settings = AgentSettings(model=PRICED, max_iterations=4, tracing=True, trace_file=path)
    run_agent(INVOICE, erp_client, settings, chat_model=_script())

    llm = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "llm"
    ]
    assert llm
    assert set(llm[0]["cost"]) == {"input", "output", "total"}
    assert llm[0]["cost"]["total"] > 0
    assert llm[0]["model"] == PRICED


def test_a_directory_that_cannot_be_written_does_not_fail_the_run(erp_client, tmp_path):
    """Losing a trace line is the correct thing to lose."""
    path = tmp_path / "run.jsonl"
    tracer = JsonlTracer(path)
    path.mkdir()  # now opening it as a file raises OSError
    settings = AgentSettings(model="test/scripted", max_iterations=4, tracing=True)
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    assert run.stop_reason is StopReason.SUBMITTED


# ---------------------------------------------------------------------------
# the cost table on the run
# ---------------------------------------------------------------------------


def test_the_run_carries_one_llm_record_per_iteration(erp_client, settings):
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script())
    assert [c.iteration for c in run.llm_calls] == [1, 2, 3]
    assert [c.tool_calls_requested for c in run.llm_calls] == [1, 1, 1]
    assert sum(c.prompt_tokens for c in run.llm_calls) == run.prompt_tokens
    assert sum(c.completion_tokens for c in run.llm_calls) == run.completion_tokens


def test_a_priced_model_totals_the_per_call_costs(erp_client):
    priced = AgentSettings(model=PRICED, max_iterations=4, tracing=False)
    run = run_agent(INVOICE, erp_client, priced, chat_model=_script())
    assert run.total_usd is not None
    assert run.total_usd == sum(c.total_usd for c in run.llm_calls)
    assert run.total_usd == run.input_usd + run.output_usd


def test_an_unpriced_model_reports_none_not_zero(erp_client, settings):
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script())
    assert run.total_usd is None
    assert all(c.total_usd is None for c in run.llm_calls)


def test_cost_survives_json_round_trip(erp_client):
    """Decimal serialises as a string, like every other money field in the repo."""
    priced = AgentSettings(model=PRICED, max_iterations=4, tracing=False)
    run = run_agent(INVOICE, erp_client, priced, chat_model=_script())
    reloaded = json.loads(run.model_dump_json())
    assert isinstance(reloaded["total_usd"], str)
    assert reloaded["llm_calls"][0]["prompt_tokens"] == 100


# ---------------------------------------------------------------------------
# the Langfuse handler, against the real SDK
# ---------------------------------------------------------------------------


@pytest.fixture
def langfuse_client(monkeypatch):
    """A real Langfuse client pointed at a dead host.

    The point is to exercise the ACTUAL SDK surface -- Langfuse's own LangChain
    CallbackHandler and trace ids -- rather than my idea of it. Span delivery
    fails in a background exporter thread, which is exactly the failure mode a
    laptop with no network has, and must be harmless.

    `create_score` is the one call intercepted: the kwargs are BOUND against
    the real method's signature (so a misspelt argument still fails here) and
    recorded instead of queued, so nothing waits on delivery to the dead host.

    Each test gets its OWN public key. The SDK keeps one resource manager per
    key for the life of the process, so a second client under a key whose
    manager was already shut down enqueues onto a queue nobody consumes, and
    the next shutdown() joins it forever.
    """
    import inspect
    import uuid

    langfuse = pytest.importorskip("langfuse")
    for name in LANGFUSE_ENV:
        monkeypatch.setenv(name, "test-key")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:1")
    public_key = f"pk-lf-test-{uuid.uuid4().hex[:12]}"
    client = langfuse.Langfuse(
        public_key=public_key,
        secret_key="sk-lf-test",
        host="http://127.0.0.1:1",
        environment="pytest",
        timeout=1,
        flush_at=512,  # large enough that nothing leaves the process mid-test
    )
    signature = inspect.signature(client.create_score)
    recorded: list[dict] = []

    def create_score(**kwargs):
        signature.bind(**kwargs)
        recorded.append(kwargs)

    monkeypatch.setattr(client, "create_score", create_score)
    client.recorded_scores = recorded
    client.test_public_key = public_key
    yield client
    client.shutdown()


def test_the_langfuse_handler_traces_a_real_run(erp_client, langfuse_client):
    """An unreachable backend must produce a complete run and a real trace id."""
    tracer = LangfuseTracer(langfuse_client, public_key=langfuse_client.test_public_key)
    settings = AgentSettings(model=PRICED, max_iterations=4, tracing=True)
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)

    assert run.stop_reason is StopReason.SUBMITTED
    assert run.trace_backend == "langfuse"
    assert run.trace_id
    assert len(run.trace_id) == 32
    # The URL needs the project id, which the SDK resolves over the API. With
    # the backend unreachable it is legitimately None -- and that must not
    # break the run or the CLI's printing.
    assert run.trace_url is None or run.trace_id in run.trace_url


def test_each_run_gets_its_own_langfuse_trace(erp_client, langfuse_client):
    """One tracer serves a whole eval split; runs must not share a trace."""
    tracer = LangfuseTracer(langfuse_client, public_key=langfuse_client.test_public_key)
    settings = AgentSettings(model=PRICED, max_iterations=4, tracing=True)
    first = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    second = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)
    assert first.trace_id
    assert second.trace_id
    assert first.trace_id != second.trace_id


def test_the_outcome_lands_on_the_trace_as_scores(erp_client, langfuse_client):
    """So "every run that never submitted" is a filter in the Langfuse UI.

    The fixture binds each call against the real create_score signature, so
    this also guards the SDK spelling -- a typo would otherwise be swallowed
    by the suppress() in record_outcome, and the scores would silently vanish.
    """
    tracer = LangfuseTracer(langfuse_client, public_key=langfuse_client.test_public_key)
    settings = AgentSettings(model=PRICED, max_iterations=4, tracing=True)
    run = run_agent(INVOICE, erp_client, settings, chat_model=_script(), tracer=tracer)

    scores = {s["name"]: s for s in langfuse_client.recorded_scores}
    assert scores["stop_reason"]["value"] == "SUBMITTED"
    assert scores["decision"]["value"] == "PROPOSE_CORRECTION"
    assert scores["classification"]["value"] == "QUANTITY_EXCEEDS_RECEIPT"
    assert all(s["trace_id"] == run.trace_id for s in scores.values())
    assert all(s["data_type"] == "CATEGORICAL" for s in scores.values())
