"""Observability, through LangChain's callback system.

The graph never calls a tracer. It runs; LangChain emits a callback for the
graph, for every node, for every chat-model call and for every tool call; and
whichever handlers are attached record what they care about. That is the
design constraint this module exists to keep: **the agent must not know what
it is being observed by**, and it must behave identically when it is not.

Three backends, chosen at the edge by `build_tracer`:

  NullTracer       off. No handlers are attached at all.
  JsonlTracer      one JSON object per run, appended to a local file. No
                   account, no network. This is what makes tracing
                   demonstrable on a laptop with nothing configured.
  LangfuseTracer   Langfuse's own LangChain `CallbackHandler`: nested
                   observations, generations with model and token usage (and
                   Langfuse's cost), tool spans, and a URL you can open. The
                   run's outcome is attached as scores, so a run that never
                   submitted is one filter away.

A `Tracer` is now a small wrapper that owns its handlers and knows how to
flush them and where the trace lives. The CLI and the eval harness hold one
for the life of the process; `run_agent` passes its `callbacks()` into the
graph config.

Two rules hold everywhere below:

  * A tracing failure must never fail the run. LangChain's callback manager
    already isolates handler errors (they are logged, not raised); every
    backend call made outside that manager is wrapped here as well.
  * What goes into a run's metadata is an ALLOW-LIST of settings, never a dump
    of a settings object or an environment. Blocklisting secrets means being
    right every time forever; allow-listing means being right once.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from agent.cost import cost_for
from agent.messages import text_of, to_openai_dicts
from agent.tools import tool_error_code

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult

    from agent.records import AgentRun

#: What stands in for a prompt or a tool result when payloads are switched off.
REDACTED = "<redacted>"

#: How much of a tool result to keep in a trace line.
TRACE_OUTPUT_CHARS = 240

#: LangChain's "plumbing, not work" tag. The graph puts it on its routing
#: functions; they are noise in a trace of what the AGENT did.
HIDDEN_TAG = "langsmith:hidden"


class Tracer:
    """A tracing backend: some callback handlers, plus where the trace lives."""

    #: Human-readable backend name, for the CLI banner and for tests.
    backend: str = "none"

    def callbacks(self) -> list[BaseCallbackHandler]:
        """The handlers to attach to one run's config."""
        return []

    @property
    def trace_id(self) -> str | None:
        return None

    @property
    def trace_url(self) -> str | None:
        return None

    def record_outcome(self, run: AgentRun) -> None:
        """Attach the verdict to the trace, once the run is over."""

    def flush(self) -> None:
        """Block until buffered data is delivered. Called once, at the edge.

        Deliberately NOT called by run_agent: an eval that runs 200 invoices
        would then pay a network round trip 200 times. The CLI flushes in its
        finally block; the eval harness flushes once at the end.
        """


# ---------------------------------------------------------------------------
# off
# ---------------------------------------------------------------------------


class NullTracer(Tracer):
    """Tracing disabled. No handler is attached and nothing is imported."""

    backend = "none"


# ---------------------------------------------------------------------------
# local file
# ---------------------------------------------------------------------------


