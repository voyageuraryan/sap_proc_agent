"""Shared fixtures for the agent tests.

Two fakes, and one thing that is deliberately NOT fake.

  * The LLM is scripted. Model output is the one input we cannot make
    deterministic, so the graph is tested against a fixed sequence of
    tool calls. The script is a real LangChain `BaseChatModel`, so the
    graph drives it through the same `bind_tools` -> `invoke` path it uses
    for ChatAnthropic. That tests the *harness*; whether the model reasons
    correctly is what the eval suite (Step 8) measures.

  * The trace backend is an in-memory LangChain callback handler, so the
    shape of what Langfuse would receive can be asserted without Langfuse.

  * The ERP is REAL. FastAPI's TestClient is an httpx.Client subclass, so it
    can be injected straight into ErpClient. Requests go through real
    routing, real dependency injection, real Pydantic serialisation and the
    real store -- just no socket. A mocked ERP here would have tested our
    idea of the contract instead of the contract.
"""

import tempfile
import uuid
from pathlib import Path

import pytest
from agent.erp_client import ErpClient
from agent.messages import to_openai_dicts
from agent.settings import AgentSettings
from agent.tracing import HIDDEN_TAG, Tracer
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from mock_erp.app import create_app
from mock_erp.settings import Settings
from pydantic import PrivateAttr

ODATA_PREFIX = "/sap/opu/odata/sap/ZPROC_SRV"

# The SC-0009 anchor, used across the suite:
#   PO  4500000009  vendor 1000000010  line 00010  14.000 EA @ 41.90
#   GR  5000000901  13.000 received
#   INV 5100000901  14.000 @ 41.90, block_reason QUANTITY_VARIANCE
#   tolerance: price 10.0%, quantity 5.0%
INVOICE = "5100000901"
PO = "4500000009"
VENDOR = "1000000010"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data" / "erp" / "invoices.json").exists():
            return parent
    raise RuntimeError("could not locate the repo root from the test file")


@pytest.fixture
def erp_app():
    """A fresh app over the committed ERP data and an empty proposals DB."""
    settings = Settings(
        erp_data_dir=_repo_root() / "data" / "erp",
        db_path=Path(tempfile.mkdtemp()) / "approvals.sqlite3",
    )
    app = create_app(settings)
    # The context manager is what runs the lifespan, which is what populates
    # app.state.store and app.state.repository.
    with TestClient(app, base_url="http://erp" + ODATA_PREFIX) as client:
        yield client


@pytest.fixture
def erp_client(erp_app):
    """An ErpClient talking to the real app in-process."""
    return ErpClient(base_url=str(erp_app.base_url), client=erp_app)


@pytest.fixture
def settings():
    """Tracing off by default in tests, so a trace backend is never implicit."""
    return AgentSettings(model="test/scripted", max_iterations=4, temperature=0.0, tracing=False)


# ---------------------------------------------------------------------------
# The recording trace backend
# ---------------------------------------------------------------------------


def _name(serialized, kwargs) -> str | None:
    """LangChain passes the run name as a kwarg for some run types and only in
    `serialized` for others (tools); the real backends read both."""
    return kwargs.get("name") or (serialized or {}).get("name")


class RecordingHandler(BaseCallbackHandler):
    """A LangChain callback handler that keeps every run in memory.

    Runs are recorded on START, so ordering is the order they were entered,
    and the same dict is updated on end, so the final state is what a real
    backend received. LangGraph routing plumbing (tagged hidden) is skipped,
    exactly as the JSONL backend skips it and Langfuse demotes it.
    """

    def __init__(self):
        self.runs: list[dict] = []
        self._by_id: dict = {}

    def _start(self, kind, name, run_id, parent_run_id, tags, **fields):
        if HIDDEN_TAG in (tags or []):
            return
        parent = self._by_id.get(parent_run_id)
        record = {
            "kind": kind,
            "name": name,
            "depth": parent["depth"] + 1 if parent else 0,
            **fields,
        }
        self.runs.append(record)
        self._by_id[run_id] = record

    def _end(self, run_id, **fields):
        record = self._by_id.get(run_id)
        if record is not None:
            record.update(fields)

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self._start(
            "chain",
            _name(serialized, kwargs),
            run_id,
            parent_run_id,
            kwargs.get("tags"),
            metadata=dict(kwargs.get("metadata") or {}),
        )

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end(run_id, outputs=outputs)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._start(
            "llm",
            _name(serialized, kwargs),
            run_id,
            parent_run_id,
            kwargs.get("tags"),
            input=to_openai_dicts(messages[0]),
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = response.generations[0][0].message
        self._end(run_id, usage=dict(message.usage_metadata or {}))

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self._start(
            "tool",
            _name(serialized, kwargs),
            run_id,
            parent_run_id,
            kwargs.get("tags"),
            input=kwargs.get("inputs"),
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id, output=output)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)


