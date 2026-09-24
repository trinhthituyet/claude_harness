"""Per-tool table mapping a tool call to the filesystem paths it touches.

Deliberately explicit and closed: a tool that is not listed here is denied by the
gate rather than allowed by default. See docs/DESIGN.md section 4.6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ToolClass = Literal["write", "read", "exec", "network", "meta", "mcp", "unknown"]


@dataclass(frozen=True)
class ToolSpec:
    """How to interpret one tool's input."""

    tool_class: ToolClass
    path_fields: tuple[str, ...] = field(default_factory=tuple)


# Tool names are the Claude Code built-ins. Keys are matched case-sensitively,
# exactly as they arrive in hook payloads and permission callbacks.
TOOL_TABLE: dict[str, ToolSpec] = {
    # --- writes: confined to the project root ---
    "Write": ToolSpec("write", ("file_path",)),
    "Edit": ToolSpec("write", ("file_path",)),
    "MultiEdit": ToolSpec("write", ("file_path",)),
    "NotebookEdit": ToolSpec("write", ("notebook_path", "file_path")),
    # --- reads: not path-confined (decision 1), still audited ---
    "Read": ToolSpec("read", ("file_path", "notebook_path")),
    "Glob": ToolSpec("read", ("path",)),
    "Grep": ToolSpec("read", ("path",)),
    "NotebookRead": ToolSpec("read", ("notebook_path",)),
    # --- shell: cannot be checked by path extraction, see section 4.8 ---
    "Bash": ToolSpec("exec"),
    "BashOutput": ToolSpec("exec"),
    "KillShell": ToolSpec("exec"),
    "KillBash": ToolSpec("exec"),
    # --- network: gated by tasks.network_enabled ---
    "WebFetch": ToolSpec("network"),
    "WebSearch": ToolSpec("network"),
    # --- no filesystem effect ---
    #: How the CLI delivers a structured result when ``output_format`` is set. It writes
    #: nothing and reads nothing; denying it (as the fail-closed default did) makes the
    #: structured channel come back empty with no obvious cause.
    "StructuredOutput": ToolSpec("meta"),
    "TodoWrite": ToolSpec("meta"),
    "Skill": ToolSpec("meta"),
    "Task": ToolSpec("meta"),
    "Agent": ToolSpec("meta"),
    "ExitPlanMode": ToolSpec("meta"),
    "ListMcpResourcesTool": ToolSpec("meta"),
    "ReadMcpResourceTool": ToolSpec("meta"),
}

MCP_PREFIX = "mcp__"


def classify(tool_name: str) -> ToolSpec:
    """Return the spec for ``tool_name``, failing closed on anything unknown."""
    if tool_name.startswith(MCP_PREFIX):
        return ToolSpec("mcp")
    return TOOL_TABLE.get(tool_name, ToolSpec("unknown"))


def mcp_server_of(tool_name: str) -> str | None:
    """Extract the server name from an ``mcp__<server>__<tool>`` name."""
    if not tool_name.startswith(MCP_PREFIX):
        return None
    rest = tool_name[len(MCP_PREFIX) :]
    server, _, _ = rest.partition("__")
    return server or None


def candidate_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Pull the raw (unresolved) path strings out of a tool input."""
    spec = classify(tool_name)
    out: list[str] = []
    for key in spec.path_fields:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str) and v)
    return out
