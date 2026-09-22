"""Build the settings blob passed to ``ClaudeAgentOptions.settings``.

Carries the deny rules and nothing else permission-wise: there is deliberately no
allow list, because an allow rule shadows ``can_use_tool`` the same way a
whole-tool ``allowed_tools`` entry does. See docs/DESIGN.md 4.3.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings as app_settings
from app.security.policy import RunPolicy
from app.security.rules import deny_rules
from app.services.snapshot import RunSnapshot

log = logging.getLogger("harness.settings")


def resolve_api_key_helper() -> str | None:
    """Find an ``apiKeyHelper`` to carry into the session, if one is needed.

    Sessions run with ``setting_sources=[]`` for isolation (docs/DESIGN.md 4.5),
    which also means the user's ``~/.claude/settings.json`` is *not* loaded — so an
    installation that authenticates through ``apiKeyHelper`` rather than
    ``ANTHROPIC_API_KEY`` would fail to authenticate. Carrying just that one field
    into the generated blob keeps isolation without breaking auth.
    """
    override = app_settings.api_key_helper
    if override:
        return override
    user_settings = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(user_settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    helper = data.get("apiKeyHelper")
    return helper if isinstance(helper, str) and helper else None


def build_sandbox(snapshot: RunSnapshot) -> dict[str, Any] | None:
    """OS-level sandbox settings for Bash. The only real containment for a shell."""
    if not snapshot.sandbox_bash:
        return None
    return {
        "enabled": True,
        # Never auto-approve a sandboxed command: the gate still gets a say.
        "autoAllowBashIfSandboxed": False,
        # Stop the model opting out via dangerouslyDisableSandbox.
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
    }


def build_settings(policy: RunPolicy, *, needs_api_key_helper: bool = True) -> dict[str, Any]:
    blob: dict[str, Any] = {"permissions": {"deny": deny_rules(policy)}}
    if needs_api_key_helper:
        helper = resolve_api_key_helper()
        if helper:
            blob["apiKeyHelper"] = helper
    return blob
