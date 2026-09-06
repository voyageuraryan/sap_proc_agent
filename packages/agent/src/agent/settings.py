"""Runtime configuration for the agent.

Read once, at the edge (cli.py), into a typed object that is then passed
inward. No module below the CLI reads os.environ, which is what makes
run_agent() callable from a test with a hand-built settings object.

Secrets are deliberately absent from this class. The provider API key is read
from the environment by LiteLLM itself, so it never lands in a Python object
that could be logged, repr'd into a traceback, or serialised into an AgentRun.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="forbid")

    # Where the mock ERP lives. The agent speaks HTTP to it like any third
    # party would -- it does not import erp_domain. See decisions.md.
    erp_base_url: str = "http://localhost:8000/sap/opu/odata/sap/ZPROC_SRV"

    # LiteLLM's "provider/model" form. Swapping providers is a config change.
    model: str = "anthropic/claude-sonnet-4-5"

    # Hard ceiling on tool-calling rounds. Without it, a model that keeps
    # re-reading the same document burns tokens until the bill arrives.
    max_iterations: int = 8

    request_timeout: float = 10.0

    # 0.0 because this is a classification task with a right answer, and
    # because the eval suite has to be reproducible run to run.
    temperature: float = 0.0

    # -- observability ---------------------------------------------------
    # On by default, because a run you cannot inspect is a run you cannot
    # debug. It degrades to a no-op when no backend is configured, so this
    # being True never requires anything to be installed or reachable.
    tracing: bool = True

    # A local JSONL file. Dependency-free, offline, greppable -- this is what
    # makes tracing demonstrable without a Langfuse account.
    trace_file: Path | None = None

    # Sent to Langfuse so a CI run and a laptop run are distinguishable in the
    # UI. Without these every trace looks like it came from the same place,
    # which is the first thing you want to filter by.
    trace_environment: str = "local"
    trace_release: str | None = None

    # Prompts and tool results are the most useful thing in a trace and the
    # most likely place for anything sensitive to appear. On here because the
    # ERP data is synthetic; the switch exists because that will not always be
    # true. See decisions.md.
    trace_payloads: bool = True


@lru_cache
def get_settings() -> AgentSettings:
    """Cached so the environment is read exactly once per process."""
    return AgentSettings()
