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


@dataclass(frozen=True)
class WorkflowNodeSpec:
    key: str
    role: RoleSpec
    instructions: str
    is_start: bool
    max_visits: int
    #: What this step's output is filed under, so later steps can name it.
    output_key: str = ""

    @property
    def artifact(self) -> str:
        return self.output_key or self.key


@dataclass(frozen=True)
class WorkflowEdgeSpec:
    from_key: str
    to_key: str | None  # None finishes the workflow
    label: str
    condition: str
    is_default: bool
    #: Node keys whose visit budget resets when this edge is taken.
    resets: tuple[str, ...] = ()

    @property
    def conditional(self) -> bool:
        return bool(self.condition.strip())


@dataclass(frozen=True)
class WorkflowSpec:
    """The graph a run walks, frozen at launch."""

    id: int
    name: str
    max_steps: int
    nodes: tuple[WorkflowNodeSpec, ...]
    edges: tuple[WorkflowEdgeSpec, ...]
    #: Where to go when a budget runs out, instead of failing the run.
    escalation_key: str | None = None

    @property
    def start(self) -> WorkflowNodeSpec:
        for node in self.nodes:
            if node.is_start:
                return node
        return self.nodes[0]

    def node(self, key: str) -> WorkflowNodeSpec:
        for node in self.nodes:
            if node.key == key:
                return node
        raise KeyError(key)

    def outgoing(self, key: str) -> list[WorkflowEdgeSpec]:
        return [edge for edge in self.edges if edge.from_key == key]

    def fans_out(self, key: str) -> bool:
        """Several unconditional edges: every one is taken, in parallel."""
        out = self.outgoing(key)
        return len(out) > 1 and all(
            not e.conditional and not e.is_default for e in out
        )

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "max_steps": self.max_steps,
            "escalation": self.escalation_key,
            "start": self.start.key,
            "nodes": [
                {
                    "key": n.key,
                    "role": n.role.name,
                    "instructions": n.instructions,
                    "max_visits": n.max_visits,
                    "output_key": n.artifact,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "from": e.from_key,
                    "to": e.to_key or "END",
                    "label": e.label,
                    "condition": e.condition,
                    "is_default": e.is_default,
                    "resets": list(e.resets),
                }
                for e in self.edges
            ],
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
    #: Set when the task runs a graph instead of a flat delegating team.
    workflow: WorkflowSpec | None = None

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
            "workflow": self.workflow.name if self.workflow else None,
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


def _workflow_spec(row) -> WorkflowSpec:
    by_id = {node.id: node for node in row.nodes}
    return WorkflowSpec(
        id=row.id,
        name=row.name,
        max_steps=row.max_steps,
        nodes=tuple(
            WorkflowNodeSpec(
                key=node.key,
                role=RoleSpec(
                    name=node.role.name,
                    description=node.role.description,
                    system_prompt=node.role.system_prompt,
                    is_lead=True,  # each node runs as its own single-role session
                ),
                instructions=node.instructions,
                is_start=node.is_start,
                max_visits=node.max_visits,
                output_key=node.output_key or node.key,
            )
            for node in row.nodes
        ),
        escalation_key=row.escalation_key,
        edges=tuple(
            WorkflowEdgeSpec(
                from_key=by_id[edge.from_node_id].key,
                to_key=by_id[edge.to_node_id].key if edge.to_node_id in by_id else None,
                label=edge.label,
                condition=edge.condition,
                is_default=edge.is_default,
                resets=tuple(edge.resets_json or []),
            )
            for edge in row.edges
            if edge.from_node_id in by_id
        ),
    )


async def build_snapshot(session: AsyncSession, task: Task) -> RunSnapshot:
    """Read everything the run needs, and validate the project path."""
    root = resolve_project_root(task.project_path)

    workflow: WorkflowSpec | None = None
    roles: list[RoleSpec]

    if task.workflow_id is not None:
        if task.workflow is None:
            raise ValueError(f"task {task.name!r} references a missing workflow")
        workflow = _workflow_spec(task.workflow)
        # Every node's role, in graph order — the roster shown in run history.
        roles = [node.role for node in workflow.nodes]
    else:
        if task.team is None:
            raise ValueError(f"task {task.name!r} has neither a team nor a workflow")
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
        workflow=workflow,
    )
