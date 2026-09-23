"""Record once, replay forever -- and refuse to replay a stale recording.

The fingerprint is the whole point of this module, so most of these tests are
about the ways a cassette can silently stop being valid.
"""

import json

import pytest
from agent.loop import run_agent
from evals.baseline import BaselineChatModel
from evals.cassettes import (
    CASSETTE_VERSION,
    Cassette,
    CassetteError,
    Recorder,
    Replayer,
    cassette_path,
    fingerprint,
)
from evals.dataset import load_cases

TOOLS = [
    {"function": {"name": "get_invoice", "description": "Fetch an invoice."}},
    {"function": {"name": "submit_resolution", "description": "Finish."}},
]
MESSAGES = [
    {"role": "system", "content": "You are an AP assistant."},
    {"role": "user", "content": "Verify 5100000901."},
]


def _fp(**overrides):
    kwargs = {"model": "m", "messages": MESSAGES, "tools": TOOLS, "temperature": 0.0}
    kwargs.update(overrides)
    return fingerprint(**kwargs)


# ---------------------------------------------------------------------------
# the fingerprint
# ---------------------------------------------------------------------------


def test_the_same_request_fingerprints_the_same():
    assert _fp() == _fp()


def test_a_changed_system_prompt_changes_the_fingerprint():
    """The case this exists for: tuning the prompt must invalidate recordings."""
    changed = [dict(MESSAGES[0], content="You are a different assistant."), MESSAGES[1]]
    assert _fp(messages=changed) != _fp()


def test_a_changed_tool_description_changes_the_fingerprint():
    """Tool descriptions are prompt, so editing one is a prompt change."""
    changed = [{"function": {"name": "get_invoice", "description": "NEW"}}, TOOLS[1]]
    assert _fp(tools=changed) != _fp()


def test_a_changed_model_or_temperature_changes_the_fingerprint():
    assert _fp(model="other") != _fp()
    assert _fp(temperature=1.0) != _fp()


def test_reordering_the_tool_registry_does_not():
    """Registry order is an implementation detail the model never sees."""
    assert _fp(tools=list(reversed(TOOLS))) == _fp()


def test_provider_generated_tool_call_ids_do_not():
    """They differ between the recording run and the replay run through no
    fault of ours; including them would make every cassette single-use."""

    def transcript(call_id):
        return [
            *MESSAGES,
            {
                "role": "assistant",
                "tool_calls": [{"id": call_id, "function": {"name": "get_invoice"}}],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "{}"},
        ]

    assert _fp(messages=transcript("call_aaa")) == _fp(messages=transcript("call_bbb"))


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------


def test_a_cassette_round_trips_through_disk(tmp_path):
    original = Cassette(
        scenario_id="SC-0009",
        invoice_number="5100000901",
        model="test/scripted",
        turns=[{"fingerprint": "abc", "response": {"choices": []}}],
    )
    path = cassette_path(tmp_path, "SC-0009")
    original.save(path)
    loaded = Cassette.load(path)
    assert loaded == original


def test_saving_is_stable_so_an_unchanged_recording_is_an_empty_diff(tmp_path):
    cassette = Cassette("SC-0001", "5100000101", "m", [{"b": 2, "a": 1}])
    first = cassette_path(tmp_path, "a")
    second = cassette_path(tmp_path, "b")
    cassette.save(first)
    cassette.save(second)
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")


def test_a_missing_cassette_says_to_record_one(tmp_path):
    with pytest.raises(CassetteError, match="record one first"):
        Cassette.load(tmp_path / "nope.json")


def test_a_cassette_from_a_future_format_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"version": CASSETTE_VERSION + 1, "turns": []}))
    with pytest.raises(CassetteError, match="re-record"):
        Cassette.load(path)


