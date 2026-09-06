"""The tool-calling loop, instrumented.

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

Step 7 added two things and changed nothing else:

  * cost, per LLM call and in total, on `AgentRun`
  * a span tree: one `agent.run` span, one `llm.completion` span per iteration,
    one `tool.<name>` span per tool call

Both are structured so `run_agent` behaves identically with tracing off --
`test_tracing_does_not_change_the_run` asserts exactly that.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.cost import TokenCost, cost_for, total_cost
from agent.erp_client import ErpClient, ErpError
from agent.prompts import SYSTEM_PROMPT, user_prompt
from agent.schemas import Resolution
from agent.settings import AgentSettings
from agent.tools import TERMINAL_TOOL, ToolSpec, build_tools, to_openai_schemas
from agent.tracing import Tracer, build_tracer, settings_metadata

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
    """One tool call, flattened for the trace, the eval, and the review UI."""

    name: str
    arguments: dict = Field(default_factory=dict)
    result_summary: str = ""
    error: str | None = None
    duration_ms: float = 0.0


class LlmCallRecord(BaseModel):
    """One model call. The unit cost is charged in, so this is the cost table.

    Input and output are separated because output tokens cost several times
    what input tokens do: a run that looks expensive is usually one where the
    model wrote too much, not one where it read too much.
    """

    iteration: int
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0.0
    input_usd: Decimal | None = None
    output_usd: Decimal | None = None
    tool_calls_requested: int = 0

    @property
    def total_usd(self) -> Decimal | None:
        if self.input_usd is None or self.output_usd is None:
            return None
        return self.input_usd + self.output_usd


class AgentRun(BaseModel):
    """Everything one run produced. The unit the eval scores and the UI renders."""

    invoice_number: str
    model: str
    stop_reason: StopReason
    iterations: int
    resolution: Resolution | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    llm_calls: list[LlmCallRecord] = Field(default_factory=list)
    #: The full transcript, replayable. Dicts, not provider objects, so this
    #: object round-trips through JSON.
    messages: list[dict] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: None means "unpriced", not "free". A silent zero would read as free.
    input_usd: Decimal | None = None
    output_usd: Decimal | None = None
    total_usd: Decimal | None = None
    #: Where to go and look at this run. None when tracing is off or local.
    trace_backend: str = "none"
    trace_id: str | None = None
    trace_url: str | None = None


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
    tracer: Tracer | None = None,
) -> AgentRun:
    """Resolve one invoice. Returns a record of what happened, never raises.

    `completion_fn` is injectable so tests can drive the loop with a scripted
    model. `tracer` is injectable so tests can assert on the span tree without
    a backend. Importing litellm lazily also keeps `import agent.loop` cheap.

    Deliberately does NOT flush the tracer: the caller decides when to pay for
    a network round trip. The CLI flushes once, in a finally block.
    """
    if completion_fn is None:
        from litellm import completion as completion_fn
    if tracer is None:
        tracer = build_tracer(settings)

    with tracer.span(
        "agent.run",
        kind="run",
        input={"invoice_number": invoice_number, "scenario_id": scenario_id},
        metadata={"scenario_id": scenario_id, **settings_metadata(settings)},
    ) as run_span:
        run = _run_loop(
            invoice_number,
            client,
            settings,
            scenario_id=scenario_id,
            completion_fn=completion_fn,
            tracer=tracer,
        )
        run.trace_backend = tracer.backend
        run.trace_id = run_span.trace_id or tracer.trace_id
        run_span.update(
            output={
                "stop_reason": run.stop_reason.value,
                "classification": run.resolution.classification.value if run.resolution else None,
                "decision": run.resolution.decision.value if run.resolution else None,
            },
            metadata={
                "iterations": run.iterations,
                "tool_calls": len(run.tool_calls),
                "llm_calls": len(run.llm_calls),
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "total_usd": str(run.total_usd) if run.total_usd is not None else None,
            },
            # A run that never reached a resolution is an ERROR-level trace, so
            # it is findable in the UI without knowing what to search for.
            error=None if run.stop_reason is StopReason.SUBMITTED else run.stop_reason.value,
        )

    run.trace_url = tracer.trace_url
    return run


def _run_loop(
    invoice_number: str,
    client: ErpClient,
    settings: AgentSettings,
    *,
    scenario_id: str | None,
    completion_fn: Any,
    tracer: Tracer,
) -> AgentRun:
    """The loop itself. Split out so the run span can wrap it cleanly."""
    specs = build_tools(client, scenario_id=scenario_id)
    schemas = to_openai_schemas(specs)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(invoice_number)},
    ]
    records: list[ToolCallRecord] = []
    llm_records: list[LlmCallRecord] = []
    costs: list[TokenCost] = []
    prompt_tokens = completion_tokens = 0
    nudged = False
    iterations = 0

    def payload(value: Any) -> Any:
        """Redaction point. One function, so the switch cannot be half-applied."""
        return value if settings.trace_payloads else "<redacted>"

    def build(stop_reason: StopReason, resolution: Resolution | None = None) -> AgentRun:
        run_cost = total_cost(costs)
        return AgentRun(
            invoice_number=invoice_number,
            model=settings.model,
            stop_reason=stop_reason,
            iterations=iterations,
            resolution=resolution,
            tool_calls=records,
            llm_calls=llm_records,
            messages=messages,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            input_usd=run_cost.input_usd,
            output_usd=run_cost.output_usd,
            total_usd=run_cost.total_usd,
        )

    for _ in range(settings.max_iterations):
        iterations += 1

        with tracer.span(
            "llm.completion",
            kind="llm",
            model=settings.model,
            input=payload(list(messages)),
            metadata={"iteration": iterations, "tools_offered": len(schemas)},
        ) as llm_span:
            started = time.perf_counter()
            response = completion_fn(
                model=settings.model,
                messages=messages,
                tools=schemas,
                tool_choice="auto",
                temperature=settings.temperature,
            )
            duration_ms = (time.perf_counter() - started) * 1000.0
            message = response.choices[0].message
            # Appended BEFORE anything can go wrong. Every tool result that
            # follows refers back to this message by tool_call_id.
            messages.append(_message_to_dict(message))

            used_prompt, used_completion = _usage(response)
            prompt_tokens += used_prompt
            completion_tokens += used_completion
            cost = cost_for(settings.model, used_prompt, used_completion)
            costs.append(cost)

            tool_calls = getattr(message, "tool_calls", None) or []
            llm_records.append(
                LlmCallRecord(
                    iteration=iterations,
                    model=settings.model,
                    prompt_tokens=used_prompt,
                    completion_tokens=used_completion,
                    duration_ms=duration_ms,
                    input_usd=cost.input_usd,
                    output_usd=cost.output_usd,
                    tool_calls_requested=len(tool_calls),
                )
            )
            llm_span.update(
                output=payload(messages[-1]),
                usage={"input": used_prompt, "output": used_completion},
                cost=cost,
                metadata={
                    "iteration": iterations,
                    "tool_calls_requested": len(tool_calls),
                    "finish": "tool_calls" if tool_calls else "content",
                    # The span also covers usage accounting and the price
                    # lookup, so span time minus provider_ms is OUR overhead.
                    # Recording both is what makes that subtraction possible.
                    "provider_ms": round(duration_ms, 2),
                },
            )

        if not tool_calls:
            if nudged:
                return build(StopReason.NO_TOOL_CALL)
            nudged = True
            messages.append({"role": "user", "content": NUDGE})
            continue

        submitted: Resolution | None = None
        for tool_call in tool_calls:
            if tool_call.function.name == TERMINAL_TOOL:
                resolution, record, content = _accept_resolution(tool_call, tracer, payload)
                records.append(record)
                messages.append(_tool_message(tool_call.id, content))
                if resolution is not None:
                    submitted = resolution
                continue

            with tracer.span(
                f"tool.{tool_call.function.name}",
                kind="tool",
                input=payload(tool_call.function.arguments),
            ) as tool_span:
                content, record = _execute_tool_call(tool_call, specs)
                tool_span.update(
                    output=payload(content[:SUMMARY_CHARS]),
                    error=record.error,
                    metadata={"duration_ms": round(record.duration_ms, 2)},
                )
            records.append(record)
            messages.append(_tool_message(tool_call.id, content))

        # Only after every tool_call in this assistant message has been
        # answered -- returning early would leave the transcript malformed.
        if submitted is not None:
            return build(StopReason.SUBMITTED, submitted)

    return build(StopReason.MAX_ITERATIONS)


def _accept_resolution(
    tool_call: Any,
    tracer: Tracer,
    payload: Any,
) -> tuple[Resolution | None, ToolCallRecord, str]:
    """Validate the terminal tool's arguments into a Resolution.

    A rejection is not a failure of the run: the error text goes back as the
    tool result and the model gets another turn to fix it. The schema teaches.
    """
    started = time.perf_counter()
    with tracer.span(
        f"tool.{TERMINAL_TOOL}", kind="tool", input=payload(tool_call.function.arguments)
    ) as span:
        try:
            resolution = Resolution.model_validate_json(tool_call.function.arguments)
        except (ValidationError, ValueError) as exc:
            content = (
                f"Error: your resolution was rejected.\n{exc}\n"
                f"Fix the fields and call {TERMINAL_TOOL} again."
            )
            span.update(output=content[:SUMMARY_CHARS], error="RESOLUTION_INVALID")
            return (
                None,
                ToolCallRecord(
                    name=TERMINAL_TOOL,
                    result_summary=content[:SUMMARY_CHARS],
                    error="RESOLUTION_INVALID",
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                ),
                content,
            )

        span.update(output="accepted")
        return (
            resolution,
            ToolCallRecord(
                name=TERMINAL_TOOL,
                arguments=resolution.model_dump(mode="json", exclude_none=True),
                result_summary="accepted",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            ),
            "accepted",
        )
