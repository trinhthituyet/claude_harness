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


USER_SETTINGS = Path.home() / ".claude" / "settings.json"


def _read_user_settings() -> dict[str, Any]:
    try:
        data = json.loads(USER_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_api_key_helper() -> str | None:
    """The ``apiKeyHelper`` to carry into a session, if one is configured."""
    override = app_settings.api_key_helper
    if override:
        return override
    helper = _read_user_settings().get("apiKeyHelper")
    return helper if isinstance(helper, str) and helper else None


def inherited_auth_settings(exclude_env: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Copy the authentication parts of ``~/.claude/settings.json`` — and only those.

    Sessions run with ``setting_sources=[]`` so that a ``.claude/settings.local.json``
    inside a target project cannot pre-approve tools (docs/DESIGN.md 4.5). That also
    stops the *user's* settings loading, which is where an installation keeps the
    credentials that make plain ``claude`` work: ``apiKeyHelper``, and an ``env`` block
    that can carry required headers, base URLs and certificate paths. Without them a
    request can be rejected as an invalid key, or hang.

    So we take exactly the auth and environment keys across, and never ``permissions``
    — inheriting those is what the isolation is there to prevent.

    ``exclude_env`` drops keys the run configures itself, so a task pointed at a local
    gateway is not silently redirected by an inherited ``ANTHROPIC_BASE_URL``.
    """
    data = _read_user_settings()
    out: dict[str, Any] = {}

    helper = resolve_api_key_helper()
    if helper:
        out["apiKeyHelper"] = helper

    env = {
        key: value
        for key, value in (data.get("env") or {}).items()
        if isinstance(value, str) and key not in exclude_env
    }
    if env:
        out["env"] = env
    return out


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


def build_settings(
    policy: RunPolicy,
    *,
    needs_api_key_helper: bool = True,
    exclude_env: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    blob: dict[str, Any] = {"permissions": {"deny": deny_rules(policy)}}
    if needs_api_key_helper:
        blob.update(inherited_auth_settings(exclude_env))
    return blob
