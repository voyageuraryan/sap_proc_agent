"""LangChain messages to the provider-neutral transcript, and back to text.

The graph works in LangChain `BaseMessage`s. Everything that leaves the agent
-- `AgentRun.messages`, the cassette fingerprint, the JSONL trace -- uses the
OpenAI dict shape instead, because that is the one dialect every consumer in
this repo already reads.

`langchain_core.messages.convert_to_openai_messages` is NOT used, on purpose:
it drops `invalid_tool_calls` (a call whose arguments were not valid JSON).
Those calls are still answered by a tool message, so dropping them from the
assistant turn would leave a transcript where a tool result answers a call
that was never made -- exactly the malformed shape the protocol forbids.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def text_of(content: Any) -> str:
    """The text of a message, whatever shape the provider used.

    Anthropic replies carry a list of content blocks (text and tool_use side
    by side); OpenAI replies carry a string. Only the text is kept -- tool
    calls are read from `tool_calls`, which LangChain normalises across both.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def _assistant(message: AIMessage) -> dict:
    out: dict[str, Any] = {"role": "assistant"}
    text = text_of(message.content)
    if text:
        out["content"] = text
    calls = [
        {
            "id": call.get("id") or "",
            "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call.get("args", {}))},
        }
        for call in message.tool_calls
    ]
    calls += [
        {
            "id": call.get("id") or "",
            "type": "function",
            # Verbatim: it is not JSON, which is the whole point of the record.
            "function": {"name": call.get("name") or "", "arguments": call.get("args") or ""},
        }
        for call in message.invalid_tool_calls
    ]
    if calls:
        out["tool_calls"] = calls
    return out


def to_openai_dicts(messages: Sequence[BaseMessage]) -> list[dict]:
    """Render a LangChain transcript as OpenAI-shaped dicts. Never drops a turn."""
    out: list[dict] = []
    for message in messages:
        if isinstance(message, AIMessage):
            out.append(_assistant(message))
        elif isinstance(message, ToolMessage):
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": text_of(message.content),
                }
            )
        elif isinstance(message, SystemMessage):
            out.append({"role": "system", "content": text_of(message.content)})
        elif isinstance(message, HumanMessage):
            out.append({"role": "user", "content": text_of(message.content)})
        else:
            out.append({"role": message.type, "content": text_of(message.content)})
    return out
