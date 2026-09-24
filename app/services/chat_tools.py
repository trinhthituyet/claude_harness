"""The chat assistant's toolset: an in-process MCP server over the harness's own API.

Two things make this safe enough to let a model drive:

* The chat session runs with ``tools=[]``, so it has **no** built-in tools at all —
  no Read, no Write, no Bash. Verified against the SDK: the session's advertised
  tool list contains only the ones defined here.
* Every mutating tool is listed in :data:`MUTATING_TOOLS` and is gated by a
  confirmation prompt in the UI before its handler runs. A declined call never
  executes; the reason is handed back to the model, which can then adjust.

Read-only tools are not gated, so browsing the current configuration is fluent.
Deletion is deliberately not exposed: it is destructive and irreversible, and the
management panels already offer it behind an explicit confirm.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Awaitable, Callable

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from sqlalchemy import select

from app.db import sessionmaker
from app.models import McpServer, ModelConfig, Role, Skill, Task, Team, TeamRole, Workflow
from app.schemas import (
    McpIn,
    NodeInput,
    ModelIn,
    RoleIn,
    TaskIn,
    TeamIn,
    TeamMemberIn,
    WorkflowEdgeIn,
    WorkflowIn,
    WorkflowNodeIn,
)
from app.security.policy import PolicyError, resolve_project_root
from app.services import crud
from app.services.catalog import ANTHROPIC_MODELS

SERVER_NAME = "harness"

#: The only tools that run without asking the user. Gating is expressed as an
#: allowlist of read-only tools rather than a denylist of mutating ones, so a tool
#: added later without being classified is confirmed by default instead of slipping
#: through unconfirmed.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "list_roles",
        "get_role",
        "list_teams",
        "list_mcp_servers",
        "list_model_configs",
        "list_skills",
        "list_tasks",
        "list_workflows",
        "check_project_path",
    }
)

READ_ONLY = ToolAnnotations(readOnlyHint=True)


def qualified(name: str) -> str:
    return f"mcp__{SERVER_NAME}__{name}"


def is_mutating(qualified_name: str) -> bool:
    """True when a call must be confirmed by the user before it runs."""
    prefix = f"mcp__{SERVER_NAME}__"
    if not qualified_name.startswith(prefix):
        # Not one of ours at all: the chat session has no other tools, so this should
        # be unreachable — treat it as mutating rather than assume it is harmless.
        return True
    return qualified_name[len(prefix) :] not in READ_ONLY_TOOLS


def ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def fail(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _handler(fn: Callable[..., Awaitable[dict[str, Any]]]):
    """Run a tool body in its own session, turning CrudError into a tool error."""

    async def wrapper(args: dict[str, Any]) -> dict[str, Any]:
        try:
            async with sessionmaker()() as session:
                result = await fn(session, args)
                await session.commit()
                return result
        except crud.CrudError as exc:
            return fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - the model should see what went wrong
            return fail(f"{type(exc).__name__}: {exc}")

    return wrapper


# ------------------------------------------------------------------ read tools


@tool("list_roles", "List the configured roles.", {}, annotations=READ_ONLY)
@_handler
async def list_roles(session, args):
    rows = (await session.execute(select(Role).order_by(Role.name))).scalars().all()
    return ok([crud.summarise(r) for r in rows])


@tool("get_role", "Get one role including its full system prompt.", {"role_id": int},
      annotations=READ_ONLY)
@_handler
async def get_role(session, args):
    row = await session.get(Role, int(args["role_id"]))
    if row is None:
        return fail(f"no role with id {args['role_id']}")
    return ok({**crud.summarise(row), "system_prompt": row.system_prompt})


@tool("list_teams", "List the configured teams and their roles.", {}, annotations=READ_ONLY)
@_handler
async def list_teams(session, args):
    from sqlalchemy.orm import selectinload

    rows = (
        await session.execute(
            select(Team).options(selectinload(Team.members).selectinload(TeamRole.role))
        )
    ).scalars().all()
    return ok([crud.summarise(r) for r in rows])


@tool("list_mcp_servers", "List the configured MCP servers.", {}, annotations=READ_ONLY)
@_handler
async def list_mcp_servers(session, args):
    rows = (await session.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
    return ok([crud.summarise(r) for r in rows])


@tool("list_model_configs", "List the configured model backends.", {}, annotations=READ_ONLY)
@_handler
async def list_model_configs(session, args):
    rows = (
        await session.execute(select(ModelConfig).order_by(ModelConfig.name))
    ).scalars().all()
    return ok({
        "configured": [crud.summarise(r) for r in rows],
        "known_anthropic_models": ANTHROPIC_MODELS,
    })


@tool("list_skills", "List skills known to the harness.", {}, annotations=READ_ONLY)
@_handler
async def list_skills(session, args):
    rows = (await session.execute(select(Skill).order_by(Skill.name))).scalars().all()
    return ok([crud.summarise(r) for r in rows])


@tool("list_tasks", "List the configured tasks.", {}, annotations=READ_ONLY)
@_handler
async def list_tasks(session, args):
    rows = (await session.execute(select(Task).order_by(Task.name))).scalars().all()
    return ok([crud.summarise(r) for r in rows])


@tool(
    "check_project_path",
    "Check whether a filesystem path can be used as a task's project directory. "
    "Always call this before creating a task.",
    {"path": Annotated[str, "Absolute path to check"]},
    annotations=READ_ONLY,
)
@_handler
async def check_project_path(session, args):
    try:
        resolved = resolve_project_root(args["path"])
    except PolicyError as exc:
        return ok({"ok": False, "error": str(exc)})
    return ok({"ok": True, "resolved": str(resolved)})


# ----------------------------------------------------------------- write tools


@tool(
    "create_role",
    "Create a new role. Ask the user for a description and system prompt first if "
    "they have not given you enough to write a good one.",
    {
        "name": Annotated[str, "Short role name, e.g. 'Software Architect'"],
        "description": Annotated[str, "One line; shown to the lead role when delegating"],
        "system_prompt": Annotated[str, "The instructions that define this role's behaviour"],
    },
)
@_handler
async def create_role(session, args):
    row = await crud.create_role(
        session,
        RoleIn(
            name=args["name"],
            description=args.get("description", ""),
            system_prompt=args["system_prompt"],
        ),
    )
    return ok({"created": "role", **crud.summarise(row)})


@tool(
    "update_role",
    "Replace an existing role's name, description and system prompt.",
    {
        "role_id": int,
        "name": str,
        "description": str,
        "system_prompt": str,
    },
)
@_handler
async def update_role(session, args):
    row = await crud.update_role(
        session,
        int(args["role_id"]),
        RoleIn(
            name=args["name"],
            description=args.get("description", ""),
            system_prompt=args["system_prompt"],
        ),
    )
    return ok({"updated": "role", **crud.summarise(row)})


@tool(
    "create_team",
    "Create a team from existing roles. Exactly one role is the lead; it drives the "
    "session and delegates to the others.",
    {
        "name": str,
        "description": str,
        "role_ids": Annotated[list[int], "Ids of the roles to include"],
        "lead_role_id": Annotated[int, "Which of those roles leads"],
    },
)
@_handler
async def create_team(session, args):
    role_ids = [int(r) for r in args["role_ids"]]
    lead = int(args["lead_role_id"])
    if lead not in role_ids:
        return fail(f"lead_role_id {lead} must be one of role_ids {role_ids}")
    row = await crud.create_team(
        session,
        TeamIn(
            name=args["name"],
            description=args.get("description", ""),
            members=[TeamMemberIn(role_id=rid, is_lead=rid == lead) for rid in role_ids],
        ),
    )
    team = await crud.load_team(session, row.id)
    return ok({"created": "team", **crud.summarise(team)})


@tool(
    "add_mcp_server",
    "Register an MCP server. For stdio give command and args; for sse/http give url. "
    "Only set trusted=true if the user explicitly accepts that the server's tools "
    "bypass the project path boundary.",
    {
        "name": str,
        "description": str,
        "transport": Annotated[str, "one of: stdio, sse, http"],
        "command": Annotated[str, "executable, for stdio transport"],
        "args": Annotated[list[str], "arguments, for stdio transport"],
        "url": Annotated[str, "endpoint, for sse or http transport"],
        "trusted": Annotated[bool, "whether its tools may bypass the path boundary"],
    },
)
@_handler
async def add_mcp_server(session, args):
    transport = (args.get("transport") or "stdio").lower()
    if transport not in {"stdio", "sse", "http"}:
        return fail(f"transport must be stdio, sse or http, not {transport!r}")
    row = await crud.create_mcp_server(
        session,
        McpIn(
            name=args["name"],
            description=args.get("description", ""),
            transport=transport,
            command=args.get("command") or None,
            args=[str(a) for a in (args.get("args") or [])],
            url=args.get("url") or None,
            trusted=bool(args.get("trusted", False)),
        ),
    )
    return ok({"created": "mcp_server", **crud.summarise(row)})


@tool(
    "add_model_config",
    "Add a model backend. Anthropic models need only a model id. A local vLLM or "
    "Ollama model needs base_url pointing at an Anthropic-API-compatible gateway "
    "(LiteLLM or claude-code-router) — a raw Ollama/vLLM endpoint will not work. "
    "Never invent an API key: ask the user, or leave it empty.",
    {
        "name": str,
        "provider": Annotated[str, "one of: anthropic, ollama, vllm, custom"],
        "model_id": Annotated[str, "e.g. claude-sonnet-5, or the gateway's model name"],
        "base_url": Annotated[str, "gateway URL, required for non-anthropic providers"],
        "is_default": bool,
    },
)
@_handler
async def add_model_config(session, args):
    provider = (args.get("provider") or "anthropic").lower()
    if provider not in {"anthropic", "ollama", "vllm", "custom"}:
        return fail(f"unknown provider {provider!r}")
    row = await crud.create_model_config(
        session,
        ModelIn(
            name=args["name"],
            provider=provider,
            model_id=args["model_id"],
            base_url=args.get("base_url") or None,
            is_default=bool(args.get("is_default", False)),
        ),
    )
    return ok({"created": "model_config", **crud.summarise(row)})


@tool(
    "create_task",
    "Create a task. Check the project path first with check_project_path. Leave the "
    "security switches at their defaults unless the user asks otherwise, and say what "
    "enabling one means before you do it.",
    {
        "name": str,
        "prompt": Annotated[str, "The instruction the session will run"],
        "project_path": Annotated[str, "Absolute directory the task operates on"],
        "team_id": Annotated[int, "0 or omitted when using workflow_id"],
        "workflow_id": Annotated[int, "run a workflow graph instead of a flat team"],
        "model_config_id": Annotated[int, "0 or omitted means the default model config"],
        "skill_ids": list[int],
        "mcp_server_ids": list[int],
        "sandbox_bash": Annotated[bool, "keep true unless the user opts out"],
        "paranoid_mode": Annotated[bool, "denies out-of-root writes instead of asking"],
        "network_enabled": Annotated[bool, "allows WebFetch and WebSearch"],
    },
)
@_handler
async def create_task(session, args):
    model_config_id = args.get("model_config_id") or None
    row = await crud.create_task(
        session,
        TaskIn(
            name=args["name"],
            prompt=args["prompt"],
            project_path=args["project_path"],
            team_id=int(args["team_id"]) if args.get("team_id") else None,
            workflow_id=int(args["workflow_id"]) if args.get("workflow_id") else None,
            model_config_id=int(model_config_id) if model_config_id else None,
            skill_ids=[int(s) for s in (args.get("skill_ids") or [])],
            mcp_server_ids=[int(s) for s in (args.get("mcp_server_ids") or [])],
            sandbox_bash=bool(args.get("sandbox_bash", True)),
            paranoid_mode=bool(args.get("paranoid_mode", False)),
            network_enabled=bool(args.get("network_enabled", False)),
        ),
    )
    return ok({"created": "task", **crud.summarise(row)})


@tool(
    "run_task",
    "Start a session for an existing task. Returns a run id the user can open in the "
    "Runs panel.",
    {"task_id": int},
)
@_handler
async def run_task(session, args):
    from app.services.runner import manager

    task = await crud.load_task(session, int(args["task_id"]))
    run_id = await manager.start(session, task)
    return ok({"started": "run", "run_id": run_id, "task": crud.summarise(task)})



@tool("list_workflows", "List the configured workflows and their graphs.", {},
      annotations=READ_ONLY)
@_handler
async def list_workflows(session, args):
    from sqlalchemy.orm import selectinload

    from app.models import WorkflowNode

    rows = (
        await session.execute(
            select(Workflow).options(
                selectinload(Workflow.nodes).selectinload(WorkflowNode.role),
                selectinload(Workflow.edges),
            )
        )
    ).scalars().all()
    return ok([crud.summarise_workflow(r) for r in rows])


@tool(
    "create_workflow",
    "Create a workflow: a graph of roles describing how the team works. Each node is one "
    "role doing a step; each edge is a transition. An edge whose to_key is null or 'END' "
    "finishes the workflow, and an edge pointing back to an earlier node makes a loop.\n"
    "A node with SEVERAL UNCONDITIONAL edges fans out: every arm runs in parallel. "
    "Several edges converging on one node join there, and it runs once after they all "
    "finish. A node whose edges carry CONDITIONS branches instead, and the router may "
    "select more than one — use that to send work back to just the roles with problems. "
    "Do not mix unconditional and conditional edges out of one node; that is rejected.",
    {
        "name": str,
        "description": str,
        "max_steps": Annotated[int, "Ceiling on supersteps for one run; 20 is sensible"],
        "escalation_key": Annotated[
            str,
            "Node to hand over to when a budget runs out, instead of failing. Optional.",
        ],
        "nodes": Annotated[
            list[dict],
            "Each: {key, role_id, instructions, is_start, max_visits, output_key, "
            "output_schema, inputs}. Exactly one start. output_key names this step's result for "
            "later steps (e.g. arch_doc, design_doc, code) and must be unique. "
            "output_schema is an optional JSON Schema ({type: object, properties: {...}}) "
            "the step's result must match — use it when a later step or a branch needs to "
            "read specific fields rather than prose, e.g. "
            "{approved: boolean, issues: string[]}. inputs is an optional list of "
            "{from, path, as} selecting which earlier results this step is shown — "
            "e.g. {from: 'review_notes', path: 'issues', as: 'my_notes'} hands a role "
            "just the notes addressed to it. Omit inputs to show everything.",
        ],
        "edges": Annotated[
            list[dict],
            "Each: {from_key, to_key (null for END), label, condition, expression, "
            "is_default, resets}. Prefer `expression` over `condition` when the source "
            "step has an output_schema: it is a deterministic test over that JSON, costs "
            "nothing and always decides the same way. Syntax: `issues contains "
            "architecture`, `approved is false`, `score > 5`, `notes is empty`, joined "
            "with and/or/not; a dotted name reads another artifact "
            "(`review_notes.approved`). Use `condition` (words, judged by a model) only "
            "for genuinely judgemental branches. resets lists node keys whose visit "
            "budget starts again when this edge is taken.",
        ],
    },
)
@_handler
async def create_workflow(session, args):
    try:
        payload = WorkflowIn(
            name=args["name"],
            description=args.get("description", ""),
            max_steps=int(args.get("max_steps") or 20),
            escalation_key=(str(args["escalation_key"]).strip().lower() or None)
            if args.get("escalation_key")
            else None,
            nodes=[
                WorkflowNodeIn(
                    key=str(n["key"]).strip().lower(),
                    role_id=int(n["role_id"]),
                    instructions=str(n.get("instructions", "")),
                    is_start=bool(n.get("is_start", False)),
                    max_visits=int(n.get("max_visits") or 3),
                    output_key=(str(n["output_key"]).strip().lower()
                                if n.get("output_key") else None),
                    output_schema=(
                        n["output_schema"]
                        if isinstance(n.get("output_schema"), dict) and n["output_schema"]
                        else None
                    ),
                    inputs=[
                        NodeInput(**{
                            "from": str(i.get("from", "")),
                            "path": str(i.get("path", "")),
                            "as": str(i.get("as", "")),
                        })
                        for i in (n.get("inputs") or [])
                        if isinstance(i, dict) and i.get("from")
                    ],
                )
                for n in args["nodes"]
            ],
            edges=[
                WorkflowEdgeIn(
                    from_key=str(e["from_key"]).strip().lower(),
                    to_key=(
                        None
                        if e.get("to_key") in (None, "", "END", "end")
                        else str(e["to_key"]).strip().lower()
                    ),
                    label=str(e["label"]).strip().lower(),
                    condition=str(e.get("condition", "")),
                    expression=str(e.get("expression", "")),
                    is_default=bool(e.get("is_default", False)),
                    resets=[str(r).strip().lower() for r in (e.get("resets") or [])],
                )
                for e in (args.get("edges") or [])
            ],
        )
    except (KeyError, TypeError, ValueError) as exc:
        return fail(f"could not read the graph: {exc}")

    row = await crud.create_workflow(session, payload)
    loaded = await crud.load_workflow(session, row.id)
    return ok({"created": "workflow", **crud.summarise_workflow(loaded)})


ALL_TOOLS = [
    list_roles,
    get_role,
    list_teams,
    list_mcp_servers,
    list_model_configs,
    list_skills,
    list_tasks,
    list_workflows,
    check_project_path,
    create_role,
    update_role,
    create_team,
    add_mcp_server,
    add_model_config,
    create_task,
    create_workflow,
    run_task,
]


def build_server():
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=ALL_TOOLS)


#: Derived for documentation and tests: everything not on the read-only allowlist.
MUTATING_TOOLS: frozenset[str] = frozenset(
    t.name for t in ALL_TOOLS if t.name not in READ_ONLY_TOOLS
)
