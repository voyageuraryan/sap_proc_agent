"""Command-line entry point for the eval harness.

Same shape as the other two CLIs in this repo: argparse and printing here,
everything real behind `run_split`, which takes its dependencies as arguments.

Exit codes exist so CI can branch without parsing text:

    0  safety passed
    1  a safety gate FAILED -- a write escaped, or ground truth leaked
    2  the harness could not run (stale cassette, missing split, no ERP)

Accuracy deliberately does not fail the build. Thresholds picked before there
is a baseline test your guess, not the agent; safety gates test a claim that
is either true or false. See decisions.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.erp_client import ErpClient, ErpError
from agent.settings import AgentSettings, get_settings
from agent.tracing import build_tracer
from dotenv import load_dotenv

from evals.cassettes import CassetteError
from evals.dataset import SPLITS, DatasetError, label_counts, load_cases, repo_root
from evals.report import render_markdown, render_terminal
from evals.runner import MODES, RunnerConfig, RunnerError, run_split

EXIT_OK = 0
EXIT_SAFETY_FAILED = 1
EXIT_HARNESS_ERROR = 2

DEFAULT_CASSETTES = Path("evals/cassettes")
DEFAULT_REPORTS = Path("evals/reports")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proc-evals",
        description="Score the agent against ground truth and gate on safety.",
    )
    parser.add_argument("--split", default="golden", choices=SPLITS)
    parser.add_argument(
        "--mode",
        default="baseline",
        choices=MODES,
        help=(
            "baseline: deterministic rules, free. replay: recorded transcripts, free "
            "and identical every time. record: a live model, saving transcripts. "
            "live: a live model, saving nothing."
        ),
    )
    parser.add_argument("--cassettes", type=Path, default=None, help="Cassette directory")
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Replay cassettes whose fingerprint no longer matches. Scores a prompt "
        "that was never run -- for debugging the harness only.",
    )
    parser.add_argument("--limit", type=int, default=None, help="First N cases only")
    parser.add_argument(
        "--model", default=None, help="provider:model for record/live, e.g. openai:gpt-4o"
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--out", type=Path, default=None, help="Directory for the report files")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument("--quiet", action="store_true", help="No per-case progress")
    return parser


def _settings_from(args: argparse.Namespace) -> AgentSettings:
    settings = get_settings()
    overrides = {
        key: value
        for key, value in (("model", args.model), ("erp_base_url", args.base_url))
        if value is not None
    }
    if args.mode == "baseline":
        # A rule engine has no model. Naming it as one would put a misleading
        # string in the report and price the run against a table it never used.
        overrides["model"] = "baseline/rules"
    return settings.model_copy(update=overrides) if overrides else settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()

    try:
        cases = load_cases(args.split, limit=args.limit)
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return EXIT_HARNESS_ERROR

    settings = _settings_from(args)
    cassettes = args.cassettes or (repo_root() / DEFAULT_CASSETTES / args.split)

    if not args.quiet:
        print(f"{args.split}: {len(cases)} cases  {label_counts(cases)}")
        print(f"mode: {args.mode}  model: {settings.model}")
        if args.mode in ("replay", "record"):
            print(f"cassettes: {cassettes}")
        print()

    config = RunnerConfig(
        mode=args.mode,
        cassette_dir=cassettes,
        allow_stale=args.allow_stale,
        progress=None if args.quiet else lambda line: print(line, flush=True),
    )

    # A tracer only when a live model is involved; replaying a cassette into a
    # dashboard would fill it with runs that did not happen today.
    tracer = build_tracer(settings) if args.mode in ("record", "live") else None

    client = ErpClient(settings.erp_base_url, settings.request_timeout)
    try:
        report = run_split(cases, client, settings, config, tracer=tracer)
    except (RunnerError, CassetteError) as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return EXIT_HARNESS_ERROR
    except ErpError as exc:
        print(f"ERP unavailable: {exc}", file=sys.stderr)
        print("start it with: uv run uvicorn mock_erp.app:app --port 8000", file=sys.stderr)
        return EXIT_HARNESS_ERROR
    finally:
        client.close()
        if tracer is not None:
            tracer.flush()

    report.split = args.split

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print()
        print(render_terminal(report))

    out = args.out or (repo_root() / DEFAULT_REPORTS)
    _write_reports(out, args.split, args.mode, report)
    if not args.quiet and not args.json:
        print()
        print(f"report written to {out}")

    return EXIT_OK if report.safety.passed else EXIT_SAFETY_FAILED


def _write_reports(out: Path, split: str, mode: str, report) -> None:
    """Both formats, always. The Markdown gets read; the JSON gets diffed."""
    try:
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{split}-{mode}"
        (out / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
        (out / f"{stem}.json").write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        print(f"could not write the report: {exc}", file=sys.stderr)


def _entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _entrypoint()
