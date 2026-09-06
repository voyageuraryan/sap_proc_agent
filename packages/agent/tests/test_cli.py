"""The CLI: argument handling, output modes, exit codes.

main() is deliberately thin, so there is little to test -- which is the point.
What IS tested is the contract with the outside world: the flags, the exit
code, and that --json emits something a machine can consume.
"""

import json
from decimal import Decimal

import pytest
from agent import cli
from agent.loop import AgentRun, LlmCallRecord, StopReason
from agent.schemas import Classification, Decision, Resolution
from agent.tracing import JsonlTracer, NullTracer
from conftest import INVOICE


def _run(**kwargs) -> AgentRun:
    defaults = dict(
        invoice_number=INVOICE,
        model="test/scripted",
        stop_reason=StopReason.SUBMITTED,
        iterations=3,
        resolution=Resolution(
            classification=Classification.PARTIAL_DELIVERY,
            decision=Decision.ESCALATE,
            reasoning="Receipts total less than invoiced; both readings are defensible.",
            evidence=["INV MENGE 14.000", "GR MENGE 13.000"],
            escalate_to="AP_SUPERVISOR",
            escalation_reason="Cannot distinguish a partial delivery from an over-invoice.",
        ),
        prompt_tokens=1200,
        completion_tokens=300,
        llm_calls=[
            LlmCallRecord(
                iteration=1,
                model="test/scripted",
                prompt_tokens=400,
                completion_tokens=100,
                duration_ms=812.0,
                input_usd=Decimal("0.00240000"),
                output_usd=Decimal("0.00225000"),
                tool_calls_requested=1,
            ),
            LlmCallRecord(
                iteration=2,
                model="test/scripted",
                prompt_tokens=800,
                completion_tokens=200,
                duration_ms=904.0,
                input_usd=Decimal("0.00480000"),
                output_usd=Decimal("0.00450000"),
                tool_calls_requested=1,
            ),
        ],
        input_usd=Decimal("0.00720000"),
        output_usd=Decimal("0.00675000"),
        total_usd=Decimal("0.01395000"),
        trace_backend="jsonl",
    )
    defaults.update(kwargs)
    return AgentRun(**defaults)


@pytest.fixture
def patched(monkeypatch, erp_app):
    """Replace run_agent and ErpClient so the CLI can be driven without a model."""
    captured: dict = {}

    class FakeClient:
        def __init__(self, base_url, timeout):
            captured["base_url"] = base_url
            captured["timeout"] = timeout
            captured["closed"] = False

        def close(self):
            captured["closed"] = True

    def fake_run_agent(invoice_number, client, settings, *, scenario_id=None, tracer=None, **kw):
        captured["invoice_number"] = invoice_number
        captured["settings"] = settings
        captured["scenario_id"] = scenario_id
        captured["tracer"] = tracer
        return captured.get("result") or _run(invoice_number=invoice_number)

    monkeypatch.setattr(cli, "ErpClient", FakeClient)
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    return captured


def test_invoice_is_required(patched):
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2  # argparse's usage error