def test_corrupt_json_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{not json")
    with pytest.raises(CassetteError, match="not valid JSON"):
        Cassette.load(path)


# ---------------------------------------------------------------------------
# record and replay against the real graph
# ---------------------------------------------------------------------------


def _record(case, erp_client, settings):
    """Run the baseline with a Recorder attached; return (run, recorder)."""
    recorder = Recorder(case.scenario_id, case.invoice_number, settings.model, settings.temperature)
    run = run_agent(
        case.invoice_number,
        erp_client,
        settings,
        chat_model=BaselineChatModel(),
        callbacks=[recorder],
    )
    return run, recorder


def _replayer(cassette, settings, **kwargs):
    return Replayer(cassette, model=settings.model, temperature=settings.temperature, **kwargs)


def test_a_recorded_run_replays_identically(erp_client, settings, tmp_path, root):
    """The property that makes CI free: same transcript, same score, no API.

    A read-only scenario, deliberately. A cassette records the MODEL's side of
    the conversation, not the ERP's -- tools re-execute for real on replay --
    so a run that writes only replays against a backend in the same state.
    That limitation is demonstrated in its own test below rather than dodged.
    """
    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")

    live, recorder = _record(case, erp_client, settings)
    recorder.cassette.save(cassette_path(tmp_path, case.scenario_id))

    replayed = run_agent(
        case.invoice_number,
        erp_client,
        settings,
        chat_model=_replayer(Cassette.load(cassette_path(tmp_path, case.scenario_id)), settings),
    )

    assert replayed.stop_reason == live.stop_reason
    assert replayed.resolution == live.resolution
    assert [c.name for c in replayed.tool_calls] == [c.name for c in live.tool_calls]
    assert len(recorder.cassette.turns) == live.iterations


def test_recording_observes_the_model_and_does_not_replace_it(erp_client, settings, root):
    """The Recorder is a callback. The run it watched must be the run that
    would have happened without it."""
    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")
    watched, _ = _record(case, erp_client, settings)
    plain = run_agent(case.invoice_number, erp_client, settings, chat_model=BaselineChatModel())
    assert watched.resolution == plain.resolution
    assert watched.iterations == plain.iterations


def test_replaying_a_run_with_side_effects_diverges_and_says_so(
    erp_client, settings, tmp_path, root
):
    """A cassette holds the model's turns, not the ERP's answers.

    Recording a QTY_OVER run raises a proposal. Replaying it against the same
    ERP raises a SECOND one, so the tool result differs, so the transcript
    differs, so the fingerprint no longer matches -- and the replay stops.

    That is the fingerprint doing its job on a genuinely diverged run, not a
    false alarm. The consequence for CI is real and worth stating: replay
    needs a backend in the state the recording was made against, which is why
    the workflow starts a fresh ERP for every job.
    """
    case = next(c for c in load_cases("golden", root=root) if c.label == "QTY_OVER")
    live, recorder = _record(case, erp_client, settings)
    assert any(c.name == "propose_correction" for c in live.tool_calls)

    with pytest.raises(CassetteError, match="no longer matches"):
        run_agent(
            case.invoice_number,
            erp_client,
            settings,
            chat_model=_replayer(recorder.cassette, settings),
        )


def test_replay_refuses_a_cassette_recorded_against_a_different_prompt(
    erp_client, settings, tmp_path, root
):
    """A cassette that replays against a changed prompt reports a green eval
    for a prompt that was never run -- a false negative on exactly the change
    you wanted to measure."""
    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")
    _, recorder = _record(case, erp_client, settings)

    cassette = recorder.cassette
    cassette.turns[0]["fingerprint"] = "0" * 64  # as if the prompt had changed

    with pytest.raises(CassetteError, match="no longer matches"):
        run_agent(
            case.invoice_number, erp_client, settings, chat_model=_replayer(cassette, settings)
        )


def test_replay_refuses_a_cassette_recorded_for_a_different_model(erp_client, settings, root):
    """Switching model is the change a cassette must never paper over."""
    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")
    _, recorder = _record(case, erp_client, settings)

    with pytest.raises(CassetteError, match="no longer matches"):
        run_agent(
            case.invoice_number,
            erp_client,
            settings,
            chat_model=Replayer(recorder.cassette, model="openai:gpt-4o"),
        )


def test_allow_stale_replays_anyway_and_records_the_mismatch(erp_client, settings, root):
    """An escape hatch for debugging the harness, never for scoring."""
    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")
    _, recorder = _record(case, erp_client, settings)
    recorder.cassette.turns[0]["fingerprint"] = "0" * 64

    replayer = _replayer(recorder.cassette, settings, strict=False)
    run = run_agent(case.invoice_number, erp_client, settings, chat_model=replayer)
    assert run.stop_reason.value == "SUBMITTED"
    assert replayer.mismatches == [0]


def test_a_graph_that_now_makes_more_calls_exhausts_the_cassette(erp_client, settings, root):
    """Recorded four turns, graph now wants five: that is a code change, and
    replaying the first four would score a run that never finished."""
    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")
    _, recorder = _record(case, erp_client, settings)

    truncated = Cassette(
        scenario_id=case.scenario_id,
        invoice_number=case.invoice_number,
        model=settings.model,
        turns=recorder.cassette.turns[:-1],
    )
    with pytest.raises(CassetteError, match="re-record"):
        run_agent(
            case.invoice_number, erp_client, settings, chat_model=_replayer(truncated, settings)
        )


def test_replay_rebuilds_the_models_own_message_type(erp_client, settings, root):
    """Not a local shim: the graph must see in replay exactly what it sees in
    production -- an AIMessage with parsed tool_calls and usage -- or the
    cassette tests a different code path."""
    from langchain_core.messages import AIMessage

    case = next(c for c in load_cases("golden", root=root) if c.label == "CLEAN")
    _, recorder = _record(case, erp_client, settings)

    # strict=False because this calls the replayer directly with a stub
    # request; the fingerprint check is exercised in its own tests above.
    reply = _replayer(recorder.cassette, settings, strict=False).invoke([])
    assert isinstance(reply, AIMessage)
    assert reply.tool_calls[0]["name"] == "get_invoice"
    assert reply.usage_metadata["input_tokens"] == 0


def test_the_recorder_and_replayer_agree_on_a_provider_rendered_tool():
    """The recorder fingerprints tools as the PROVIDER was sent them; the
    replayer as LangChain renders them. Both must hash the same."""
    anthropic_style = [{"name": "get_invoice", "description": "Fetch an invoice."}]
    openai_style = [{"type": "function", "function": anthropic_style[0]}]
    assert _fp(tools=anthropic_style) == _fp(tools=openai_style)
