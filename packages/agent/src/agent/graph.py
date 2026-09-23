"""The agent as a LangGraph state graph.

The shape is the same tool-calling loop the agent always was -- ask the model,
run what it asked for, repeat -- expressed as three nodes and the edges
between them, so the control flow is data LangGraph can execute, trace and
draw, rather than a for-loop only one file understands:

    START --> agent --(tool calls)--> tools --> END   resolution accepted,
                ^                       |             or iteration cap hit
                |                       |
                +------(otherwise)------+
                |
                +---(no tool call)--> nudge --> END   the model already
                ^                       |             ignored one nudge
                +------(otherwise)------+

One protocol invariant governs the whole file:

    every tool call the model makes MUST be answered by exactly one
    ToolMessage carrying the same tool_call_id.

Break it in one direction (drop the model's turn) and the model re-asks
forever, burning tokens. Break it in the other (a tool call with no result)
and the provider returns 400. So a tool failure becomes message CONTENT, never
an exception -- `_execute` cannot raise, and malformed calls (LangChain's
`invalid_tool_calls`) are answered too.

Why a hand-built StateGraph rather than LangChain's prebuilt `create_agent`:
the prebuilt loop cannot express three things this agent depends on -- a
terminal tool whose acceptance ENDS the run, exactly one nudge before giving
up on prose, and a stop reason recorded as data rather than inferred
afterwards. Each of those is one node or one edge here.

Every decision to stop is made inside a node and written to `stop_reason`;
the edges only read it. That keeps routing trivially correct and puts every
"why did it stop" in the state, where the run record picks it up.
"""

from __future__ import annotations

import json
import operator
import time
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from agent.cost import cost_for
from agent.records import LlmCallRecord, StopReason, ToolCallRecord
from agent.schemas import Resolution
from agent.settings import AgentSettings
from agent.tools import TERMINAL_TOOL, tool_error_code

#: How much of a tool result to keep in the record. The full result is already
#: in the transcript; this is for the human-readable one.
SUMMARY_CHARS = 240

#: What we say when the model replies with prose instead of calling a tool.
#: One nudge, then we give up -- see decisions.md.
NUDGE = (
    "You did not call a tool. Either call a tool to gather more evidence, or "
    "call submit_resolution with your final answer. Do not reply with prose."
)

#: The root run's name, in every trace backend.
RUN_NAME = "agent.run"

#: LangChain's convention for "plumbing, not work". Langfuse files runs with
#: this tag at DEBUG level and the JSONL tracer skips them, so the routing
#: functions below do not appear in a trace as if they were steps.
HIDDEN_TAG = "langsmith:hidden"


def _router(fn):
    return RunnableLambda(fn, name=fn.__name__).with_config(tags=[HIDDEN_TAG])


class AgentState(TypedDict):
    """Everything the graph knows. The transcript is the only thing the MODEL sees."""

    messages: Annotated[list[AnyMessage], add_messages]
    iterations: int
    nudged: bool
    resolution: Resolution | None
    stop_reason: StopReason | None
    tool_records: Annotated[list[ToolCallRecord], operator.add]
    llm_records: Annotated[list[LlmCallRecord], operator.add]


def initial_state(messages: list[AnyMessage]) -> AgentState:
    return AgentState(
        messages=messages,
        iterations=0,
        nudged=False,
        resolution=None,
        stop_reason=None,
        tool_records=[],
        llm_records=[],
    )


def recursion_limit(settings: AgentSettings) -> int:
    """LangGraph's own ceiling, set just above the one this graph enforces.

    Each iteration is two supersteps (agent, then tools or nudge), so the
    iteration cap is always reached first; this limit only fires if a routing
    bug ever made the graph cycle without counting.
    """
    return 2 * settings.max_iterations + 4


# ---------------------------------------------------------------------------
# tool execution -- the function that cannot raise
# ---------------------------------------------------------------------------