def test_a_submitted_run_exits_zero(patched, capsys):
    assert cli.main(["--invoice", INVOICE]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert INVOICE in out
    assert "ESCALATE" in out
    assert "AP_SUPERVISOR" in out


def test_a_run_that_never_submitted_exits_nonzero(patched, capsys):
    patched["result"] = _run(stop_reason=StopReason.MAX_ITERATIONS, resolution=None)
    assert cli.main(["--invoice", INVOICE]) == cli.EXIT_NOT_SUBMITTED
    assert "no resolution was submitted" in capsys.readouterr().out


def test_json_mode_emits_a_parseable_agent_run(patched, capsys):
    cli.main(["--invoice", INVOICE, "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["invoice_number"] == INVOICE
    assert parsed["resolution"]["decision"] == "ESCALATE"
    assert parsed["stop_reason"] == "SUBMITTED"


def test_flags_override_settings_without_mutating_the_cached_object(patched):
    from agent.settings import get_settings

    before = get_settings().model
    cli.main(["--invoice", INVOICE, "--model", "openai/gpt-4o", "--max-iterations", "2"])
    assert patched["settings"].model == "openai/gpt-4o"
    assert patched["settings"].max_iterations == 2
    # The cached settings object is untouched: model_copy returns a new one.
    assert get_settings().model == before


def test_scenario_id_is_passed_through_for_evals(patched):
    cli.main(["--invoice", INVOICE, "--scenario-id", "SC-0009"])
    assert patched["scenario_id"] == "SC-0009"


def test_the_client_is_closed_even_when_the_run_raises(monkeypatch, patched):
    def boom(*args, **kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(cli, "run_agent", boom)
    with pytest.raises(RuntimeError):
        cli.main(["--invoice", INVOICE])
    assert patched["closed"] is True


# ---------------------------------------------------------------------------
# step 7: cost and tracing at the CLI edge
# ---------------------------------------------------------------------------


def test_the_cost_table_shows_one_row_per_call_and_a_total(patched, capsys):
    cli.main(["--invoice", INVOICE])
    out = capsys.readouterr().out
    assert "cost" in out
    # per-call rows, then the total
    assert "$0.004650" in out  # call 1: 0.0024 + 0.00225
    assert "$0.009300" in out  # call 2: 0.0048 + 0.0045
    assert "$0.013950" in out  # total
    assert "1200" in out and "300" in out


def test_an_unpriced_run_says_so_rather_than_showing_zero(patched, capsys):
    patched["result"] = _run(
        llm_calls=[LlmCallRecord(iteration=1, model="test/scripted", prompt_tokens=10)],
        input_usd=None,
        output_usd=None,
        total_usd=None,
    )
    cli.main(["--invoice", INVOICE])
    out = capsys.readouterr().out
    assert "unpriced" in out
    assert "$0.000000" not in out


def test_the_trace_backend_and_url_are_printed_when_present(patched, capsys):
    patched["result"] = _run(
        trace_backend="langfuse", trace_url="https://cloud.langfuse.com/trace/abc"
    )
    out_lines = (cli.main(["--invoice", INVOICE]), capsys.readouterr().out)[1]
    assert "tracing   langfuse" in out_lines
    assert "https://cloud.langfuse.com/trace/abc" in out_lines


def test_no_trace_disables_tracing(patched):
    cli.main(["--invoice", INVOICE, "--no-trace"])
    assert patched["settings"].tracing is False


def test_tracing_is_on_by_default(patched):
    cli.main(["--invoice", INVOICE])
    assert patched["settings"].tracing is True


def test_trace_file_selects_the_jsonl_backend(patched, tmp_path):
    target = tmp_path / "traces" / "run.jsonl"
    cli.main(["--invoice", INVOICE, "--trace-file", str(target)])
    assert patched["settings"].trace_file == target
    assert isinstance(patched["tracer"], JsonlTracer)


def test_no_trace_wins_over_trace_file(patched, tmp_path):
    """An explicit off must not be overridden by a backend flag."""
    cli.main(["--invoice", INVOICE, "--no-trace", "--trace-file", str(tmp_path / "t.jsonl")])
    assert isinstance(patched["tracer"], NullTracer)


def test_the_tracer_is_flushed_even_when_the_run_raises(monkeypatch, patched):
    """Langfuse batches in a background thread; exiting without a flush
    silently loses the trace you just paid to produce."""
    flushed: list[bool] = []

    class SpyTracer(NullTracer):
        def flush(self):
            flushed.append(True)

    monkeypatch.setattr(cli, "build_tracer", lambda settings: SpyTracer())

    def boom(*args, **kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(cli, "run_agent", boom)
    with pytest.raises(RuntimeError):
        cli.main(["--invoice", INVOICE])
    assert flushed == [True]


def test_json_mode_includes_the_cost_table_and_trace_pointer(patched, capsys):
    cli.main(["--invoice", INVOICE, "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total_usd"] == "0.01395000"
    assert len(parsed["llm_calls"]) == 2
    assert parsed["llm_calls"][0]["input_usd"] == "0.00240000"
    assert parsed["trace_backend"] == "jsonl"
