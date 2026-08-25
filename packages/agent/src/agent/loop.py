"""The tool-calling loop.

The whole agent is this: a list of messages, and a for-loop that grows it.
There is no other state. Everything the model "knows" at any point is what is
in `messages`, which is why the loop's only real job is to keep that list
well-formed.

One protocol invariant governs the whole file:

    every assistant tool_call MUST be answered by exactly one `tool` message
    carrying the same tool_call_id.

Break it in one direction (drop the assistant message) and the model re-asks
forever, burning tokens. Break it in the other (append the assistant message
with no results) and the provider returns 400. So tool failures become message
*content*, never exceptions -- `_execute_tool_call` cannot raise.
"""

from __future__ import annotations

import json
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.erp_client import ErpClient, ErpError
from agent.prompts import SYSTEM_PROMPT, user_prompt
from agent.schemas import Resolution
from agent.settings import AgentSettings
from agent.tools import TERMINAL_TOOL, ToolSpec, build_tools, to_openai_schemas

#: How much of a tool result to keep in the trace. The full result is already
#: in `messages`; this is for the human-readable transcript.
SUMMARY_CHARS = 240

#: What we say when the model replies with prose instead of calling a tool.
#: One nudge, then we give up -- see decisions.md.
NUDGE = (
    "You did not call a tool. Either call a tool to gather more evidence, or "
    "call submit_resolution with your final answer. Do not reply with prose."
)


class StopReason(StrEnum):
    SUBMITTED = "SUBMITTED"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    NO_TOOL_CALL = "NO_TOOL_CALL"


class ToolCallRecord(BaseModel):
    """One tool call, flattened for the trace, the eval, and Step 7's tracing."""

    name: str
    arguments: dict = Field(default_factory=dict)
    result_summary: str = ""
    error: str | None = None
    duration_ms: float = 0.0


class AgentRun(BaseModel):
    """Everything one run produced. The unit the eval scores and the UI renders."""

    invoice_number: str
    model: str
    stop_reason: StopReason
    iterations: int
    resolution: Resolution | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    #: The full transcript, replayable. Dicts, not provider objects, so this
    #: object round-trips through JSON.
    messages: list[dict] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _message_to_dict(msg: Any) -> dict:
    """Normalise a provider message object into a plain dict.

    LiteLLM hands back a pydantic model. Appending it raw would work on the
    next request but breaks AgentRun validation and json serialisation, so it
    is converted once, here.
    """
    if hasattr(msg, "model_dump"):
        raw = msg.model_dump()
    elif isinstance(msg, dict):
        raw = dict(msg)
    else:
        raw = dict(msg)  # mappings
    # Providers reject explicit nulls for absent fields (a plain reply carries
    # tool_calls=None, which is not the same as omitting the key).
    return {k: v for k, v in raw.items() if v is not None}


