"""Run-scoped security policy: what this session is allowed to touch.

Pure data plus validation — no FastAPI, no DB, no SDK imports, so it can be
unit-tested directly. See docs/DESIGN.md sections 4.2 and 4.6.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Characters that cannot be expressed unambiguously in a Claude Code permission
# rule's ``Tool(pattern)`` grammar. A project path containing one of these is
# rejected at task-creation time rather than silently producing a broken rule.
RULE_UNSAFE_CHARS = "()[],\n\r\t"


class PolicyError(ValueError):
    """A project path (or other policy input) is unusable."""


def resolve_project_root(raw: str | os.PathLike[str]) -> Path:
    """Validate and canonicalise a task's project path.

    Resolving symlinks here is mandatory, not cosmetic: on macOS ``/tmp`` is a
    symlink to ``/private/tmp``, so an unresolved root makes every later
    containment comparison fail. See docs/DESIGN.md section 4.2.
    """
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise PolicyError("project path must be absolute")
    if "\x00" in str(root):
        raise PolicyError("project path contains a NUL byte")
    resolved = Path(os.path.realpath(root))
    if not resolved.exists():
        raise PolicyError(f"project path does not exist: {resolved}")
    if not resolved.is_dir():
        raise PolicyError(f"project path is not a directory: {resolved}")
    if resolved == Path("/") or resolved == Path.home():
        raise PolicyError("refusing to use / or the home directory as a project root")
    if resolved.parts[:2] in {("/", "etc"), ("/", "System"), ("/", "usr")}:
        raise PolicyError(f"refusing to use a system directory as a project root: {resolved}")
    if any(ch in str(resolved) for ch in RULE_UNSAFE_CHARS):
        raise PolicyError(
            "project path cannot be expressed as a permission rule "
            f"(contains one of {RULE_UNSAFE_CHARS!r}): {resolved}"
        )
    return resolved


@dataclass
class RunPolicy:
    """The security decisions for a single run.

    ``session_roots`` grows at runtime when the user approves an out-of-root
    write with "remember for this run" (docs/DESIGN.md section 4.4a). It is
    intentionally per-run and never persisted.
    """

    root: Path
    trusted_mcp_servers: frozenset[str] = frozenset()
    network_enabled: bool = False
    paranoid: bool = False
    session_roots: list[Path] = field(default_factory=list)

    @property
    def write_roots(self) -> list[Path]:
        return [self.root, *self.session_roots]

    def add_session_root(self, path: str | os.PathLike[str]) -> Path:
        """Widen the write boundary for the rest of this run only."""
        resolved = Path(os.path.realpath(Path(path).expanduser()))
        if resolved not in self.session_roots:
            self.session_roots.append(resolved)
        return resolved

    def trusts(self, server: str | None) -> bool:
        return server is not None and server in self.trusted_mcp_servers
