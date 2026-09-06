"""Run a split and produce a report.

The seam the CLI calls and the tests call. Everything it needs arrives as an
argument -- the cases, the client, the settings, the completion function -- so
it can be exercised against a rule engine, a cassette, or a live model without
knowing which.

Modes, in the order you would reach for them:

    baseline   deterministic rules, no API key, no network, no cost
    replay     recorded transcripts, no API key, no cost, and identical every
               time -- this is what CI runs
    record     a live model, saving each transcript for later replay
    live       a live model, saving nothing
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent.erp_client import ErpClient
from agent.loop import AgentRun, StopReason, run_agent
from agent.settings import AgentSettings
from agent.tracing import NullTracer, Tracer

from evals.baseline import baseline_completion
from evals.cassettes import Cassette, CassetteError, Recorder, Replayer, cassette_path
from evals.dataset import EvalCase
from evals.report import EvalReport, now
from evals.safety import SafetyReport, applied_proposals, compare, find_label_leaks, snapshot
from evals.scoring import CaseResult, score

MODES = ("baseline", "replay", "record", "live")


@dataclass
class RunnerConfig:
    mode: str = "baseline"
    cassette_dir: Path | None = None
    #: Replay a cassette whose fingerprint no longer matches. Off by default:
    #: a stale cassette scores a prompt that was never actually run.
    allow_stale: bool = False
    progress: Callable[[str], None] | None = None


class RunnerError(RuntimeError):
    pass


@contextmanager
def _completion_for(
    case: EvalCase, settings: AgentSettings, config: RunnerConfig
) -> Iterator[tuple[object, Cassette | None]]:
    """Yield the completion_fn for one case, plus a cassette to save if recording."""
    if config.mode == "baseline":
        yield baseline_completion, None
        return

    if config.mode == "live":
        from litellm import completion

        yield completion, None
        return

    if config.cassette_dir is None:
        raise RunnerError(f"mode {config.mode!r} needs a cassette directory")

    path = cassette_path(config.cassette_dir, case.scenario_id)

    if config.mode == "replay":
        cassette = Cassette.load(path)
        if cassette.invoice_number != case.invoice_number:
            raise CassetteError(
                f"{path} was recorded for invoice {cassette.invoice_number}, "
                f"but {case.scenario_id} now resolves to {case.invoice_number}. "
                f"The dataset changed -- re-record."
            )
        yield Replayer(cassette, strict=not config.allow_stale), None
        return

    # record
    from litellm import completion

    recorder = Recorder(completion, case.scenario_id, case.invoice_number, settings.model)
    yield recorder, recorder.cassette
    recorder.cassette.save(path)


def run_case(
    case: EvalCase,
    client: ErpClient,
    settings: AgentSettings,
    config: RunnerConfig,
    *,
    tracer: Tracer | None = None,
) -> tuple[CaseResult, AgentRun]:
    """One scenario, scored. A harness failure is recorded, never raised.

    A cassette that no longer matches, or a model that times out, has to show
    up as a failed CASE rather than as an aborted run -- otherwise one bad
    scenario destroys the report for the other 199.
    """
    started = time.perf_counter()
    try:
        with _completion_for(case, settings, config) as (completion_fn, _):
            run = run_agent(
                case.invoice_number,
                client,
                settings,
                scenario_id=case.scenario_id,
                completion_fn=completion_fn,
                tracer=tracer or NullTracer(),
            )
    except Exception as exc:  # noqa: BLE001 - one bad case must not end the run
        duration_ms = (time.perf_counter() - started) * 1000.0
        empty = AgentRun(
            invoice_number=case.invoice_number,
            model=settings.model,
            stop_reason=StopReason.NO_TOOL_CALL,
            iterations=0,
        )
        result = score(case, empty, duration_ms=duration_ms)
        return (
            CaseResult(**{**result.__dict__, "error": f"{type(exc).__name__}: {exc}"}),
            empty,
        )

    duration_ms = (time.perf_counter() - started) * 1000.0
    return score(case, run, duration_ms=duration_ms), run


def run_split(
    cases: list[EvalCase],
    client: ErpClient,
    settings: AgentSettings,
    config: RunnerConfig | None = None,
    *,
    tracer: Tracer | None = None,
) -> EvalReport:
    """Run every case, check the safety gates, and assemble the report."""
    config = config or RunnerConfig()
    if config.mode not in MODES:
        raise RunnerError(f"unknown mode {config.mode!r}; expected one of {', '.join(MODES)}")

    # Every invoice in every scenario, not just the targeted ones: a write to
    # the invoice the agent was NOT asked about still breaks the guarantee.
    watched = sorted({n for case in cases for n in case.all_invoice_numbers})
    before = snapshot(client, watched)

    report = EvalReport(split="", model=settings.model, mode=config.mode, started_at=now())
    runs: list[AgentRun] = []

    for index, case in enumerate(cases, 1):
        if config.progress:
            config.progress(f"[{index:>3}/{len(cases)}] {case.scenario_id} {case.label}")
        result, run = run_case(case, client, settings, config, tracer=tracer)
        report.cases.append(result)
        runs.append(run)

    after = snapshot(client, watched)
    applied, snapshot_error = applied_proposals(client)
    report.safety = SafetyReport(
        changed_invoices=compare(before, after),
        applied_proposals=applied,
        label_leaks=find_label_leaks(runs),
        snapshot_errors=[snapshot_error] if snapshot_error else [],
    )
    report.finished_at = now()

    if config.mode == "baseline":
        report.notes.append(
            "Baseline mode: a deterministic rule engine, not a model. It scores well "
            "because the dataset was generated by rules and this encodes the same ones. "
            "Read it as the cost and accuracy FLOOR the model has to beat, not as "
            "evidence that the task is easy."
        )
    return report
