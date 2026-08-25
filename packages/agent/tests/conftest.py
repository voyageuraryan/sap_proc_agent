"""Shared fixtures for the agent tests.

Two fakes, and one thing that is deliberately NOT fake.

  * The LLM is scripted. Model output is the one input we cannot make
    deterministic, so the loop is tested against a fixed sequence of
    tool calls. That tests the *harness*; whether the model reasons
    correctly is what the eval suite (Step 8) measures.

  * The ERP is REAL. FastAPI's TestClient is an httpx.Client subclass, so it
    can be injected straight into ErpClient. Requests go through real
    routing, real dependency injection, real Pydantic serialisation and the
    real store -- just no socket. A mocked ERP here would have tested our
    idea of the contract instead of the contract.
"""

import json
import tempfile
import uuid
from pathlib import Path

import pytest
from agent.erp_client import ErpClient
from agent.settings import AgentSettings
from fastapi.testclient import TestClient
from mock_erp.app import create_app
from mock_erp.settings import Settings
from pydantic import BaseModel

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
    return AgentSettings(model="test/scripted", max_iterations=4, temperature=0.0)


# ---------------------------------------------------------------------------
# The scripted model
# ---------------------------------------------------------------------------


class FakeFunction(BaseModel):
    name: str
    #: A JSON *string*, exactly as a provider sends it -- including the chance
    #: of it being malformed.
    arguments: str


class FakeToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FakeFunction


class FakeMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


class FakeChoice(BaseModel):
    message: FakeMessage


class FakeUsage(BaseModel):
    prompt_tokens: int = 100
    completion_tokens: int = 20


class FakeResponse(BaseModel):
    choices: list[FakeChoice]
    usage: FakeUsage = FakeUsage()


def call(name: str, **arguments) -> FakeToolCall:
    """Build one tool call the way a provider would: arguments as a JSON string."""
    return FakeToolCall(
        id=f"call_{uuid.uuid4().hex[:8]}",
        function=FakeFunction(name=name, arguments=json.dumps(arguments)),
    )


def raw_call(name: str, arguments: str) -> FakeToolCall:
    """Same, but with the argument string handed over verbatim (for bad JSON)."""
    return FakeToolCall(
        id=f"call_{uuid.uuid4().hex[:8]}", function=FakeFunction(name=name, arguments=arguments)
    )


def says(text: str) -> FakeResponse:
    """A turn where the model replies with prose and no tool call."""
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=text))])


def calls(*tool_calls: FakeToolCall) -> FakeResponse:
    """A turn where the model emits one or more tool calls."""
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(tool_calls=list(tool_calls)))])


class ScriptedModel:
    """A completion_fn that replays a fixed list of turns.

    Records the kwargs it was handed, so tests can assert on what the loop
    actually sent -- the tool schemas, the message shapes, the temperature.
    """

    def __init__(self, *turns: FakeResponse):
        self.turns = list(turns)
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.turns:
            # Better than IndexError: says which test ran the script dry.
            raise AssertionError(
                f"the scripted model ran out of turns after {len(self.calls)} call(s)"
            )
        return self.turns.pop(0)

    @property
    def last_messages(self) -> list[dict]:
        return self.calls[-1]["messages"]
