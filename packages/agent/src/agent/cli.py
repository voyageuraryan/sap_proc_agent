"""Command-line entry point for the agent.

Same four-layer shape as generator/cli.py, and for the same reason:

    main()        argparse, .env loading, printing   <- messy, tiny, untested
    run_agent()   the seam: everything as arguments  <- what tests call
    ErpClient     I/O against the ERP
    Resolution    the typed result

main() contains no procurement logic. If an `if` in here is about invoices
rather than about arguments, it is in the wrong file.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from agent.erp_client import ErpClient, ErpError
from agent.loop import AgentRun, StopReason, run_agent
from agent.settings import AgentSettings, get_settings

#: Process exit codes, so CI and the eval harness can branch without parsing.
EXIT_OK = 0
EXIT_NOT_SUBMITTED = 1
EXIT_ERP_DOWN = 2

#: How much of a tool call's arguments to show in the transcript.
ARG_CHARS = 110


def _print_trace(run: AgentRun) -> None:
    """The demo screen: what the agent looked at, and what it concluded."""
    print(f"invoice   {run.invoice_number}")
    print(f"model     {run.model}")
    print(f"stopped   {run.stop_reason.value} after {run.iterations} iteration(s)")
    print()

    if run.tool_calls:
        print("tool calls")
        for i, call in enumerate(run.tool_calls, 1):
            mark = "x" if call.error else "-"
            args = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
            if len(args) > ARG_CHARS:
                args = args[: ARG_CHARS - 3] + "..."
            print(f"  {mark} {i}. {call.name}({args})  [{call.duration_ms:.0f} ms]")
            summary = call.result_summary.replace("\n", " ")
            print(f"       {summary[:160]}")
        print()

    if run.resolution is None:
        print("no resolution was submitted")
    else:
        r = run.resolution
        print(f"classification  {r.classification.value}")
        print(f"decision        {r.decision.value}")
        print(f"reasoning       {r.reasoning}")
        print("evidence")
        for item in r.evidence:
            print(f"  - {item}")
        if r.correction is not None:
            print("correction")
            for key, value in r.correction.model_dump(mode="json").items():
                print(f"  {key:24} {value}")
        if r.escalate_to:
            print(f"escalate to     {r.escalate_to}")
            print(f"because         {r.escalation_reason}")
    print()
    print(f"tokens    {run.prompt_tokens} in / {run.completion_tokens} out")


def _settings_from(args: argparse.Namespace) -> AgentSettings:
    """Config read once, then overridden immutably by the flags that were given.

    model_copy rather than assignment: settings are a value, so an override
    produces a new one and nothing that already captured the old is surprised.
    """
    settings = get_settings()
    overrides = {
        key: value
        for key, value in (
            ("model", args.model),
            ("erp_base_url", args.base_url),
            ("max_iterations", args.max_iterations),
            ("temperature", args.temperature),
        )
        if value is not None
    }
    return settings.model_copy(update=overrides) if overrides else settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="proc-agent",
        description="Verify one supplier invoice against its PO and goods receipts.",
    )
    parser.add_argument("--invoice", required=True, help="Invoice number, e.g. 5100000901")
    parser.add_argument("--scenario-id", default=None, help="Eval bookkeeping, e.g. SC-0009")
    parser.add_argument("--model", default=None, help="Override the configured model")
    parser.add_argument("--base-url", default=None, help="Override the mock ERP base URL")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full AgentRun as JSON instead of a transcript (for CI and evals)",
    )
    args = parser.parse_args(argv)

    # Loads ANTHROPIC_API_KEY into the environment for LiteLLM to find. Done
    # here, at the edge, not at import time -- an imported module that mutates
    # os.environ is a surprise.
    load_dotenv()

    settings = _settings_from(args)

    client = ErpClient(settings.erp_base_url, settings.request_timeout)
    try:
        run = run_agent(
            args.invoice,
            client,
            settings,
            scenario_id=args.scenario_id,
        )
    finally:
        # The client owns a socket pool. Release it even if run_agent raised.
        client.close()

    if args.json:
        print(run.model_dump_json(indent=2))
    else:
        _print_trace(run)

    return EXIT_OK if run.stop_reason is StopReason.SUBMITTED else EXIT_NOT_SUBMITTED


def _entrypoint() -> None:
    """Console-script shim: turn a return code into a process exit code."""
    try:
        raise SystemExit(main())
    except ErpError as exc:
        # Only reachable if the ERP is unreachable before the loop starts.
        print(f"ERP unavailable: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_ERP_DOWN) from exc


if __name__ == "__main__":
    _entrypoint()
