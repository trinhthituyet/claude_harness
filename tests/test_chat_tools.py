"""The chat assistant's tools, called directly the way the SDK calls them."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from app.services import chat_tools


@pytest.fixture
async def db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_DB", str(tmp_path / "chat.db"))
    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    await app.db.init_db()
    yield
    await app.db.dispose_db()


async def call(tool, **args):
    """Invoke a tool exactly as the SDK would, and decode its result."""
    result = await tool.handler(args)
    text = result["content"][0]["text"]
    if result.get("is_error"):
        return {"error": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


# ------------------------------------------------------------ tool inventory


def test_tool_names_are_unique():
    names = [t.name for t in chat_tools.ALL_TOOLS]
    assert len(names) == len(set(names))


def test_mutating_set_matches_real_tools():
    names = {t.name for t in chat_tools.ALL_TOOLS}
    assert chat_tools.MUTATING_TOOLS <= names
    assert chat_tools.READ_ONLY_TOOLS <= names
    # Every tool is classified exactly once.
    assert chat_tools.MUTATING_TOOLS | chat_tools.READ_ONLY_TOOLS == names
    assert not (chat_tools.MUTATING_TOOLS & chat_tools.READ_ONLY_TOOLS)


def _read_only_hint(tool) -> bool:
    """Read the hint in either spelling: mcp versions differ on the field name."""
    annotations = tool.annotations
    if annotations is None:
        return False
    return bool(
        getattr(annotations, "read_only_hint", None)
        or getattr(annotations, "readOnlyHint", None)
    )


def test_gating_agrees_with_the_advertised_annotations():
    """A tool marked readOnlyHint must be the same set we let run unconfirmed."""
    annotated = {t.name for t in chat_tools.ALL_TOOLS if _read_only_hint(t)}
    assert annotated == chat_tools.READ_ONLY_TOOLS


def test_no_delete_tool_is_exposed():
    """Deletion is deliberately left to the panels, which confirm it explicitly."""
    assert not [t for t in chat_tools.ALL_TOOLS if "delete" in t.name or "remove" in t.name]


def test_is_mutating_classification():
    assert chat_tools.is_mutating(chat_tools.qualified("create_role")) is True
    assert chat_tools.is_mutating(chat_tools.qualified("run_task")) is True
    assert chat_tools.is_mutating(chat_tools.qualified("list_roles")) is False
    assert chat_tools.is_mutating(chat_tools.qualified("check_project_path")) is False


def test_unknown_tools_are_treated_as_mutating():
    """Fail closed: anything unrecognised needs a confirmation, not a free pass."""
    assert chat_tools.is_mutating("mcp__other__whatever") is True
    assert chat_tools.is_mutating("Bash") is True
    assert chat_tools.is_mutating(chat_tools.qualified("some_future_tool")) is True


def test_read_only_tools_are_annotated():
    by_name = {t.name: t for t in chat_tools.ALL_TOOLS}
    for name in ("list_roles", "list_teams", "check_project_path"):
        assert _read_only_hint(by_name[name]), name


# ------------------------------------------------------------------ behaviour


async def test_create_and_list_role(db):
    created = await call(
        chat_tools.create_role,
        name="Reviewer",
        description="Reviews diffs",
        system_prompt="You review code for correctness.",
    )
    assert created["created"] == "role"
    listed = await call(chat_tools.list_roles)
    assert [r["name"] for r in listed] == ["Reviewer"]

    full = await call(chat_tools.get_role, role_id=created["id"])
    assert full["system_prompt"] == "You review code for correctness."


async def test_duplicate_role_is_an_error_the_model_can_read(db):
    await call(chat_tools.create_role, name="Dup", description="", system_prompt="x")
    again = await call(chat_tools.create_role, name="Dup", description="", system_prompt="x")
    assert "already exists" in again["error"]


async def test_update_role_replaces_the_prompt(db):
    created = await call(chat_tools.create_role, name="R", description="d", system_prompt="old")
    await call(
        chat_tools.update_role,
        role_id=created["id"],
        name="R",
        description="d",
        system_prompt="new",
    )
    assert (await call(chat_tools.get_role, role_id=created["id"]))["system_prompt"] == "new"


async def test_create_team_requires_lead_among_roles(db):
    a = await call(chat_tools.create_role, name="A", description="", system_prompt="a")
    b = await call(chat_tools.create_role, name="B", description="", system_prompt="b")
    bad = await call(
        chat_tools.create_team, name="T", description="", role_ids=[a["id"]],
        lead_role_id=b["id"],
    )
    assert "must be one of" in bad["error"]

    good = await call(
        chat_tools.create_team, name="T", description="",
        role_ids=[a["id"], b["id"]], lead_role_id=b["id"],
    )
    leads = [r["name"] for r in good["roles"] if r["is_lead"]]
    assert leads == ["B"]


async def test_create_team_with_unknown_role(db):
    result = await call(
        chat_tools.create_team, name="T", description="", role_ids=[999], lead_role_id=999
    )
    assert "unknown role ids" in result["error"]


async def test_check_project_path(db, tmp_path):
    ok = await call(chat_tools.check_project_path, path=str(tmp_path))
    assert ok["ok"] is True
    assert ok["resolved"] == str(Path(os.path.realpath(tmp_path)))

    bad = await call(chat_tools.check_project_path, path="/definitely/not/here")
    assert bad["ok"] is False
    assert "does not exist" in bad["error"]


async def test_add_mcp_server_validates_transport(db):
    bad = await call(chat_tools.add_mcp_server, name="x", description="", transport="carrier")
    assert "transport must be" in bad["error"]

    missing_command = await call(
        chat_tools.add_mcp_server, name="x", description="", transport="stdio"
    )
    assert "needs a command" in missing_command["error"]

    created = await call(
        chat_tools.add_mcp_server, name="git", description="git tools",
        transport="stdio", command="uvx", args=["mcp-server-git"],
    )
    assert created["created"] == "mcp_server"
    assert created["trusted"] is False


async def test_add_model_config_rejects_local_without_gateway(db):
    bad = await call(
        chat_tools.add_model_config, name="local", provider="ollama", model_id="llama3"
    )
    assert "gateway" in bad["error"]

    good = await call(
        chat_tools.add_model_config, name="local", provider="ollama", model_id="llama3",
        base_url="http://localhost:4000",
    )
    assert good["created"] == "model_config"
    assert good["api_key_set"] is False


async def test_create_task_end_to_end(db, tmp_path):
    role = await call(chat_tools.create_role, name="Eng", description="", system_prompt="build")
    team = await call(
        chat_tools.create_team, name="Squad", description="",
        role_ids=[role["id"]], lead_role_id=role["id"],
    )
    project = tmp_path / "proj"
    project.mkdir()
    task = await call(
        chat_tools.create_task,
        name="tidy up",
        prompt="Tidy the imports",
        project_path=str(project),
        team_id=team["id"],
    )
    assert task["created"] == "task"
    assert task["sandbox_bash"] is True
    assert task["network_enabled"] is False
    assert [t["name"] for t in await call(chat_tools.list_tasks)] == ["tidy up"]


async def test_create_task_rejects_a_bad_project_path(db):
    role = await call(chat_tools.create_role, name="Eng", description="", system_prompt="build")
    team = await call(
        chat_tools.create_team, name="Squad", description="",
        role_ids=[role["id"]], lead_role_id=role["id"],
    )
    result = await call(
        chat_tools.create_task, name="t", prompt="p",
        project_path="/definitely/not/here", team_id=team["id"],
    )
    assert "does not exist" in result["error"]


async def test_server_advertises_only_our_tools(db):
    server = chat_tools.build_server()
    assert server["type"] == "sdk"
    assert server["name"] == chat_tools.SERVER_NAME
