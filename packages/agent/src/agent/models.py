"""Which chat model the agent talks to, built at the edge.

The graph takes a LangChain `BaseChatModel` and nothing more specific, so a
provider is a configuration value rather than an import. `init_chat_model`
resolves "anthropic:claude-sonnet-4-5" to `ChatAnthropic`, "openai:gpt-4o" to
`ChatOpenAI`, and so on -- each provider's package only has to be installed.
langchain-anthropic is the one this project depends on; the rest are opt-in.

Kept out of `graph.py` on purpose: the graph must be constructible with a
scripted model in a test, a rule engine in the eval baseline, or a cassette in
CI, and none of those should pay for (or need credentials for) a provider
client they will never call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from agent.settings import AgentSettings


def provider_model(model: str) -> str:
    """Normalise a model id to `init_chat_model`'s "provider:model" form.

    Accepts the LiteLLM-style "provider/model" too, so an existing .env or a
    CI input written before the LangChain rebuild keeps working. Only the
    FIRST separator is the provider boundary: "openai:ft:gpt-4o:org" and
    "bedrock/anthropic.claude..." both keep the rest of the id intact.
    """
    if ":" in model.split("/", 1)[0]:
        return model
    provider, sep, rest = model.partition("/")
    return f"{provider}:{rest}" if sep else model


def build_chat_model(settings: AgentSettings) -> BaseChatModel:
    """The real model. Imported lazily so `import agent.graph` stays cheap.

    The API key is NOT passed here. The provider package reads it from the
    environment, so it never enters a Python object that could be logged,
    repr'd into a traceback, or serialised into an AgentRun.
    """
    from langchain.chat_models import init_chat_model

    return init_chat_model(
        provider_model(settings.model),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        timeout=settings.llm_timeout,
    )
