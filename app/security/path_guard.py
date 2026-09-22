"""Path containment and the per-tool verdict.

This module is the crux of the security boundary and is deliberately pure: no
FastAPI, no DB, no SDK. See docs/DESIGN.md section 4.6.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.security.policy import RunPolicy
from app.security.tool_paths import candidate_paths, classify, mcp_server_of

Decision = Literal["allow", "deny", "ask"]

# Commands that are never worth the risk in a shell we cannot statically analyse.
# This is a speed bump, not the boundary — the OS sandbox is (docs section 4.8).
_BASH_RED_FLAGS = (
    "sudo ",
    "| sh",
    "| bash",
    "curl http",
    "wget http",
    "chmod +s",
    "/.ssh/",
    "/.aws/",
    "/.claude/",
    "launchctl ",
    "crontab ",
)


@dataclass
class Verdict:
    decision: Decision
    reason: str = ""
    candidates: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def resolve_candidate(raw: str, cwd: Path) -> Path:
    """Resolve a possibly-not-yet-existing path, following symlinked parents.

    ``Path.resolve()`` alone is not enough for a file that does not exist yet, and
    plain string joining misses a symlinked parent directory. So: realpath the
    deepest existing ancestor, then re-attach the remaining tail.
    """
    q = Path(raw).expanduser()
    if not q.is_absolute():
        q = cwd / q
    tail: list[str] = []
    anc = q
    # Bounded to avoid pathological loops on odd inputs.
    for _ in range(len(q.parts) + 1):
        if anc.exists():
            break
        if anc.parent == anc:
            break
        tail.append(anc.name)
        anc = anc.parent
    return Path(os.path.realpath(anc)).joinpath(*reversed(tail))


def contained(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or sits underneath it.

    Uses path components, not string prefixes: ``/proj-evil`` must not count as
    inside ``/proj``.
    """
    return path == root or root in path.parents


def contained_in_any(path: Path, roots: list[Path]) -> bool:
    return any(contained(path, r) for r in roots)


def check(tool_name: str, tool_input: dict[str, Any], policy: RunPolicy) -> Verdict:
    """Decide what should happen to one tool call.

    Returns ``allow`` for anything harmless, ``deny`` for anything we refuse
    outright, and ``ask`` only for the one case worth interrupting a human over:
    a write whose resolved path falls outside the project root.
    """
    spec = classify(tool_name)
    raw_paths = candidate_paths(tool_name, tool_input)

    # A NUL byte is a classic truncation trick: the guard would inspect one path
    # while the syscall sees a shorter one. Refuse before resolving anything.
    if any("\x00" in p for p in raw_paths):
        return Verdict("deny", "path contains a NUL byte", list(raw_paths), [])

    resolved = [resolve_candidate(p, policy.root) for p in raw_paths]
    resolved_str = [str(p) for p in resolved]

    def verdict(decision: Decision, reason: str = "") -> Verdict:
        return Verdict(decision, reason, list(raw_paths), resolved_str)

    if spec.tool_class == "unknown":
        return verdict("deny", f"unknown tool {tool_name!r} is not on the harness allow table")

    if spec.tool_class == "mcp":
        server = mcp_server_of(tool_name)
        if policy.trusts(server):
            return verdict("allow", f"MCP server {server!r} is marked trusted")
        return verdict(
            "deny",
            f"MCP server {server!r} is not marked trusted; its tool inputs cannot be "
            "checked against the project boundary",
        )

    if spec.tool_class == "network":
        if policy.network_enabled:
            return verdict("allow", "network tools enabled for this task")
        return verdict("deny", f"{tool_name} is disabled because this task has no network access")

    if spec.tool_class == "meta":
        return verdict("allow")

    if spec.tool_class == "read":
        # Decision 1: reads are not path-confined. Deny rules in the generated
        # settings blob cover the credential set; everything else is allowed and
        # audited. See docs/DESIGN.md section 4.6.
        return verdict("allow", "reads are not path-confined by design")

    if spec.tool_class == "exec":
        command = tool_input.get("command")
        if isinstance(command, str):
            lowered = command.lower()
            for flag in _BASH_RED_FLAGS:
                if flag in lowered:
                    return verdict("deny", f"command contains a blocked pattern: {flag.strip()!r}")
        return verdict("allow", "shell confinement is enforced by the OS sandbox")

    # --- writes ---
    if not resolved:
        return verdict("deny", f"{tool_name} is a write tool but no target path was supplied")
    outside = [str(p) for p in resolved if not contained_in_any(p, policy.write_roots)]
    if outside:
        roots = ", ".join(str(r) for r in policy.write_roots)
        return verdict(
            "ask",
            f"{tool_name} would write outside the project boundary: "
            f"{', '.join(outside)} (allowed: {roots})",
        )
    return verdict("allow")
