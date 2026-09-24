"""Write operations shared by the REST routers and the chat assistant's tools.

Both entry points must create entities the same way — same validation, same
defaults, same conflict rules — so the logic lives here once. These functions
raise :class:`CrudError`; the routers translate that into an HTTP status and the
chat tools turn it into an error result the model can read and react to.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    McpServer,
    ModelConfig,
    Role,
    Skill,
    Task,
    TaskMcpServer,
    TaskSkill,
    Team,
    TeamRole,
    Workflow,
)
from app.schemas import InlineTeam, McpIn, ModelIn, RoleIn, TaskIn, TeamIn, WorkflowIn
from app.security.policy import PolicyError, resolve_project_root
from app.services import graph


class CrudError(ValueError):
    """Invalid input. ``conflict`` marks a duplicate rather than a bad value."""

    def __init__(self, message: str, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


# ----------------------------------------------------------------------- roles


async def create_role(session: AsyncSession, payload: RoleIn) -> Role:
    if (await session.execute(select(Role).where(Role.name == payload.name))).scalars().first():
        raise CrudError(f"a role named {payload.name!r} already exists", conflict=True)
    row = Role(
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model_config_id=payload.model_config_id,
    )
    session.add(row)
    await session.flush()
    return row


async def update_role(session: AsyncSession, role_id: int, payload: RoleIn) -> Role:
    row = await session.get(Role, role_id)
    if row is None:
        raise CrudError(f"no role with id {role_id}")
    clash = (
        await session.execute(
            select(Role).where(Role.name == payload.name, Role.id != role_id)
        )
    ).scalars().first()
    if clash is not None:
        raise CrudError(f"another role is already named {payload.name!r}", conflict=True)
    row.name = payload.name
    row.description = payload.description
    row.system_prompt = payload.system_prompt
    row.model_config_id = payload.model_config_id
    await session.flush()
    return row


# ----------------------------------------------------------------------- teams


async def load_team(session: AsyncSession, team_id: int) -> Team:
    """Re-read a team with members eagerly loaded.

    ``session.get`` would hand back the identity-mapped instance whose collections
    may still be unloaded; touching them then triggers a lazy load, which async
    SQLAlchemy cannot do. ``populate_existing`` refreshes the cached instance.
    """
    stmt = (
        select(Team)
        .options(selectinload(Team.members).selectinload(TeamRole.role))
        .where(Team.id == team_id)
        .execution_options(populate_existing=True)
    )
    team = (await session.execute(stmt)).scalars().first()
    if team is None:
        raise CrudError(f"no team with id {team_id}")
    return team


async def _add_members(
    session: AsyncSession, team: Team, members: list[tuple[int, bool]]
) -> None:
    any_lead = any(is_lead for _, is_lead in members)
    for position, (role_id, is_lead) in enumerate(members):
        session.add(
            TeamRole(
                team_id=team.id,
                role_id=role_id,
                position=position,
                is_lead=is_lead or (not any_lead and position == 0),
            )
        )
    await session.flush()


async def create_team(session: AsyncSession, payload: TeamIn) -> Team:
    payload.check()
    if (await session.execute(select(Team).where(Team.name == payload.name))).scalars().first():
        raise CrudError(f"a team named {payload.name!r} already exists", conflict=True)
    role_ids = [m.role_id for m in payload.members]
    await assert_roles_exist(session, role_ids)

    team = Team(name=payload.name, description=payload.description)
    session.add(team)
    await session.flush()
    await _add_members(session, team, [(m.role_id, m.is_lead) for m in payload.members])
    return team


async def replace_team_members(session: AsyncSession, team: Team, payload: TeamIn) -> Team:
    payload.check()
    await assert_roles_exist(session, [m.role_id for m in payload.members])
    team.name = payload.name
    team.description = payload.description
    await session.execute(delete(TeamRole).where(TeamRole.team_id == team.id))
    await _add_members(session, team, [(m.role_id, m.is_lead) for m in payload.members])
    return team


async def assert_roles_exist(session: AsyncSession, role_ids: list[int]) -> dict[int, Role]:
    found = (await session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars().all()
    by_id = {r.id: r for r in found}
    missing = sorted(set(role_ids) - set(by_id))
    if missing:
        raise CrudError(f"unknown role ids: {missing}")
    return by_id


async def materialise_inline_team(session: AsyncSession, spec: InlineTeam) -> int:
    """Create a team, and any brand-new roles it defines, in one step."""
    spec.check()
    if (await session.execute(select(Team).where(Team.name == spec.name))).scalars().first():
        raise CrudError(f"a team named {spec.name!r} already exists", conflict=True)

    role_ids = list(spec.role_ids)
    for new_role in spec.new_roles:
        row = await create_role(session, new_role)
        role_ids.append(row.id)
    by_id = await assert_roles_exist(session, role_ids)

    team = Team(name=spec.name, description=spec.description)
    session.add(team)
    await session.flush()
    lead_index = 0
    if spec.lead_role_name:
        for index, role_id in enumerate(role_ids):
            if by_id[role_id].name == spec.lead_role_name:
                lead_index = index
                break
    await _add_members(
        session, team, [(role_id, i == lead_index) for i, role_id in enumerate(role_ids)]
    )
    return team.id


# ------------------------------------------------------------------ mcp / model


def _apply_mcp(row: McpServer, payload: McpIn) -> None:
    row.name = payload.name
    row.description = payload.description
    row.transport = payload.transport
    row.command = payload.command
    row.args_json = list(payload.args)
    row.env_json = dict(payload.env)
    row.url = payload.url
    row.headers_json = dict(payload.headers)
    row.enabled = payload.enabled
    row.trusted = payload.trusted


async def create_mcp_server(session: AsyncSession, payload: McpIn) -> McpServer:
    payload.check()
    if (
        await session.execute(select(McpServer).where(McpServer.name == payload.name))
    ).scalars().first():
        raise CrudError(f"an MCP server named {payload.name!r} already exists", conflict=True)
    row = McpServer(name=payload.name)
    _apply_mcp(row, payload)
    session.add(row)
    await session.flush()
    return row


async def update_mcp_server(session: AsyncSession, row: McpServer, payload: McpIn) -> McpServer:
    payload.check()
    # An empty env value means "keep the stored secret".
    merged = {**(row.env_json or {}), **{k: v for k, v in payload.env.items() if v}}
    _apply_mcp(row, payload)
    row.env_json = merged
    await session.flush()
    return row


async def clear_default_model(session: AsyncSession, keep_id: int | None) -> None:
    from sqlalchemy import update

    stmt = update(ModelConfig).values(is_default=False)
    if keep_id is not None:
        stmt = stmt.where(ModelConfig.id != keep_id)
    await session.execute(stmt)


async def create_model_config(session: AsyncSession, payload: ModelIn) -> ModelConfig:
    payload.check()
    if (
        await session.execute(select(ModelConfig).where(ModelConfig.name == payload.name))
    ).scalars().first():
        raise CrudError(f"a model config named {payload.name!r} already exists", conflict=True)
    row = ModelConfig(
        name=payload.name,
        provider=payload.provider,
        model_id=payload.model_id,
        base_url=payload.base_url,
        api_key=payload.api_key,
        extra_env_json=dict(payload.extra_env),
        is_default=payload.is_default,
    )
    session.add(row)
    await session.flush()
    if payload.is_default:
        await clear_default_model(session, row.id)
    return row


# ------------------------------------------------------------------- workflows


def workflow_loaders():
    from app.models import WorkflowEdge, WorkflowNode

    return (
        selectinload(Workflow.nodes).selectinload(WorkflowNode.role),
        selectinload(Workflow.edges),
    )


async def load_workflow(session: AsyncSession, workflow_id: int) -> Workflow:
    stmt = (
        select(Workflow)
        .options(*workflow_loaders())
        .where(Workflow.id == workflow_id)
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise CrudError(f"no workflow with id {workflow_id}")
    return row


async def _write_graph(session: AsyncSession, workflow: Workflow, payload: WorkflowIn) -> None:
    """Replace a workflow's nodes and edges wholesale.

    Edges arrive keyed by node *key* rather than id, which is what both the editor and
    the chat assistant naturally produce, so nodes are inserted first to resolve them.
    """
    from app.models import WorkflowEdge, WorkflowNode

    await session.execute(delete(WorkflowEdge).where(WorkflowEdge.workflow_id == workflow.id))
    await session.execute(delete(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id))
    await session.flush()

    role_ids = [node.role_id for node in payload.nodes]
    await assert_roles_exist(session, role_ids)

    has_start = any(node.is_start for node in payload.nodes)
    by_key: dict[str, WorkflowNode] = {}
    for position, node in enumerate(payload.nodes):
        row = WorkflowNode(
            workflow_id=workflow.id,
            key=node.key,
            role_id=node.role_id,
            instructions=node.instructions,
            is_start=node.is_start or (not has_start and position == 0),
            max_visits=node.max_visits,
            output_key=node.output_key or node.key,
            position=position,
            pos_x=node.pos_x,
            pos_y=node.pos_y,
        )
        session.add(row)
        by_key[node.key] = row
    await session.flush()

    for position, edge in enumerate(payload.edges):
        session.add(
            WorkflowEdge(
                workflow_id=workflow.id,
                from_node_id=by_key[edge.from_key].id,
                to_node_id=(
                    None if edge.to_key in (None, graph.END) else by_key[edge.to_key].id
                ),
                label=edge.label,
                condition=edge.condition,
                is_default=edge.is_default,
                resets_json=list(edge.resets),
                position=position,
            )
        )
    await session.flush()


async def create_workflow(session: AsyncSession, payload: WorkflowIn) -> Workflow:
    payload.check()
    if (
        await session.execute(select(Workflow).where(Workflow.name == payload.name))
    ).scalars().first():
        raise CrudError(f"a workflow named {payload.name!r} already exists", conflict=True)
    workflow = Workflow(
        name=payload.name, description=payload.description, max_steps=payload.max_steps,
        escalation_key=payload.escalation_key,
    )
    session.add(workflow)
    await session.flush()
    await _write_graph(session, workflow, payload)
    return workflow


async def update_workflow(
    session: AsyncSession, workflow_id: int, payload: WorkflowIn
) -> Workflow:
    payload.check()
    workflow = await load_workflow(session, workflow_id)
    clash = (
        await session.execute(
            select(Workflow).where(Workflow.name == payload.name, Workflow.id != workflow_id)
        )
    ).scalars().first()
    if clash is not None:
        raise CrudError(f"another workflow is named {payload.name!r}", conflict=True)
    workflow.name = payload.name
    workflow.description = payload.description
    workflow.max_steps = payload.max_steps
    workflow.escalation_key = payload.escalation_key
    await _write_graph(session, workflow, payload)
    return workflow


def summarise_workflow(row: Workflow) -> dict[str, Any]:
    """A compact description for the chat assistant and for run snapshots."""
    by_id = {node.id: node for node in row.nodes}
    return {
        "id": row.id,
        "name": row.name,
        "max_steps": row.max_steps,
        "escalation": row.escalation_key,
        "start": next((n.key for n in row.nodes if n.is_start), None),
        "nodes": [
            {
                "key": node.key,
                "role": node.role.name if node.role else None,
                "instructions": node.instructions,
                "max_visits": node.max_visits,
                "output_key": node.output_key or node.key,
            }
            for node in row.nodes
        ],
        "edges": [
            {
                "from": by_id[edge.from_node_id].key if edge.from_node_id in by_id else None,
                "to": by_id[edge.to_node_id].key if edge.to_node_id in by_id else graph.END,
                "label": edge.label,
                "condition": edge.condition,
                "is_default": edge.is_default,
                "resets": list(edge.resets_json or []),
            }
            for edge in row.edges
        ],
    }


# ----------------------------------------------------------------------- tasks


def task_loaders():
    """Eager-load everything TaskOut and the run snapshot read."""
    from app.models import WorkflowNode

    return (
        selectinload(Task.team).selectinload(Team.members).selectinload(TeamRole.role),
        selectinload(Task.workflow).selectinload(Workflow.nodes).selectinload(WorkflowNode.role),
        selectinload(Task.workflow).selectinload(Workflow.edges),
        selectinload(Task.skill_links),
        selectinload(Task.mcp_links),
    )


async def load_task(session: AsyncSession, task_id: int) -> Task:
    stmt = (
        select(Task)
        .options(*task_loaders())
        .where(Task.id == task_id)
        .execution_options(populate_existing=True)
    )
    task = (await session.execute(stmt)).scalars().first()
    if task is None:
        raise CrudError(f"no task with id {task_id}")
    return task


async def assert_references_exist(session: AsyncSession, payload: TaskIn) -> None:
    if payload.skill_ids:
        rows = (
            await session.execute(select(Skill.id).where(Skill.id.in_(payload.skill_ids)))
        ).scalars().all()
        missing = sorted(set(payload.skill_ids) - set(rows))
        if missing:
            raise CrudError(f"unknown skill ids: {missing}")
    if payload.mcp_server_ids:
        rows = (
            await session.execute(
                select(McpServer.id).where(McpServer.id.in_(payload.mcp_server_ids))
            )
        ).scalars().all()
        missing = sorted(set(payload.mcp_server_ids) - set(rows))
        if missing:
            raise CrudError(f"unknown MCP server ids: {missing}")


def apply_task_flags(row: Task, payload: TaskIn) -> None:
    row.name = payload.name
    row.prompt = payload.prompt
    row.project_path = payload.project_path
    row.model_config_id = payload.model_config_id
    row.trust_project_settings = payload.trust_project_settings
    row.sandbox_bash = payload.sandbox_bash
    row.paranoid_mode = payload.paranoid_mode
    row.network_enabled = payload.network_enabled
    row.approval_timeout_s = payload.approval_timeout_s
    row.max_turns = payload.max_turns
    row.max_budget_usd = payload.max_budget_usd


async def set_task_links(session: AsyncSession, task: Task, payload: TaskIn) -> None:
    """Replace the skill / MCP links.

    Done with DELETE statements rather than by clearing the ORM collections: on a
    freshly flushed Task those collections are unloaded, and touching them would
    trigger a lazy load that async SQLAlchemy cannot perform.
    """
    await session.execute(delete(TaskSkill).where(TaskSkill.task_id == task.id))
    await session.execute(delete(TaskMcpServer).where(TaskMcpServer.task_id == task.id))
    for skill_id in dict.fromkeys(payload.skill_ids):
        session.add(TaskSkill(task_id=task.id, skill_id=skill_id))
    for server_id in dict.fromkeys(payload.mcp_server_ids):
        session.add(TaskMcpServer(task_id=task.id, server_id=server_id))


async def resolve_task_team(session: AsyncSession, payload: TaskIn) -> tuple[int | None, int | None]:
    """Resolve what the task will run: ``(team_id, workflow_id)``, one of them set."""
    if payload.workflow_id is not None:
        workflow = await load_workflow(session, payload.workflow_id)
        if not workflow.nodes:
            raise CrudError(f"workflow {workflow.name!r} has no nodes")
        return None, workflow.id
    if payload.inline_team is not None:
        return await materialise_inline_team(session, payload.inline_team), None
    team = await load_team(session, payload.team_id)
    if not team.members:
        raise CrudError(f"team {team.name!r} has no roles")
    return team.id, None


async def create_task(session: AsyncSession, payload: TaskIn) -> Task:
    payload.check()
    try:
        resolve_project_root(payload.project_path)
    except PolicyError as exc:
        raise CrudError(str(exc)) from exc
    await assert_references_exist(session, payload)

    team_id, workflow_id = await resolve_task_team(session, payload)
    row = Task(team_id=team_id, workflow_id=workflow_id, name=payload.name,
               prompt=payload.prompt, project_path=payload.project_path)
    apply_task_flags(row, payload)
    session.add(row)
    await session.flush()
    await set_task_links(session, row, payload)
    return row


def summarise(row: Any) -> dict[str, Any]:
    """A compact, secret-free description for the chat assistant to read back."""
    if isinstance(row, Role):
        return {"id": row.id, "name": row.name, "description": row.description}
    if isinstance(row, Team):
        return {
            "id": row.id,
            "name": row.name,
            "roles": [
                {"name": m.role.name, "is_lead": m.is_lead}
                for m in sorted(row.members, key=lambda m: m.position)
            ],
        }
    if isinstance(row, McpServer):
        return {
            "id": row.id,
            "name": row.name,
            "transport": row.transport,
            "trusted": row.trusted,
            "env_keys": sorted((row.env_json or {}).keys()),
        }
    if isinstance(row, ModelConfig):
        return {
            "id": row.id,
            "name": row.name,
            "provider": row.provider,
            "model_id": row.model_id,
            "base_url": row.base_url,
            "is_default": row.is_default,
            "api_key_set": bool(row.api_key),
        }
    if isinstance(row, Task):
        return {
            "id": row.id,
            "name": row.name,
            "project_path": row.project_path,
            "team_id": row.team_id,
            "paranoid_mode": row.paranoid_mode,
            "sandbox_bash": row.sandbox_bash,
            "network_enabled": row.network_enabled,
        }
    if isinstance(row, Skill):
        return {"id": row.id, "name": row.name, "status": row.status}
    return {"id": getattr(row, "id", None)}