class JsonlCallbackHandler(BaseCallbackHandler):
    """Append one JSON object per LangChain run to a file.

    A line is written when its run ENDS, so a parent appears after its
    children -- the order a flame graph is built in -- and a crashed run still
    leaves everything that completed. `depth` is derived from the parent run,
    so the nesting can be rebuilt without an id scheme; `run_id` and
    `parent_run_id` are there when it cannot.
    """

    def __init__(self, path: Path, *, model: str = "", payloads: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.payloads = payloads
        self._open: dict[UUID, dict[str, Any]] = {}
        self._depth: dict[UUID, int] = {}
        self._seq = 0

    # -- plumbing ----------------------------------------------------------

    def _payload(self, value: Any) -> Any:
        """Redaction point. One function, so the switch cannot be half-applied."""
        return value if self.payloads else REDACTED

    def _start(
        self,
        kind: str,
        name: str,
        run_id: UUID,
        parent_run_id: UUID | None,
        tags: list[str] | None,
        **fields: Any,
    ) -> None:
        if HIDDEN_TAG in (tags or []):
            return
        depth = self._depth[parent_run_id] + 1 if parent_run_id in self._depth else 0
        self._depth[run_id] = depth
        self._seq += 1
        self._open[run_id] = {
            "seq": self._seq,
            "depth": depth,
            "kind": kind,
            "name": name,
            "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "_started": time.perf_counter(),
            **fields,
        }

    def _end(self, run_id: UUID, **fields: Any) -> None:
        record = self._open.pop(run_id, None)
        self._depth.pop(run_id, None)
        if record is None:
            return
        record.update(fields)
        record["duration_ms"] = round((time.perf_counter() - record.pop("_started")) * 1000.0, 2)
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        except OSError:
            # A full disk must not end the run. Losing a trace line is the
            # correct thing to lose.
            return

    # -- chains: the graph and its nodes -------------------------------------

    def on_chain_start(
        self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs
    ):
        name = kwargs.get("name") or (serialized or {}).get("name") or "chain"
        extra = {"metadata": dict(metadata or {})} if parent_run_id is None else {}
        self._start("chain", name, run_id, parent_run_id, tags, **extra)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end(run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=type(error).__name__)

    # -- the model -------------------------------------------------------------

    def on_chat_model_start(
        self, serialized, messages, *, run_id, parent_run_id=None, tags=None, **kwargs
    ):
        name = kwargs.get("name") or (serialized or {}).get("name") or "chat_model"
        prompt = to_openai_dicts(messages[0]) if messages else []
        self._start(
            "llm",
            name,
            run_id,
            parent_run_id,
            tags,
            model=self.model,
            input=self._payload(prompt),
        )

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        message = None
        with contextlib.suppress(AttributeError, IndexError):
            message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None) or {}
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)
        cost = cost_for(self.model, prompt_tokens, completion_tokens)
        output = to_openai_dicts([message])[0] if message is not None else None
        self._end(
            run_id,
            output=self._payload(output),
            usage={"input": prompt_tokens, "output": completion_tokens},
            cost=cost.as_langfuse_cost_details(),
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=type(error).__name__)

    # -- tools -----------------------------------------------------------------

    def on_tool_start(
        self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, inputs=None, **kwargs
    ):
        name = kwargs.get("name") or (serialized or {}).get("name") or "tool"
        self._start(
            "tool",
            name,
            run_id,
            parent_run_id,
            tags,
            input=self._payload(inputs if inputs is not None else input_str),
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        self._end(run_id, output=self._payload(text_of(text)[:TRACE_OUTPUT_CHARS]))

    def on_tool_error(self, error, *, run_id, **kwargs):
        record = self._open.get(run_id) or {}
        self._end(run_id, error=tool_error_code(record.get("name", ""), error))


class JsonlTracer(Tracer):
    """A local JSONL file. Dependency-free, offline, greppable."""

    backend = "jsonl"

    def __init__(self, path: Path, *, model: str = "", payloads: bool = True):
        self.path = Path(path)
        self.handler = JsonlCallbackHandler(self.path, model=model, payloads=payloads)

    def callbacks(self) -> list[BaseCallbackHandler]:
        return [self.handler]


# ---------------------------------------------------------------------------
# langfuse
# ---------------------------------------------------------------------------


class LangfuseTracer(Tracer):
    """Langfuse's own LangChain handler, plus the outcome as trace scores.

    The handler is Langfuse's, not ours: it maps chains to spans, chat models
    to generations (model, usage, and Langfuse's own cost), and tools to tool
    observations. What this class adds is the part a callback cannot know --
    how the run ENDED -- recorded as two categorical scores once it is over.
    """

    backend = "langfuse"

    def __init__(self, client: Any, *, public_key: str | None = None, handler: Any = None):
        self._client = client
        if handler is None:
            from langfuse.langchain import CallbackHandler

            handler = CallbackHandler(public_key=public_key)
        self.handler = handler

    def callbacks(self) -> list[BaseCallbackHandler]:
        return [self.handler]

    @property
    def trace_id(self) -> str | None:
        trace_id = getattr(self.handler, "last_trace_id", None)
        return str(trace_id) if trace_id else None

    @property
    def trace_url(self) -> str | None:
        if self.trace_id is None:
            return None
        try:
            return self._client.get_trace_url(trace_id=self.trace_id)
        except Exception:  # noqa: BLE001
            return None

    def record_outcome(self, run: AgentRun) -> None:
        """Scores, so "every run that never submitted" is a filter, not a search."""
        if self.trace_id is None:
            return
        scores = {"stop_reason": run.stop_reason.value}
        if run.resolution is not None:
            scores["decision"] = run.resolution.decision.value
            scores["classification"] = run.resolution.classification.value
        for name, value in scores.items():
            # Suppressed on purpose: tracing must never break the run.
            with contextlib.suppress(Exception):
                self._client.create_score(
                    name=name, value=value, trace_id=self.trace_id, data_type="CATEGORICAL"
                )

    def flush(self) -> None:
        # A backend that cannot be reached must not fail the process that was
        # only trying to tell it something.
        with contextlib.suppress(Exception):
            self._client.flush()


# ---------------------------------------------------------------------------
# fan-out
# ---------------------------------------------------------------------------


class CompositeTracer(Tracer):
    """Several backends at once.

    Exists because the useful local setup is *both*: a JSONL file you can grep
    in the terminal, and Langfuse for the shareable view. With callbacks this
    is just a longer handler list.
    """

    def __init__(self, tracers: list[Tracer]):
        self._tracers = tracers
        self.backend = "+".join(t.backend for t in tracers)

    def callbacks(self) -> list[BaseCallbackHandler]:
        return [handler for tracer in self._tracers for handler in tracer.callbacks()]

    @property
    def trace_id(self) -> str | None:
        return next((t.trace_id for t in self._tracers if t.trace_id), None)

    @property
    def trace_url(self) -> str | None:
        return next((t.trace_url for t in self._tracers if t.trace_url), None)

    def record_outcome(self, run: AgentRun) -> None:
        for tracer in self._tracers:
            tracer.record_outcome(run)

    def flush(self) -> None:
        for tracer in self._tracers:
            tracer.flush()


# ---------------------------------------------------------------------------
# the factory -- the only place that decides
# ---------------------------------------------------------------------------

#: Both must be present for the Langfuse client to be worth constructing.
#: Constructing it without them prints an authentication error and returns a
#: disabled client, which looks like a bug in this project rather than missing
#: configuration.
LANGFUSE_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def langfuse_is_configured(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return all(env.get(name) for name in LANGFUSE_ENV)


def build_tracer(settings: Any, *, environ: dict[str, str] | None = None) -> Tracer:
    """Pick a backend from configuration. Never raises.

    Order of decisions, all of them explicit:
      1. tracing off  -> NullTracer, and nothing is imported.
      2. trace_file   -> JsonlTracer.
      3. Langfuse keys present AND the SDK installed -> LangfuseTracer.
      4. whatever is left: 0 -> Null, 1 -> itself, 2 -> Composite.
    """
    if not getattr(settings, "tracing", False):
        return NullTracer()

    payloads = bool(getattr(settings, "trace_payloads", True))
    tracers: list[Tracer] = []

    trace_file = getattr(settings, "trace_file", None)
    if trace_file:
        with contextlib.suppress(OSError):
            tracers.append(
                JsonlTracer(
                    Path(trace_file), model=getattr(settings, "model", ""), payloads=payloads
                )
            )

    if langfuse_is_configured(environ):
        env = os.environ if environ is None else environ
        tracer = _langfuse_tracer(settings, public_key=env.get("LANGFUSE_PUBLIC_KEY"))
        if tracer is not None:
            tracers.append(tracer)

    if not tracers:
        return NullTracer()
    if len(tracers) == 1:
        return tracers[0]
    return CompositeTracer(tracers)


def _redact(*, data: Any, **_: Any) -> Any:
    """Langfuse's mask hook: every input and output becomes the placeholder."""
    return REDACTED


def _langfuse_tracer(settings: Any, *, public_key: str | None) -> LangfuseTracer | None:
    """Construct the client and its handler, or None if that is not possible.

    `environment` and `release` are passed through so a trace can be filtered
    by where it came from -- without them, a CI run and a laptop run are
    indistinguishable in the UI, which is the first thing you want to slice
    by. Payload redaction uses the SDK's own `mask`, so it applies to every
    observation the handler creates, not just the ones this module touches.
    """
    try:
        from langfuse import Langfuse
    except ImportError:
        return None
    try:
        client = Langfuse(
            public_key=public_key,
            environment=getattr(settings, "trace_environment", None) or "local",
            release=getattr(settings, "trace_release", None),
            mask=None if getattr(settings, "trace_payloads", True) else _redact,
        )
        return LangfuseTracer(client, public_key=public_key)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# what goes INTO a trace
# ---------------------------------------------------------------------------

#: Configuration worth seeing next to a trace. An allow-list, so adding a
#: secret to AgentSettings can never leak it into a trace by default.
TRACED_SETTINGS = (
    "model",
    "temperature",
    "max_iterations",
    "erp_base_url",
    "request_timeout",
)


def settings_metadata(settings: Any) -> dict[str, Any]:
    return {
        name: getattr(settings, name)
        for name in TRACED_SETTINGS
        if getattr(settings, name, None) is not None
    }