def _usage(response: Any) -> tuple[int, int]:
    """Token counts, defensively. Some providers omit usage entirely."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _tool_message(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _execute_tool_call(
    tool_call: Any,
    specs: dict[str, ToolSpec],
) -> tuple[str, ToolCallRecord]:
    """Run one tool call and return (content_for_the_model, trace_record).

    This function NEVER raises. Every failure -- unknown tool, malformed JSON,
    schema violation, ERP 404, unexpected exception -- comes back as text the
    model can read and act on. That is what lets the loop stay well-formed
    without a try/except around the whole thing.
    """
    name = getattr(tool_call.function, "name", "") or ""
    started = time.perf_counter()

    def finish(content: str, error: str | None, arguments: dict) -> tuple[str, ToolCallRecord]:
        return content, ToolCallRecord(
            name=name,
            arguments=arguments,
            result_summary=content[:SUMMARY_CHARS],
            error=error,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    if name not in specs:
        return finish(
            f"Error: no tool named '{name}'. Available tools: {sorted(specs)}.",
            "UNKNOWN_TOOL",
            {},
        )

    # `arguments` arrives as a JSON *string*, not a dict. Models do sometimes
    # emit invalid JSON, so this is a real branch, not a formality.
    raw_args = tool_call.function.arguments
    try:
        parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return finish(f"Error: arguments were not valid JSON ({exc}).", "BAD_JSON", {})
    if not isinstance(parsed, dict):
        return finish("Error: arguments must be a JSON object.", "BAD_JSON", {})

    spec = specs[name]
    try:
        validated = spec.args_model(**parsed)
    except ValidationError as exc:
        return finish(
            f"Error: arguments did not match the schema for '{name}'.\n{exc}",
            "VALIDATION_ERROR",
            parsed,
        )

    try:
        result = spec.fn(validated)
    except ErpError as exc:
        # The expected failure. The model can react: a PO_NOT_FOUND is
        # evidence, and escalating on it is the right answer.
        return finish(f"ERP error {exc.code} (HTTP {exc.status}): {exc.message}", exc.code, parsed)
    except Exception as exc:  # noqa: BLE001 - a bug must not kill the transcript
        return finish(
            f"Internal error running '{name}': {type(exc).__name__}: {exc}",
            "INTERNAL_ERROR",
            parsed,
        )

    try:
        content = json.dumps(result, default=str)
    except (TypeError, ValueError) as exc:
        return finish(
            f"Internal error serialising result of '{name}': {exc}", "UNSERIALISABLE", parsed
        )
    return finish(content, None, parsed)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def run_agent(
    invoice_number: str,
    client: ErpClient,
    settings: AgentSettings,
    *,
    scenario_id: str | None = None,
    completion_fn: Any = None,
) -> AgentRun:
    """Resolve one invoice. Returns a record of what happened, never raises.

    `completion_fn` is injectable so tests can drive the loop with a scripted
    model. Importing litellm lazily also keeps `import agent.loop` cheap.
    """
    if completion_fn is None:
        from litellm import completion as completion_fn  # noqa: PLC0415

    specs = build_tools(client, scenario_id=scenario_id)
    schemas = to_openai_schemas(specs)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(invoice_number)},
    ]
    records: list[ToolCallRecord] = []
    prompt_tokens = completion_tokens = 0
    nudged = False
    iterations = 0

    def run(stop_reason: StopReason, resolution: Resolution | None = None) -> AgentRun:
        return AgentRun(
            invoice_number=invoice_number,
            model=settings.model,
            stop_reason=stop_reason,
            iterations=iterations,
            resolution=resolution,
            tool_calls=records,
            messages=messages,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    for _ in range(settings.max_iterations):
        iterations += 1

        response = completion_fn(
            model=settings.model,
            messages=messages,
            tools=schemas,
            tool_choice="auto",
            temperature=settings.temperature,
        )
        message = response.choices[0].message
        # Appended BEFORE anything can go wrong. Every tool result that
        # follows refers back to this message by tool_call_id.
        messages.append(_message_to_dict(message))

        used_prompt, used_completion = _usage(response)
        prompt_tokens += used_prompt
        completion_tokens += used_completion

        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            if nudged:
                return run(StopReason.NO_TOOL_CALL)
            nudged = True
            messages.append({"role": "user", "content": NUDGE})
            continue

        submitted: Resolution | None = None
        for tool_call in tool_calls:
            if tool_call.function.name == TERMINAL_TOOL:
                started = time.perf_counter()
                try:
                    resolution = Resolution.model_validate_json(tool_call.function.arguments)
                except (ValidationError, ValueError) as exc:
                    # The schema teaches: hand the error back and let it retry.
                    content = (
                        f"Error: your resolution was rejected.\n{exc}\n"
                        f"Fix the fields and call {TERMINAL_TOOL} again."
                    )
                    records.append(
                        ToolCallRecord(
                            name=TERMINAL_TOOL,
                            result_summary=content[:SUMMARY_CHARS],
                            error="RESOLUTION_INVALID",
                            duration_ms=(time.perf_counter() - started) * 1000.0,
                        )
                    )
                    messages.append(_tool_message(tool_call.id, content))
                    continue

                records.append(
                    ToolCallRecord(
                        name=TERMINAL_TOOL,
                        arguments=resolution.model_dump(mode="json", exclude_none=True),
                        result_summary="accepted",
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                    )
                )
                # Answer the call even though we are about to return: the
                # transcript has to stay valid to be replayable.
                messages.append(_tool_message(tool_call.id, "accepted"))
                submitted = resolution
                continue

            content, record = _execute_tool_call(tool_call, specs)
            records.append(record)
            messages.append(_tool_message(tool_call.id, content))

        # Only after every tool_call in this assistant message has been
        # answered -- returning early would leave the transcript malformed.
        if submitted is not None:
            return run(StopReason.SUBMITTED, submitted)

    return run(StopReason.MAX_ITERATIONS)
