"""Assemble ClaudeAgentOptions for a run.

Two choices here are security decisions, not style:

* ``allowed_tools`` stays **empty**. A whole-tool entry auto-approves the tool
  before ``can_use_tool`` is consulted (verified; docs/DESIGN.md 4.1 probe P3).
* ``skills`` is always an explicit list, never ``"all"`` — ``"all"`` appends a bare
  ``Skill`` entry to the effective allowed tools, which shadows the callback.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from app.config import settings as app_settings
from app.security.gate import Gate
from app.security.policy import RunPolicy
from app.services import model_resolver, prompt_composer
from app.services.settings_builder import build_sandbox, build_settings
from app.services.snapshot import RunSnapshot

# Tools that must never be available, regardless of task configuration.
NETWORK_TOOLS = ("WebFetch", "WebSearch")


class UnsafeOptions(RuntimeError):
    """Raised when a built option set would weaken the permission boundary."""


def _assert_safe(options: ClaudeAgentOptions) -> None:
    """Guard against a refactor quietly reintroducing a shadowing entry."""
    if options.permission_mode in {"bypassPermissions", "acceptEdits"}:
        raise UnsafeOptions(f"permission_mode {options.permission_mode!r} is never allowed")
    for entry in options.allowed_tools:
        bare = entry.split("(", 1)[0].strip()
        if bare == entry.strip():
            raise UnsafeOptions(
                f"allowed_tools entry {entry!r} allows a whole tool and would shadow "
                "can_use_tool"
            )
    if options.skills == "all":
        raise UnsafeOptions('skills="all" shadows can_use_tool; pass an explicit list')
    if options.can_use_tool is None:
        raise UnsafeOptions("can_use_tool must be set")
    if not options.hooks or not options.hooks.get("PreToolUse"):
        raise UnsafeOptions("a PreToolUse hook must be registered")


def setting_sources(snapshot: RunSnapshot) -> list[str]:
    """Which filesystem settings layers to load.

    Default is none: a ``.claude/settings.local.json`` inside the target project
    could otherwise carry allow rules and pre-approve tools. Skills are only
    discoverable through settings, so a task using skills opts into the user layer.
    ``HARNESS_SETTING_SOURCES`` overrides both, for installations whose
    authentication needs the user layer loaded.
    """
    if app_settings.setting_sources is not None:
        return list(app_settings.setting_sources)
    if snapshot.trust_project_settings:
        return ["user", "project"]
    if snapshot.skills:
        return ["user"]
    return []


def build_options(
    snapshot: RunSnapshot,
    policy: RunPolicy,
    gate: Gate,
    *,
    session_id: str,
    stderr_sink=None,
) -> tuple[ClaudeAgentOptions, dict[str, Any]]:
    """Return the options plus the settings blob (for the run record)."""
    model_id, model_env = model_resolver.resolve(snapshot.model)
    # Anything the model config sets must win over the user's inherited settings,
    # or a task pointed at a local gateway would be redirected back to the default.
    settings_blob = build_settings(policy, exclude_env=frozenset(model_env))

    disallowed = [] if snapshot.network_enabled else list(NETWORK_TOOLS)

    options = ClaudeAgentOptions(
        cwd=str(policy.root),
        add_dirs=[],  # reads are unconfined by decision; nothing to widen
        permission_mode="dontAsk" if snapshot.paranoid_mode else "default",
        allowed_tools=[],  # see module docstring
        disallowed_tools=disallowed,
        # In paranoid mode ("dontAsk") nothing ever prompts, so this callback simply
        # never fires — the hook still vetoes. See docs/DESIGN.md 4.4a.
        can_use_tool=gate.can_use_tool,
        hooks={"PreToolUse": [HookMatcher(hooks=[gate.pre_tool_use])]},
        settings=json.dumps(settings_blob),
        sandbox=build_sandbox(snapshot),
        setting_sources=setting_sources(snapshot),
        mcp_servers=dict(snapshot.mcp_servers),
        strict_mcp_config=True,
        skills=snapshot.skills or None,
        agents=prompt_composer.compose_agents(snapshot) or None,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": prompt_composer.compose_system_prompt(snapshot),
        },
        model=model_id,
        env=model_resolver.scrubbed_base_env() | model_env,
        session_id=session_id,
        max_turns=snapshot.max_turns,
        max_budget_usd=snapshot.max_budget_usd,
        include_partial_messages=False,
        forward_subagent_text=True,
        cli_path=app_settings.cli_path,
        stderr=stderr_sink,
    )

    _assert_safe(options)
    return options, settings_blob
