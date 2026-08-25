"""The CLI: argument handling, output modes, exit codes.

main() is deliberately thin, so there is little to test -- which is the point.
What IS tested is the contract with the outside world: the flags, the exit
code, and that --json emits something a machine can consume.
"""

import json

import pytest
from agent import cli
from agent.loop import AgentRun, StopReason
from agent.schemas import Classification, Decision, Resolution
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

    def fake_run_agent(invoice_number, client, settings, *, scenario_id=None, **kw):
        captured["invoice_number"] = invoice_number
        captured["settings"] = settings
        captured["scenario_id"] = scenario_id
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
