"""Record a live run once; replay it forever, for free, in CI.

The agent's loop already takes `completion_fn` as an argument, so a cassette
is just a different callable: one that returns the response the real provider
gave last time. Nothing in the agent changes, and nothing in the agent knows
it is being replayed.

The design decision that matters is the FINGERPRINT. Each recorded turn stores
a hash of the request that produced it -- the messages, the tool schemas, the
model, the temperature. On replay the hash is recomputed and compared. If the
system prompt, a tool description, or the schema has changed since recording,
the hashes differ and replay FAILS.

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

#: Bump when the on-disk shape changes, so an old cassette fails loudly
#: instead of being misread by a newer reader.
CASSETTE_VERSION = 1


class CassetteError(RuntimeError):
    """A cassette is missing, exhausted, or no longer matches the code."""


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
            "tools": sorted((t["function"]["name"], t["function"]["description"]) for t in tools),
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


class Recorder:
    """Wraps a real completion_fn and remembers what it returned."""

    def __init__(self, inner: Any, scenario_id: str, invoice_number: str, model: str):
        self._inner = inner
        self.cassette = Cassette(
            scenario_id=scenario_id, invoice_number=invoice_number, model=model, turns=[]
        )

    def __call__(self, **kwargs: Any) -> Any:
        response = self._inner(**kwargs)
        self.cassette.turns.append(
            {
                "fingerprint": fingerprint(
                    model=kwargs["model"],
                    messages=kwargs["messages"],
                    tools=kwargs["tools"],
                    temperature=kwargs.get("temperature", 0.0),
                ),
                "response": _to_dict(response),
            }
        )
        return response


class Replayer:
    """Returns recorded responses in order, verifying each request first."""

    def __init__(self, cassette: Cassette, *, strict: bool = True):
        self.cassette = cassette
        self.strict = strict
        self.index = 0
        self.mismatches: list[int] = []

    def __call__(self, **kwargs: Any) -> Any:
        if self.index >= len(self.cassette.turns):
            raise CassetteError(
                f"{self.cassette.scenario_id}: the agent asked for turn "
                f"{self.index + 1} but the cassette holds {len(self.cassette.turns)}. "
                f"The loop now makes more calls than when this was recorded -- re-record."
            )
        turn = self.cassette.turns[self.index]
        actual = fingerprint(
            model=kwargs["model"],
            messages=kwargs["messages"],
            tools=kwargs["tools"],
            temperature=kwargs.get("temperature", 0.0),
        )
        if actual != turn["fingerprint"]:
            self.mismatches.append(self.index)
            if self.strict:
                raise CassetteError(
                    f"{self.cassette.scenario_id} turn {self.index + 1}: the request no "
                    f"longer matches the recording. The prompt, a tool description or a "
                    f"schema changed since this cassette was made, so replaying it would "
                    f"score a prompt that was never run. Re-record with --record."
                )
        self.index += 1
        return _from_dict(turn["response"])


def _to_dict(response: Any) -> dict:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)


def _from_dict(data: dict) -> Any:
    """Rebuild the provider's own response type.

    Deliberately litellm's ModelResponse rather than a local shim: the loop
    must see in replay exactly the type it sees in production, or the cassette
    tests a different code path than the one that runs.
    """
    from litellm import ModelResponse

    return ModelResponse(**data)
