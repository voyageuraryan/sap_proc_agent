"""Observability: one trace per run, one span per LLM call and per tool call.

The design constraint that shapes this whole module: **the loop must not know
what a tracer is made of.** `loop.py` imports `Tracer` and calls
`tracer.span(...)`; it never imports langfuse, and it works identically when
tracing is off. That is what keeps "observability is part of the demo" from
becoming "the demo needs a SaaS account to run at all".

Three backends, chosen at the edge by `build_tracer`:

  NullTracer       off. Yields a span that discards everything.
  JsonlTracer      one JSON object per span, appended to a local file. No
                   dependencies, no account, works offline. This is what makes
                   tracing demonstrable on a laptop with no network.
  LangfuseTracer   the real thing: nested spans, token usage, cost, and a URL
                   you can open.

Two rules hold everywhere below:

  * A tracing failure must never fail the run. Every backend call is wrapped.
    An observability tool that can take down the thing it observes is worse
    than no observability tool.
  * Span payloads are built from an ALLOW-LIST of fields, never by dumping a
    settings object or an environment. Blocklisting secrets means being right
    every time forever; allow-listing means being right once.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from agent.cost import TokenCost

#: What kind of thing a span measures. "llm" becomes a Langfuse *generation*
#: (the kind that carries model, tokens and cost); the others are plain spans.
SpanKind = Literal["run", "llm", "tool"]

#: Fields a caller may set on a span. Anything else is dropped, loudly in
#: tests and silently in production -- see Span.update.
SPAN_FIELDS = frozenset({"input", "output", "metadata", "error", "model", "usage", "cost", "name"})


class Span(Protocol):
    """A live measurement. Update it as the outcome becomes known."""

    def update(self, **fields: Any) -> None: ...

    @property
    def trace_id(self) -> str | None: ...


class Tracer(ABC):
    """A tracing backend. One method, used as a context manager."""

    #: Human-readable backend name, for the CLI banner and for tests.
    backend: str = "none"

    @abstractmethod
    def span(self, name: str, *, kind: SpanKind, **fields: Any) -> Any:
        """Open a span. Must be usable as `with tracer.span(...) as span:`."""

    @property
    def trace_id(self) -> str | None:
        return None

    @property
    def trace_url(self) -> str | None:
        return None

    def flush(self) -> None:  # noqa: B027 - a no-op default is correct here
        """Block until buffered spans are delivered. Called once, at the edge.

        Deliberately NOT called by run_agent: an eval that runs 200 invoices
        would then pay a network round trip 200 times. The CLI flushes in its
        finally block; the eval harness flushes once at the end.
        """


# ---------------------------------------------------------------------------
# off
# ---------------------------------------------------------------------------


class _NullSpan:
    def update(self, **fields: Any) -> None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None


class NullTracer(Tracer):
    """Tracing disabled. Every call is a no-op and nothing is imported."""

    backend = "none"
    _span = _NullSpan()

    @contextmanager
    def span(self, name: str, *, kind: SpanKind, **fields: Any) -> Iterator[_NullSpan]:
        yield self._span


# ---------------------------------------------------------------------------
# local file
# ---------------------------------------------------------------------------


class _JsonlSpan:
    def __init__(self, record: dict):
        self._record = record

    def update(self, **fields: Any) -> None:
        for key, value in fields.items():
            if key in SPAN_FIELDS:
                self._record[key] = value

    @property
    def trace_id(self) -> str | None:
        return None


class JsonlTracer(Tracer):
    """Append one JSON object per span to a file.

    Spans are written on *close*, so a parent appears after its children --
    which is the same order a flame graph is built in, and means a crashed run
    still leaves everything that completed.

    `depth` and `seq` are recorded so the nesting can be rebuilt without a
    span-id scheme; for a single-run file the order plus the depth is enough.
    """

    backend = "jsonl"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._depth = 0
        self._seq = 0

    @contextmanager
    def span(self, name: str, *, kind: SpanKind, **fields: Any) -> Iterator[_JsonlSpan]:
        self._seq += 1
        record: dict[str, Any] = {
            "seq": self._seq,
            "depth": self._depth,
            "name": name,
            "kind": kind,
        }
        record.update({k: v for k, v in fields.items() if k in SPAN_FIELDS})
        span = _JsonlSpan(record)
        self._depth += 1
        started = time.perf_counter()
        try:
            yield span
        finally:
            self._depth -= 1
            record["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            self._write(record)

    def _write(self, record: dict) -> None:
        try:
            cost = record.get("cost")
            if isinstance(cost, TokenCost):
                record["cost"] = cost.as_langfuse_cost_details()
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        except OSError:
            # A full disk must not end the run. Losing a trace line is the
            # correct thing to lose.
            return


# ---------------------------------------------------------------------------
# langfuse
# ---------------------------------------------------------------------------


class _LangfuseSpan:
    """Adapter from this module's field names to the Langfuse SDK's."""

    def __init__(self, native: Any, kind: SpanKind):
        self._native = native
        self._kind = kind

    def update(self, **fields: Any) -> None:
        payload: dict[str, Any] = {}
        for key in ("input", "output", "metadata", "model", "name"):
            if key in fields and fields[key] is not None:
                payload[key] = fields[key]

        # An error is a level plus a message in Langfuse, not a field. Mapping
        # it here is why the loop can just say error="PO_NOT_FOUND".
        error = fields.get("error")
        if error:
            payload["level"] = "ERROR"
            payload["status_message"] = str(error)

        if self._kind == "llm":
            usage = fields.get("usage")
            if usage:
                payload["usage_details"] = usage
            cost = fields.get("cost")
            if isinstance(cost, TokenCost):
                details = cost.as_langfuse_cost_details()
                if details is not None:
                    payload["cost_details"] = details

        if not payload:
            return
        # Suppressed on purpose: tracing must never break the run.
        with contextlib.suppress(Exception):
            self._native.update(**payload)

    @property
    def trace_id(self) -> str | None:
        try:
            return str(self._native.trace_id)
        except Exception:  # noqa: BLE001
            return None


class LangfuseTracer(Tracer):
    """Nested spans in Langfuse, with token usage and cost on the LLM spans."""

    backend = "langfuse"

    def __init__(self, client: Any):
        self._client = client
        self._trace_id: str | None = None

    @contextmanager
    def span(self, name: str, *, kind: SpanKind, **fields: Any) -> Iterator[Any]:
        as_type = "generation" if kind == "llm" else "span"
        opening: dict[str, Any] = {"name": name, "as_type": as_type}
        for key in ("input", "metadata", "model"):
            if fields.get(key) is not None:
                opening[key] = fields[key]

        try:
            manager = self._client.start_as_current_observation(**opening)
        except Exception:  # noqa: BLE001 - degrade to a no-op, do not fail the run
            yield _NullSpan()
            return

        with manager as native:
            span = _LangfuseSpan(native, kind)
            if self._trace_id is None:
                self._trace_id = span.trace_id
            # Whatever was passed but not accepted at open time (output on a
            # cheap span, for instance) is applied as an update.
            leftover = {k: v for k, v in fields.items() if k not in opening and k in SPAN_FIELDS}
            if leftover:
                span.update(**leftover)
            yield span

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    @property
    def trace_url(self) -> str | None:
        if self._trace_id is None:
            return None
        try:
            return self._client.get_trace_url(trace_id=self._trace_id)
        except Exception:  # noqa: BLE001
            return None

    def flush(self) -> None:
        # A backend that cannot be reached must not fail the process that was
        # only trying to tell it something.
        with contextlib.suppress(Exception):
            self._client.flush()


# ---------------------------------------------------------------------------
# fan-out
# ---------------------------------------------------------------------------


class _CompositeSpan:
    def __init__(self, spans: list[Any]):
        self._spans = spans

    def update(self, **fields: Any) -> None:
        for span in self._spans:
            span.update(**fields)

    @property
    def trace_id(self) -> str | None:
        for span in self._spans:
            if span.trace_id:
                return span.trace_id
        return None


class CompositeTracer(Tracer):
    """Send every span to several backends.

    Exists because the useful local setup is *both*: a jsonl file you can grep
    in the terminal, and Langfuse for the shareable view.
    """

    def __init__(self, tracers: list[Tracer]):
        self._tracers = tracers
        self.backend = "+".join(t.backend for t in tracers)

    @contextmanager
    def span(self, name: str, *, kind: SpanKind, **fields: Any) -> Iterator[_CompositeSpan]:
        from contextlib import ExitStack

        with ExitStack() as stack:
            spans = [
                stack.enter_context(tracer.span(name, kind=kind, **fields))
                for tracer in self._tracers
            ]
            yield _CompositeSpan(spans)

    @property
    def trace_id(self) -> str | None:
        for tracer in self._tracers:
            if tracer.trace_id:
                return tracer.trace_id
        return None

    @property
    def trace_url(self) -> str | None:
        for tracer in self._tracers:
            if tracer.trace_url:
                return tracer.trace_url
        return None

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

    tracers: list[Tracer] = []

    trace_file = getattr(settings, "trace_file", None)
    if trace_file:
        with contextlib.suppress(OSError):
            tracers.append(JsonlTracer(Path(trace_file)))

    if langfuse_is_configured(environ):
        client = _langfuse_client(settings)
        if client is not None:
            tracers.append(LangfuseTracer(client))

    if not tracers:
        return NullTracer()
    if len(tracers) == 1:
        return tracers[0]
    return CompositeTracer(tracers)


def _langfuse_client(settings: Any) -> Any:
    """Construct the SDK client, or None if that is not possible.

    `environment` and `release` are passed through so a trace can be filtered
    by where it came from -- without them, a CI run and a laptop run are
    indistinguishable in the UI, which is the first thing you want to slice by.
    """
    try:
        from langfuse import Langfuse
    except ImportError:
        return None
    try:
        return Langfuse(
            environment=getattr(settings, "trace_environment", None) or "local",
            release=getattr(settings, "trace_release", None),
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# what goes INTO a span
# ---------------------------------------------------------------------------

#: Configuration worth seeing next to a trace. An allow-list, so adding a
#: secret to AgentSettings can never leak it into a span by default.
TRACED_SETTINGS = ("model", "temperature", "max_iterations", "erp_base_url", "request_timeout")


def settings_metadata(settings: Any) -> dict[str, Any]:
    return {
        name: getattr(settings, name)
        for name in TRACED_SETTINGS
        if getattr(settings, name, None) is not None
    }
