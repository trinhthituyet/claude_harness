"""Resolve a Task row (plus its team, model, skills and MCP servers) into an
immutable snapshot. Everything a run needs is copied here so later edits to the
task cannot change a run in flight, or make its history unreadable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import McpServer, ModelConfig, Skill, Task
from app.security.policy import resolve_project_root


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    system_prompt: str
    is_lead: bool
    model: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    name: str = "default"
    provider: str = "anthropic"
    model_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "api_key_set": bool(self.api_key),
        }


@dataclass
class RunSnapshot:
    task_id: int
    task_name: str
    prompt: str
    project_path: str
    root: Path
    roles: list[RoleSpec]
    model: ModelSpec
    skills: list[str]
    mcp_servers: dict[str, dict[str, Any]]
    trusted_servers: frozenset[str]
    trust_project_settings: bool
    sandbox_bash: bool
    paranoid_mode: bool
    network_enabled: bool
    approval_timeout_s: int
    max_turns: int | None
    max_budget_usd: float | None

    @property
    def lead(self) -> RoleSpec:
        for role in self.roles:
            if role.is_lead:
                return role
        return self.roles[0]

    @property
    def teammates(self) -> list[RoleSpec]:
        lead = self.lead
        return [r for r in self.roles if r is not lead]

    def team_public(self) -> dict[str, Any]:
        return {
            "lead": self.lead.name,
            "roles": [
                {
                    "name": r.name,
                    "description": r.description,
                    "system_prompt": r.system_prompt,
                    "is_lead": r.is_lead,
                    "model": r.model,
                }
                for r in self.roles
            ],
        }


def _mcp_config(row: McpServer) -> dict[str, Any]:
    if row.transport == "stdio":
        config: dict[str, Any] = {"type": "stdio", "command": row.command or ""}
        if row.args_json:
            config["args"] = list(row.args_json)
        if row.env_json:
            config["env"] = dict(row.env_json)
        return config
    config = {"type": row.transport, "url": row.url or ""}
    if row.headers_json:
        config["headers"] = dict(row.headers_json)
    return config


async def build_snapshot(session: AsyncSession, task: Task) -> RunSnapshot:
    """Read everything the run needs, and validate the project path."""
    root = resolve_project_root(task.project_path)

    members = sorted(task.team.members, key=lambda m: m.position)
    if not members:
        raise ValueError(f"team {task.team.name!r} has no roles")
    has_lead = any(m.is_lead for m in members)
    roles = [
        RoleSpec(
            name=m.role.name,
            description=m.role.description,
            system_prompt=m.role.system_prompt,
            is_lead=m.is_lead or (not has_lead and i == 0),
        )
        for i, m in enumerate(members)
    ]

    model = ModelSpec()
    model_row: ModelConfig | None = None
    if task.model_config_id is not None:
        model_row = await session.get(ModelConfig, task.model_config_id)
    if model_row is None:
        model_row = (
            await session.execute(select(ModelConfig).where(ModelConfig.is_default.is_(True)))
        ).scalars().first()
    if model_row is not None:
        model = ModelSpec(
            name=model_row.name,
            provider=model_row.provider,
            model_id=model_row.model_id,
            base_url=model_row.base_url,
            api_key=model_row.api_key,
            extra_env=dict(model_row.extra_env_json or {}),
        )

    skill_ids = [link.skill_id for link in task.skill_links]
    skills: list[str] = []
    if skill_ids:
        rows = (
            await session.execute(select(Skill).where(Skill.id.in_(skill_ids)))
        ).scalars().all()
        skills = [r.name for r in rows if r.enabled and r.status == "installed"]

    server_ids = [link.server_id for link in task.mcp_links]
    mcp_servers: dict[str, dict[str, Any]] = {}
    trusted: set[str] = set()
    if server_ids:
        rows = (
            await session.execute(select(McpServer).where(McpServer.id.in_(server_ids)))
        ).scalars().all()
        for row in rows:
            if not row.enabled or row.status != "installed":
                continue
            mcp_servers[row.name] = _mcp_config(row)
            if row.trusted:
                trusted.add(row.name)

    return RunSnapshot(
        task_id=task.id,
        task_name=task.name,
        prompt=task.prompt,
        project_path=task.project_path,
        root=root,
        roles=roles,
        model=model,
        skills=skills,
        mcp_servers=mcp_servers,
        trusted_servers=frozenset(trusted),
        trust_project_settings=task.trust_project_settings,
        sandbox_bash=task.sandbox_bash,
        paranoid_mode=task.paranoid_mode,
        network_enabled=task.network_enabled,
        approval_timeout_s=task.approval_timeout_s,
        max_turns=task.max_turns,
        max_budget_usd=task.max_budget_usd,
    )
