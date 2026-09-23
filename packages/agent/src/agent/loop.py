"""Run the agent on one invoice. The package's single entry point.

The loop itself is now a LangGraph graph (graph.py). This module is the seam
around it: build the prompt, bind the tools to an ERP client, pick a chat
model, attach the tracing callbacks, invoke the graph, and fold the final
state into an `AgentRun` -- the same record, field for field, that the
hand-written loop used to return. Everything downstream (the eval harness,
the review UI, `--json`) reads that record and nothing else, which is why the
rebuild did not have to touch them beyond the model seam.

Three things are injectable, each for a reason:

  chat_model  any LangChain BaseChatModel. A scripted fake in the tests, the
              rule engine in the eval baseline, a cassette in CI, the real
              provider otherwise. The graph cannot tell which.
  tracer      so tests can assert on the callback stream without a backend.
  callbacks   extra LangChain handlers for this run only -- the eval harness
              records cassettes through one.

`run_agent` behaves identically with tracing off --
`test_tracing_does_not_change_the_run` asserts exactly that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.cost import TokenCost, total_cost
from agent.erp_client import ErpClient
from agent.graph import RUN_NAME, build_graph, initial_state, recursion_limit
from agent.messages import to_openai_dicts
from agent.prompts import SYSTEM_PROMPT, user_prompt

# Re-exported: these were defined here before the rebuild, and the eval
# harness, the review UI and the demo import them from this module.
from agent.records import AgentRun, LlmCallRecord, StopReason, ToolCallRecord
from agent.settings import AgentSettings
from agent.tools import build_tools
from agent.tracing import Tracer, build_tracer, settings_metadata

if TYPE_CHECKING:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models import BaseChatModel

__all__ = [
    "AgentRun",
    "LlmCallRecord",
    "StopReason",
    "ToolCallRecord",
    "run_agent",
]


def run_agent(
    invoice_number: str,
    client: ErpClient,
    settings: AgentSettings,
    *,
    scenario_id: str | None = None,
    chat_model: BaseChatModel | None = None,
    tracer: Tracer | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> AgentRun:
    """Resolve one invoice. Returns a record of what happened.

    A model or network failure propagates, exactly as it did before the
    rebuild: the CLI reports it, and the eval harness records it as a failed
    case. Everything that goes wrong INSIDE a tool comes back as a message.

    Deliberately does NOT flush the tracer: the caller decides when to pay for
    a network round trip. The CLI flushes once, in a finally block.
    """
    if chat_model is None:
        from agent.models import build_chat_model

        chat_model = build_chat_model(settings)
    if tracer is None:
        tracer = build_tracer(settings)

    graph = build_graph(chat_model, build_tools(client, scenario_id=scenario_id), settings)
    config: dict[str, Any] = {
        "run_name": RUN_NAME,
        "callbacks": [*tracer.callbacks(), *(callbacks or [])],
        "metadata": {
            "invoice_number": invoice_number,
            "scenario_id": scenario_id,
            **settings_metadata(settings),
        },
        "tags": ["proc-agent"],
        "recursion_limit": recursion_limit(settings),
    }
    final = graph.invoke(
        initial_state(
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=user_prompt(invoice_number)),
            ]
        ),
        config,
    )

    run = _to_run(invoice_number, settings, final)
    run.trace_backend = tracer.backend
    tracer.record_outcome(run)
    run.trace_id = tracer.trace_id
    run.trace_url = tracer.trace_url
    return run


def _to_run(invoice_number: str, settings: AgentSettings, final: dict[str, Any]) -> AgentRun:
    """Fold the graph's final state into the record everything downstream reads."""
    llm_records: list[LlmCallRecord] = final["llm_records"]
    run_cost = total_cost(
        [TokenCost(input_usd=r.input_usd, output_usd=r.output_usd) for r in llm_records]
    )
    return AgentRun(
        invoice_number=invoice_number,
        model=settings.model,
        # A graph that ended without a stop reason would be a routing bug;
        # reported as the cap rather than hidden as success.
        stop_reason=final["stop_reason"] or StopReason.MAX_ITERATIONS,
        iterations=final["iterations"],
        resolution=final["resolution"],
        tool_calls=final["tool_records"],
        llm_calls=llm_records,
        messages=to_openai_dicts(final["messages"]),
        prompt_tokens=sum(r.prompt_tokens for r in llm_records),
        completion_tokens=sum(r.completion_tokens for r in llm_records),
        input_usd=run_cost.input_usd,
        output_usd=run_cost.output_usd,
        total_usd=run_cost.total_usd,
    )