def _execute(
    call: dict,
    tools: dict[str, BaseTool],
    config: RunnableConfig,
) -> tuple[ToolMessage, ToolCallRecord, Resolution | None]:
    """Run one tool call. Returns (answer for the model, record, resolution-if-any).

    NEVER raises. Unknown tool, schema violation, ERP 404, a bug in a tool --
    every one comes back as text the model can read and act on. Running the
    tool through `invoke(..., config)` is what makes it a child run of this
    graph, so every callback handler -- Langfuse, the JSONL file -- sees it
    under the right parent, with the right error.
    """
    name = call.get("name") or ""
    call_id = call.get("id") or ""
    args = call.get("args")
    started = time.perf_counter()

    def finish(
        content: str,
        error: str | None,
        arguments: dict,
        resolution: Resolution | None = None,
    ) -> tuple[ToolMessage, ToolCallRecord, Resolution | None]:
        message = ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=name,
            # Providers that understand it (Anthropic's is_error) are told the
            # call failed; the content says why.
            status="error" if error else "success",
        )
        record = ToolCallRecord(
            name=name,
            arguments=arguments,
            result_summary=content[:SUMMARY_CHARS],
            error=error,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return message, record, resolution

    if name not in tools:
        return finish(
            f"Error: no tool named '{name}'. Available tools: {sorted(tools)}.",
            "UNKNOWN_TOOL",
            {},
        )
    if not isinstance(args, dict):
        return finish("Error: arguments must be a JSON object.", "BAD_JSON", {})

    try:
        result = tools[name].invoke(args, config)
    except ValidationError as exc:
        code = tool_error_code(name, exc)
        if name == TERMINAL_TOOL:
            # A rejection is not a failure of the run: the error goes back as
            # the tool result and the model gets another turn. The schema teaches.
            return finish(
                f"Error: your resolution was rejected.\n{exc}\n"
                f"Fix the fields and call {TERMINAL_TOOL} again.",
                code,
                {},
            )
        return finish(f"Error: arguments did not match the schema for '{name}'.\n{exc}", code, args)
    except Exception as exc:  # noqa: BLE001 - a bug must not kill the transcript
        code = tool_error_code(name, exc)
        if code == "INTERNAL_ERROR":
            return finish(
                f"Internal error running '{name}': {type(exc).__name__}: {exc}", code, args
            )
        # An ErpError: the expected failure, and one the model can react to.
        return finish(f"ERP error {code} (HTTP {exc.status}): {exc.message}", code, args)

    if name == TERMINAL_TOOL:
        # Already validated by the tool; parsed once more to KEEP the object.
        # Deterministic, so it cannot disagree with the validation that passed.
        resolution = Resolution.model_validate(args)
        return finish(
            "accepted", None, resolution.model_dump(mode="json", exclude_none=True), resolution
        )

    try:
        content = json.dumps(result, default=str)
    except (TypeError, ValueError) as exc:
        return finish(
            f"Internal error serialising result of '{name}': {exc}", "UNSERIALISABLE", args
        )
    return finish(content, None, args)


def _answer_malformed(call: dict) -> tuple[ToolMessage, ToolCallRecord]:
    """A call whose arguments were not valid JSON. It still gets an answer."""
    content = f"Error: arguments were not valid JSON ({call.get('error') or 'unparseable'})."
    name = call.get("name") or ""
    return (
        ToolMessage(content=content, tool_call_id=call.get("id") or "", name=name, status="error"),
        ToolCallRecord(name=name, result_summary=content[:SUMMARY_CHARS], error="BAD_JSON"),
    )


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


def build_graph(
    chat_model: BaseChatModel,
    tools: dict[str, BaseTool],
    settings: AgentSettings,
):
    """Compile the agent graph for one chat model and one bound tool set."""
    model = chat_model.bind_tools(list(tools.values()))

    def agent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        iteration = state["iterations"] + 1
        started = time.perf_counter()
        reply = model.invoke(state["messages"], config)
        duration_ms = (time.perf_counter() - started) * 1000.0

        # Some providers omit usage entirely; that is zero tokens, not a crash.
        usage = getattr(reply, "usage_metadata", None) or {}
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)
        cost = cost_for(settings.model, prompt_tokens, completion_tokens)
        requested = len(getattr(reply, "tool_calls", None) or []) + len(
            getattr(reply, "invalid_tool_calls", None) or []
        )
        return {
            "messages": [reply],
            "iterations": iteration,
            "llm_records": [
                LlmCallRecord(
                    iteration=iteration,
                    model=settings.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=duration_ms,
                    input_usd=cost.input_usd,
                    output_usd=cost.output_usd,
                    tool_calls_requested=requested,
                )
            ],
        }

    def run_tools(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        reply = state["messages"][-1]
        answers: list[ToolMessage] = []
        records: list[ToolCallRecord] = []
        submitted: Resolution | None = None

        for call in reply.tool_calls:
            message, record, resolution = _execute(call, tools, config)
            answers.append(message)
            records.append(record)
            if resolution is not None:
                submitted = resolution
        for call in reply.invalid_tool_calls:
            message, record = _answer_malformed(call)
            answers.append(message)
            records.append(record)

        # Only after EVERY call in this turn has been answered -- stopping
        # early would leave the transcript malformed.
        if submitted is not None:
            stop: StopReason | None = StopReason.SUBMITTED
        elif state["iterations"] >= settings.max_iterations:
            stop = StopReason.MAX_ITERATIONS
        else:
            stop = None
        update: dict[str, Any] = {"messages": answers, "tool_records": records, "stop_reason": stop}
        if submitted is not None:
            update["resolution"] = submitted
        return update

    def nudge(state: AgentState) -> dict[str, Any]:
        if state["nudged"]:
            return {"stop_reason": StopReason.NO_TOOL_CALL}
        stop = StopReason.MAX_ITERATIONS if state["iterations"] >= settings.max_iterations else None
        return {"messages": [HumanMessage(content=NUDGE)], "nudged": True, "stop_reason": stop}

    def after_agent(state: AgentState) -> str:
        reply = state["messages"][-1]
        asked = isinstance(reply, AIMessage) and (reply.tool_calls or reply.invalid_tool_calls)
        return "tools" if asked else "nudge"

    def continue_or_stop(state: AgentState) -> str:
        return END if state["stop_reason"] is not None else "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("nudge", nudge)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", _router(after_agent), ["tools", "nudge"])
    graph.add_conditional_edges("tools", _router(continue_or_stop), ["agent", END])
    graph.add_conditional_edges("nudge", _router(continue_or_stop), ["agent", END])
    return graph.compile(name=RUN_NAME)
