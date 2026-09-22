"""API tests: CRUD, validation and the empty/invalid states that matter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_DB", str(tmp_path / "test.db"))
    # config and db read the env at import time, so reload them per test.
    import importlib

    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    import app.main

    importlib.reload(app.main)

    application = app.main.create_app()
    transport = ASGITransport(app=application)
    async with application.router.lifespan_context(application):
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


@pytest.fixture
def project(tmp_path: Path) -> str:
    directory = tmp_path / "proj"
    directory.mkdir()
    return str(Path(os.path.realpath(directory)))


async def make_role(client, name="Engineer"):
    response = await client.post(
        "/api/roles",
        json={"name": name, "description": "builds", "system_prompt": "You build."},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def make_team(client, role_ids, name="Squad"):
    response = await client.post(
        "/api/teams",
        json={
            "name": name,
            "members": [{"role_id": rid, "is_lead": i == 0} for i, rid in enumerate(role_ids)],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------- health


async def test_health_reports_sdk(client):
    body = (await client.get("/api/health")).json()
    assert body["ok"] is True
    assert body["sdk_version"]


# ---------------------------------------------------------------- empty states


async def test_everything_starts_empty(client):
    for path in ("/api/mcps", "/api/models", "/api/roles", "/api/teams", "/api/tasks", "/api/runs"):
        assert (await client.get(path)).json() == []


async def test_suggestions_are_offered_when_empty(client):
    assert len((await client.get("/api/skills/suggested")).json()) > 0
    assert len((await client.get("/api/mcps/suggested")).json()) > 0
    assert len((await client.get("/api/roles/suggested")).json()) > 0


# ----------------------------------------------------------------- validation


async def test_role_needs_a_system_prompt(client):
    response = await client.post("/api/roles", json={"name": "X", "system_prompt": ""})
    assert response.status_code == 422


async def test_duplicate_role_name_conflicts(client):
    await make_role(client, "Dup")
    response = await client.post(
        "/api/roles", json={"name": "Dup", "system_prompt": "x"}
    )
    assert response.status_code == 409


async def test_team_with_zero_roles_rejected(client):
    response = await client.post("/api/teams", json={"name": "Empty", "members": []})
    assert response.status_code == 422


async def test_team_with_two_leads_rejected(client):
    a, b = await make_role(client, "A"), await make_role(client, "B")
    response = await client.post(
        "/api/teams",
        json={
            "name": "TwoLeads",
            "members": [{"role_id": a, "is_lead": True}, {"role_id": b, "is_lead": True}],
        },
    )
    assert response.status_code == 422


async def test_team_defaults_the_first_role_to_lead(client):
    role = await make_role(client, "Solo")
    team_id = await make_team(client, [role], "SoloTeam")
    teams = (await client.get("/api/teams")).json()
    team = next(t for t in teams if t["id"] == team_id)
    assert team["members"][0]["is_lead"] is True


async def test_local_model_without_gateway_rejected(client):
    response = await client.post(
        "/api/models",
        json={"name": "local", "provider": "ollama", "model_id": "llama3"},
    )
    assert response.status_code == 422
    assert "gateway" in response.text


async def test_stdio_mcp_needs_a_command(client):
    response = await client.post("/api/mcps", json={"name": "x", "transport": "stdio"})
    assert response.status_code == 422


async def test_api_key_is_never_echoed_back(client):
    await client.post(
        "/api/models",
        json={
            "name": "anthropic",
            "provider": "anthropic",
            "model_id": "claude-sonnet-5",
            "api_key": "sk-ant-supersecret1234",
        },
    )
    body = (await client.get("/api/models")).json()[0]
    assert "supersecret" not in str(body)
    assert body["api_key_masked"].endswith("1234")


async def test_mcp_env_values_are_not_returned(client):
    await client.post(
        "/api/mcps",
        json={
            "name": "svc",
            "transport": "stdio",
            "command": "echo",
            "env": {"TOKEN": "secret-value"},
        },
    )
    body = (await client.get("/api/mcps")).json()[0]
    assert "secret-value" not in str(body)
    assert body["env_keys"] == ["TOKEN"]


# ---------------------------------------------------------------------- paths


async def test_path_validation_accepts_a_directory(client, project):
    body = (await client.get("/api/fs/validate", params={"path": project})).json()
    assert body["ok"] is True
    assert body["is_dir"] is True


async def test_path_validation_rejects_a_file(client, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    body = (await client.get("/api/fs/validate", params={"path": str(target)})).json()
    assert body["ok"] is False
    assert "not a directory" in body["error"]


async def test_path_validation_rejects_relative(client):
    body = (await client.get("/api/fs/validate", params={"path": "rel/path"})).json()
    assert body["ok"] is False


async def test_ls_lists_directories(client, project):
    (Path(project) / "child").mkdir()
    body = (await client.get("/api/fs/ls", params={"path": project})).json()
    assert [d["name"] for d in body["dirs"]] == ["child"]


# ---------------------------------------------------------------------- tasks


async def test_task_requires_a_valid_project_path(client):
    role = await make_role(client)
    team = await make_team(client, [role])
    response = await client.post(
        "/api/tasks",
        json={
            "name": "bad path",
            "prompt": "go",
            "project_path": "/definitely/not/here",
            "team_id": team,
        },
    )
    assert response.status_code == 422
    assert "does not exist" in response.text


async def test_task_requires_a_prompt(client, project):
    role = await make_role(client)
    team = await make_team(client, [role])
    response = await client.post(
        "/api/tasks",
        json={"name": "no prompt", "prompt": "   ", "project_path": project, "team_id": team},
    )
    assert response.status_code == 422


async def test_task_requires_a_team(client, project):
    response = await client.post(
        "/api/tasks", json={"name": "no team", "prompt": "go", "project_path": project}
    )
    assert response.status_code == 422


async def test_task_round_trip(client, project):
    role = await make_role(client)
    team = await make_team(client, [role])
    created = await client.post(
        "/api/tasks",
        json={
            "name": "ship it",
            "prompt": "Refactor the parser",
            "project_path": project,
            "team_id": team,
            "paranoid_mode": True,
            "network_enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["paranoid_mode"] is True
    assert task["team_name"] == "Squad"

    updated = await client.put(
        f"/api/tasks/{task['id']}",
        json={
            "name": "ship it",
            "prompt": "Refactor the parser properly",
            "project_path": project,
            "team_id": team,
            "paranoid_mode": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["prompt"].endswith("properly")
    assert updated.json()["paranoid_mode"] is False

    assert (await client.delete(f"/api/tasks/{task['id']}")).status_code == 204
    assert (await client.get("/api/tasks")).json() == []


async def test_inline_team_creates_roles_on_the_fly(client, project):
    existing = await make_role(client, "Reviewer")
    response = await client.post(
        "/api/tasks",
        json={
            "name": "inline",
            "prompt": "go",
            "project_path": project,
            "inline_team": {
                "name": "Inline Squad",
                "role_ids": [existing],
                "new_roles": [
                    {"name": "Fresh Role", "description": "new", "system_prompt": "You are new."}
                ],
                "lead_role_name": "Reviewer",
            },
        },
    )
    assert response.status_code == 201, response.text
    teams = (await client.get("/api/teams")).json()
    squad = next(t for t in teams if t["name"] == "Inline Squad")
    assert {m["role_name"] for m in squad["members"]} == {"Reviewer", "Fresh Role"}
    assert next(m for m in squad["members"] if m["is_lead"])["role_name"] == "Reviewer"
    assert any(r["name"] == "Fresh Role" for r in (await client.get("/api/roles")).json())


async def test_unknown_run_is_404(client):
    assert (await client.get("/api/runs/does-not-exist")).status_code == 404
    assert (await client.post("/api/runs/does-not-exist/cancel")).status_code == 404


async def test_pending_approvals_is_empty_with_no_live_runs(client):
    assert (await client.get("/api/runs/pending-approvals")).json() == []
