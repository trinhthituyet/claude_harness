"""Translate SDK messages into harness events.

Shared by the flat task runner and the workflow runner so both streams look the same
in the UI. Pure functions with no bus or DB involvement, which makes them easy to test
against recorded messages.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

TOOL_RESULT_LIMIT = 8192
THINKING_LIMIT = 2000
TOOL_INPUT_LIMIT = 2000

Event = tuple[str, dict[str, Any]]


def truncate_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Keep tool inputs loggable: long file contents are the common offender."""
    out: dict[str, Any] = {}
    for key, value in (tool_input or {}).items():
        if isinstance(value, str) and len(value) > TOOL_INPUT_LIMIT:
            out[key] = value[:TOOL_INPUT_LIMIT] + f"… [{len(value)} chars]"
        else:
            out[key] = value
    return out


def stringify(content: Any) -> str:
    """Flatten tool-result content, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def translate(message: Any, node: str | None = None) -> list[Event]:
    """The events one SDK message produces.

    ``node`` tags every event with the workflow step it came from, so the UI can
    group a run's output by step.
    """
    events: list[Event] = []

    def add(type_: str, payload: dict[str, Any]) -> None:
        if node is not None:
            payload = {**payload, "node": node}
        events.append((type_, payload))

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    add("assistant_text",
                        {"text": block.text, "parent": message.parent_tool_use_id})
            elif isinstance(block, ThinkingBlock):
                add("thinking", {"text": block.thinking[:THINKING_LIMIT]})
            elif isinstance(block, ToolUseBlock):
                add("tool_use", {
                    "id": block.id,
                    "name": block.name,
                    "input": truncate_input(block.input),
                    "parent": message.parent_tool_use_id,
                })
    elif isinstance(message, UserMessage):
        content = message.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    add("tool_result", {
                        "id": block.tool_use_id,
                        "is_error": bool(block.is_error),
                        "content": stringify(block.content)[:TOOL_RESULT_LIMIT],
                    })
    elif isinstance(message, ResultMessage):
        add("result", {
            "subtype": message.subtype,
            "is_error": message.is_error,
            "num_turns": message.num_turns,
            "total_cost_usd": message.total_cost_usd,
            "duration_ms": message.duration_ms,
            "terminal_reason": message.terminal_reason,
        })

    return events


def collect_text(message: Any) -> list[str]:
    """The assistant prose in a message, for the run's recorded output."""
    if not isinstance(message, AssistantMessage):
        return []
    return [
        block.text
        for block in message.content
        if isinstance(block, TextBlock) and block.text.strip()
    ]
