"""The options builder must never produce a configuration that weakens the gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.security.gate import Gate
from app.security.policy import RunPolicy
from app.services.options_builder import UnsafeOptions, build_options, setting_sources
from app.services.snapshot import ModelSpec, RoleSpec, RunSnapshot


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return Path(os.path.realpath(project))


def make_snapshot(root: Path, **overrides) -> RunSnapshot:
    defaults = dict(
        task_id=1,
        task_name="demo",
        prompt="do the thing",
        project_path=str(root),
        root=root,
        roles=[
            RoleSpec("Architect", "designs", "You design.", True),
            RoleSpec("Engineer", "implements", "You implement.", False),
        ],
        model=ModelSpec(model_id="claude-sonnet-5"),
        skills=[],
        mcp_servers={},
        trusted_servers=frozenset(),
        trust_project_settings=False,
        sandbox_bash=True,
        paranoid_mode=False,
        network_enabled=False,
        approval_timeout_s=300,
        max_turns=None,
        max_budget_usd=None,
    )
    defaults.update(overrides)
    return RunSnapshot(**defaults)


def build(snapshot: RunSnapshot):
    policy = RunPolicy(root=snapshot.root)

    async def emit(*args, **kwargs):
        return None

    async def audit(**kwargs):
        return None

    gate = Gate(policy, emit=emit, audit=audit)
    return build_options(snapshot, policy, gate, session_id="run-1")


def test_allowed_tools_is_empty(root: Path):
    options, _ = build(make_snapshot(root))
    assert options.allowed_tools == []


def test_skills_is_never_all(root: Path):
    options, _ = build(make_snapshot(root, skills=["code-review"]))
    assert options.skills == ["code-review"]


def test_permission_mode_and_callbacks_are_wired(root: Path):
    options, _ = build(make_snapshot(root))
    assert options.permission_mode == "default"
    assert options.can_use_tool is not None
    assert options.hooks["PreToolUse"]


def test_paranoid_mode_uses_dont_ask(root: Path):
    options, _ = build(make_snapshot(root, paranoid_mode=True))
    assert options.permission_mode == "dontAsk"
    # The hook still vetoes even though nothing will prompt.
    assert options.hooks["PreToolUse"]


def test_network_tools_disallowed_unless_enabled(root: Path):
    off, _ = build(make_snapshot(root))
    on, _ = build(make_snapshot(root, network_enabled=True))
    assert set(off.disallowed_tools) == {"WebFetch", "WebSearch"}
    assert on.disallowed_tools == []


def test_cwd_is_the_resolved_root_and_no_dirs_are_added(root: Path):
    options, _ = build(make_snapshot(root))
    assert options.cwd == str(root)
    assert options.add_dirs == []


def test_settings_carry_deny_rules_but_no_allow_list(root: Path):
    options, blob = build(make_snapshot(root))
    assert "deny" in blob["permissions"]
    assert "allow" not in blob["permissions"]
    assert json.loads(options.settings) == blob


def test_deny_rules_cover_the_git_hooks_escape(root: Path):
    _, blob = build(make_snapshot(root))
    joined = " ".join(blob["permissions"]["deny"])
    assert ".git/hooks" in joined
    assert ".ssh" in joined


def test_sandbox_is_strict_when_enabled(root: Path):
    options, _ = build(make_snapshot(root))
    assert options.sandbox["enabled"] is True
    assert options.sandbox["allowUnsandboxedCommands"] is False
    assert options.sandbox["autoAllowBashIfSandboxed"] is False


def test_sandbox_absent_when_disabled(root: Path):
    options, _ = build(make_snapshot(root, sandbox_bash=False))
    assert options.sandbox is None


def test_strict_mcp_config_is_always_on(root: Path):
    options, _ = build(make_snapshot(root))
    assert options.strict_mcp_config is True


def test_teammates_become_subagents_and_lead_is_appended(root: Path):
    options, _ = build(make_snapshot(root))
    assert set(options.agents) == {"Engineer"}
    appended = options.system_prompt["append"]
    assert "Architect" in appended
    assert "Engineer" in appended


def test_single_role_team_has_no_agents(root: Path):
    snapshot = make_snapshot(root, roles=[RoleSpec("Solo", "d", "You work alone.", True)])
    options, _ = build(snapshot)
    assert options.agents is None


def test_setting_sources_isolation_rules(root: Path):
    assert setting_sources(make_snapshot(root)) == []
    assert setting_sources(make_snapshot(root, skills=["x"])) == ["user"]
    assert setting_sources(make_snapshot(root, trust_project_settings=True)) == ["user", "project"]


def test_env_denylist_is_applied(root: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    options, _ = build(make_snapshot(root))
    assert "GITHUB_TOKEN" not in options.env
    assert "AWS_SECRET_ACCESS_KEY" not in options.env


def test_local_provider_without_gateway_is_rejected(root: Path):
    snapshot = make_snapshot(root, model=ModelSpec(provider="ollama", model_id="llama3"))
    with pytest.raises(Exception, match="gateway"):
        build(snapshot)


def test_local_provider_with_gateway_sets_base_url(root: Path):
    snapshot = make_snapshot(
        root,
        model=ModelSpec(provider="ollama", model_id="llama3", base_url="http://localhost:4000"),
    )
    options, _ = build(snapshot)
    assert options.env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"


def test_assert_safe_catches_a_whole_tool_allow_entry(root: Path, monkeypatch):
    """A refactor that reintroduces a shadowing entry must fail loudly."""
    import app.services.options_builder as builder

    original = builder.ClaudeAgentOptions

    def patched(**kwargs):
        kwargs["allowed_tools"] = ["Write"]
        return original(**kwargs)

    monkeypatch.setattr(builder, "ClaudeAgentOptions", patched)
    with pytest.raises(UnsafeOptions, match="shadow"):
        build(make_snapshot(root))
