"""What one run produced, as plain data.

Split out of the loop so the graph, the tracers and the eval harness can all
import the record types without importing each other. These shapes are the
agent's public output contract -- the eval scores them, the review UI renders
them, `--json` prints them -- and they are unchanged by the LangChain rebuild.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from agent.schemas import Resolution


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
    #: The full transcript, replayable, in the provider-neutral OpenAI shape
    #: (role / content / tool_calls / tool_call_id). Dicts, not LangChain
    #: message objects, so this round-trips through JSON and every consumer
    #: reads one dialect whatever provider produced it.
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