class RecordingTracer(Tracer):
    """A Tracer whose one handler records everything. No backend, no network."""

    backend = "recording"

    def __init__(self, trace_id: str | None = "trace-test"):
        self.handler = RecordingHandler()
        self._trace_id = trace_id
        self.outcomes: list = []
        self.flushed = False

    def callbacks(self):
        return [self.handler]

    @property
    def runs(self) -> list[dict]:
        return self.handler.runs

    @property
    def trace_id(self):
        return self._trace_id

    @property
    def trace_url(self):
        return None if self._trace_id is None else f"https://langfuse.test/t/{self._trace_id}"

    def record_outcome(self, run):
        self.outcomes.append(run)

    def flush(self):
        self.flushed = True

    def named(self, name: str) -> list[dict]:
        return [r for r in self.runs if r["name"] == name]

    def of_kind(self, kind: str) -> list[dict]:
        return [r for r in self.runs if r["kind"] == kind]


@pytest.fixture
def tracer():
    return RecordingTracer()


# ---------------------------------------------------------------------------
# The scripted model
# ---------------------------------------------------------------------------

#: Every scripted turn reports this usage, so token and cost arithmetic has
#: something non-zero to add up.
USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


def call(name: str, **arguments) -> dict:
    """One well-formed tool call, as LangChain represents it: parsed args."""
    return {
        "name": name,
        "args": arguments,
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "tool_call",
    }


def raw_call(name: str, arguments: str) -> dict:
    """A tool call whose arguments were NOT valid JSON.

    LangChain's output parsers put these in `invalid_tool_calls`, with the raw
    string kept verbatim. The graph must still answer them.
    """
    return {
        "name": name,
        "args": arguments,
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "error": "Expecting value",
        "type": "invalid_tool_call",
    }


def says(text: str) -> AIMessage:
    """A turn where the model replies with prose and no tool call."""
    return AIMessage(content=text, usage_metadata=dict(USAGE))


def calls(*tool_calls: dict) -> AIMessage:
    """A turn where the model emits one or more tool calls."""
    return AIMessage(
        content="",
        tool_calls=[c for c in tool_calls if c["type"] == "tool_call"],
        invalid_tool_calls=[c for c in tool_calls if c["type"] == "invalid_tool_call"],
        usage_metadata=dict(USAGE),
    )


class ScriptedModel(BaseChatModel):
    """A LangChain chat model that replays a fixed list of turns.

    Records what it was handed on every call -- the messages, and the tools
    `bind_tools` rendered -- so tests can assert on what the graph actually
    sent.
    """

    _turns: list[AIMessage] = PrivateAttr(default_factory=list)
    _calls: list[dict] = PrivateAttr(default_factory=list)

    def __init__(self, *turns: AIMessage, **kwargs):
        super().__init__(**kwargs)
        self._turns = list(turns)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append({"messages": list(messages), "tools": kwargs.get("tools", [])})
        if not self._turns:
            # Better than IndexError: says which test ran the script dry.
            raise AssertionError(
                f"the scripted model ran out of turns after {len(self._calls)} call(s)"
            )
        # A copy, so a scripted turn is never mutated by the graph.
        return ChatResult(generations=[ChatGeneration(message=self._turns.pop(0).model_copy())])

    @property
    def turns(self) -> list[AIMessage]:
        return self._turns

    @property
    def calls(self) -> list[dict]:
        return self._calls

    def sent(self, index: int = -1) -> list[dict]:
        """The messages of one call, in the provider-neutral OpenAI shape."""
        return to_openai_dicts(self._calls[index]["messages"])
