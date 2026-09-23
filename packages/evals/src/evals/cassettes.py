"""Record a live run once; replay it forever, for free, in CI.

The agent graph takes any LangChain chat model, so a cassette is two small
LangChain pieces and nothing in the agent changes:

  Recorder   a callback HANDLER. It watches the real model -- every
             on_chat_model_start and the matching on_llm_end -- and writes
             down what it was asked and what it answered. Observing rather
             than wrapping means the real model's call is untouched: it still
             appears in Langfuse as the generation it is, with its own usage.
  Replayer   a chat MODEL. It answers each call with the next recorded
             reply, after checking the request is the one that was recorded.

The design decision that matters is the FINGERPRINT. Each recorded turn stores
a hash of the request that produced it -- the messages, the tool names and
descriptions, the model, the temperature. On replay the hash is recomputed and
compared. If the system prompt, a tool description, or the model has changed
since recording, the hashes differ and replay FAILS.

That is deliberate and it is the whole value of the mechanism. A cassette that
happily replays against a changed prompt is worse than no cassette: it reports
a green eval for a prompt that was never actually tested, which is a false
negative on exactly the change you most wanted to measure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from agent.messages import to_openai_dicts
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import message_to_dict, messages_from_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, PrivateAttr

#: Bump when the on-disk shape changes, so an old cassette fails loudly
#: instead of being misread by a newer reader. 2 = LangChain message dicts
#: (the pre-rebuild format stored LiteLLM ModelResponse dicts).
CASSETTE_VERSION = 2


class CassetteError(RuntimeError):
    """A cassette is missing, exhausted, or no longer matches the code."""


def _tool_signature(tool: dict) -> tuple[str, str]:
    """(name, description), whichever provider format the tool was rendered in.

    The recorder sees tools as the PROVIDER was sent them (Anthropic's flat
    {name, description, input_schema}); the replayer sees LangChain's OpenAI
    rendering ({type, function: {...}}). Both carry the same two strings, and
    those are what the model reads.
    """
    function = tool.get("function", tool)
    return str(function.get("name", "")), str(function.get("description", ""))


def fingerprint(*, model: str, messages: list[dict], tools: list[dict], temperature: float) -> str:
    """A stable hash of everything that determines the model's answer.

    tool_call ids are stripped: the provider generates them, so they differ
    between the recording run and the replay run through no fault of ours,
    and including them would make every cassette single-use.
    """
    scrubbed = []
    for message in messages:
        copy = {k: v for k, v in message.items() if k != "tool_call_id"}
        calls = copy.get("tool_calls")
        if calls:
            copy["tool_calls"] = [
                {k: v for k, v in (call if isinstance(call, dict) else {}).items() if k != "id"}
                for call in calls
            ]
        scrubbed.append(copy)

    payload = json.dumps(
        {
            "model": model,
            "temperature": temperature,
            "messages": scrubbed,
            # Only names and descriptions: the model reads those. Reordering
            # the registry should not invalidate a recording, so they sort.
            "tools": sorted(_tool_signature(t) for t in tools),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class Cassette:
    """The recorded turns of one scenario, in order."""

    scenario_id: str
    invoice_number: str
    model: str
    turns: list[dict]
    version: int = CASSETTE_VERSION

    @classmethod
    def load(cls, path: Path) -> Cassette:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CassetteError(f"no cassette at {path}; record one first") from exc
        except json.JSONDecodeError as exc:
            raise CassetteError(f"{path} is not valid JSON: {exc}") from exc
        if raw.get("version") != CASSETTE_VERSION:
            raise CassetteError(
                f"{path} is version {raw.get('version')}, this build reads "
                f"{CASSETTE_VERSION}; re-record"
            )
        return cls(
            scenario_id=raw["scenario_id"],
            invoice_number=raw["invoice_number"],
            model=raw["model"],
            turns=raw["turns"],
            version=raw["version"],
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys + LF so a re-recording that changed nothing is an empty
        # git diff, and a recording that DID change is readable in review.
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "version": self.version,
                    "scenario_id": self.scenario_id,
                    "invoice_number": self.invoice_number,
                    "model": self.model,
                    "turns": self.turns,
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")


def cassette_path(directory: Path, scenario_id: str) -> Path:
    return Path(directory) / f"{scenario_id}.json"


class Recorder(BaseCallbackHandler):
    """Watches a real chat model and remembers each request and reply.

    Attached as a callback for one run. It never touches the model: the
    request is fingerprinted from `on_chat_model_start` (the messages, and
    the tools as `invocation_params` shows them), and the reply is taken from
    the matching `on_llm_end`.

    `raise_error` is on, unlike every tracing handler: a recording that
    silently missed a turn would replay as a different run, so a failure to
    record is a failure of the run.
    """

    raise_error = True

    def __init__(self, scenario_id: str, invoice_number: str, model: str, temperature: float):
        self.model = model
        self.temperature = temperature
        self.cassette = Cassette(
            scenario_id=scenario_id, invoice_number=invoice_number, model=model, turns=[]
        )
        self._pending: dict[UUID, str] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs: Any) -> None:
        params = kwargs.get("invocation_params") or {}
        self._pending[run_id] = fingerprint(
            model=self.model,
            messages=to_openai_dicts(messages[0]),
            tools=list(params.get("tools") or []),
            temperature=self.temperature,
        )

    def on_llm_end(self, response, *, run_id, **kwargs: Any) -> None:
        request = self._pending.pop(run_id, None)
        if request is None:
            return
        message = response.generations[0][0].message
        self.cassette.turns.append({"fingerprint": request, "response": message_to_dict(message)})


class Replayer(BaseChatModel):
    """A chat model that returns recorded replies in order, verifying each request.

    The reply is rebuilt as the same LangChain message type the provider
    produced (an AIMessage with its tool_calls and usage), so the graph sees
    in replay exactly what it saw when the cassette was recorded.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cassette: Cassette
    #: What the fingerprint is computed against. The CURRENT settings, not
    #: the cassette's own record, so changing the model invalidates it.
    replay_model: str = ""
    replay_temperature: float = 0.0
    strict: bool = True

    _index: int = PrivateAttr(default=0)
    _mismatches: list[int] = PrivateAttr(default_factory=list)

    def __init__(
        self,
        cassette: Cassette,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        strict: bool = True,
    ):
        super().__init__(
            cassette=cassette,
            replay_model=cassette.model if model is None else model,
            replay_temperature=temperature,
            strict=strict,
        )

    @property
    def _llm_type(self) -> str:
        return "cassette-replay"

    @property
    def index(self) -> int:
        return self._index

    @property
    def mismatches(self) -> list[int]:
        return self._mismatches

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        turns = self.cassette.turns
        if self._index >= len(turns):
            raise CassetteError(
                f"{self.cassette.scenario_id}: the agent asked for turn "
                f"{self._index + 1} but the cassette holds {len(turns)}. "
                f"The graph now makes more calls than when this was recorded -- re-record."
            )
        turn = turns[self._index]
        actual = fingerprint(
            model=self.replay_model,
            messages=to_openai_dicts(messages),
            tools=list(kwargs.get("tools") or []),
            temperature=self.replay_temperature,
        )
        if actual != turn["fingerprint"]:
            self._mismatches.append(self._index)
            if self.strict:
                raise CassetteError(
                    f"{self.cassette.scenario_id} turn {self._index + 1}: the request no "
                    f"longer matches the recording. The prompt, a tool description or the "
                    f"model changed since this cassette was made, so replaying it would "
                    f"score a prompt that was never run. Re-record with --mode record."
                )
        self._index += 1
        (message,) = messages_from_dict([turn["response"]])
        return ChatResult(generations=[ChatGeneration(message=message)])
