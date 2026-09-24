"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ORM = ConfigDict(from_attributes=True)

MASK = "••••••••"


def mask(secret: str | None) -> str | None:
    """Never echo a stored credential back to the client."""
    if not secret:
        return None
    tail = secret[-4:] if len(secret) > 8 else ""
    return f"{MASK}{tail}"


# --------------------------------------------------------------------- skills


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    source_url: str | None = None
    enabled: bool = True


class SkillOut(BaseModel):
    model_config = ORM
    id: int
    name: str
    description: str
    status: str
    origin: str
    install_path: str | None
    scope: str
    source_url: str | None
    metadata_json: dict[str, Any]
    enabled: bool


# ----------------------------------------------------------------------- mcps


class McpIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    transport: Literal["stdio", "sse", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    trusted: bool = False

    @field_validator("command")
    @classmethod
    def _command_required_for_stdio(cls, v, info):
        return v

    def check(self) -> None:
        if self.transport == "stdio" and not self.command:
            raise ValueError("a stdio MCP server needs a command")
        if self.transport in {"sse", "http"} and not self.url:
            raise ValueError(f"a {self.transport} MCP server needs a url")


class McpOut(BaseModel):
    id: int
    name: str
    description: str
    transport: str
    command: str | None
    args: list[str]
    env_keys: list[str]
    url: str | None
    header_keys: list[str]
    status: str
    enabled: bool
    trusted: bool

    @classmethod
    def of(cls, row) -> "McpOut":
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            transport=row.transport,
            command=row.command,
            args=row.args_json or [],
            env_keys=sorted((row.env_json or {}).keys()),
            url=row.url,
            header_keys=sorted((row.headers_json or {}).keys()),
            status=row.status,
            enabled=row.enabled,
            trusted=row.trusted,
        )


# --------------------------------------------------------------------- models


class ModelIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider: Literal["anthropic", "ollama", "vllm", "custom"] = "anthropic"
    model_id: str = Field(min_length=1)
    base_url: str | None = None
    api_key: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)
    is_default: bool = False

    def check(self) -> None:
        if self.provider != "anthropic" and not self.base_url:
            raise ValueError(
                f"provider {self.provider!r} needs base_url: the URL of an "
                "Anthropic-API-compatible gateway (LiteLLM / claude-code-router). "
                "A raw Ollama or vLLM endpoint will not work."
            )


class ModelOut(BaseModel):
    id: int
    name: str
    provider: str
    model_id: str
    base_url: str | None
    api_key_masked: str | None
    extra_env_keys: list[str]
    is_default: bool

    @classmethod
    def of(cls, row) -> "ModelOut":
        return cls(
            id=row.id,
            name=row.name,
            provider=row.provider,
            model_id=row.model_id,
            base_url=row.base_url,
            api_key_masked=mask(row.api_key),
            extra_env_keys=sorted((row.extra_env_json or {}).keys()),
            is_default=row.is_default,
        )


# ---------------------------------------------------------------------- roles


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    system_prompt: str = Field(min_length=1)
    model_config_id: int | None = None


class RoleOut(BaseModel):
    model_config = ORM
    id: int
    name: str
    description: str
    system_prompt: str
    model_config_id: int | None


# ---------------------------------------------------------------------- teams


class TeamMemberIn(BaseModel):
    role_id: int
    is_lead: bool = False


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    members: list[TeamMemberIn] = Field(min_length=1)

    def check(self) -> None:
        if not self.members:
            raise ValueError("a team needs at least one role")
        leads = [m for m in self.members if m.is_lead]
        if len(leads) > 1:
            raise ValueError("a team can have only one lead role")
        ids = [m.role_id for m in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("a role cannot appear twice in the same team")


class TeamMemberOut(BaseModel):
    role_id: int
    role_name: str
    description: str
    is_lead: bool
    position: int


class TeamOut(BaseModel):
    id: int
    name: str
    description: str
    members: list[TeamMemberOut]

    @classmethod
    def of(cls, row) -> "TeamOut":
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            members=[
                TeamMemberOut(
                    role_id=m.role_id,
                    role_name=m.role.name,
                    description=m.role.description,
                    is_lead=m.is_lead,
                    position=m.position,
                )
                for m in sorted(row.members, key=lambda m: m.position)
            ],
        )


# ---------------------------------------------------------------------- tasks


class InlineTeam(BaseModel):
    """Define a team (and optionally brand-new roles) while creating a task."""

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    role_ids: list[int] = Field(default_factory=list)
    new_roles: list[RoleIn] = Field(default_factory=list)
    lead_role_name: str | None = None

    def check(self) -> None:
        if not self.role_ids and not self.new_roles:
            raise ValueError("an inline team needs at least one existing or new role")


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1)
    project_path: str = Field(min_length=1)
    team_id: int | None = None
    inline_team: InlineTeam | None = None
    #: Alternative to a team: run a graph of roles instead of one delegating session.
    workflow_id: int | None = None
    model_config_id: int | None = None
    skill_ids: list[int] = Field(default_factory=list)
    mcp_server_ids: list[int] = Field(default_factory=list)
    trust_project_settings: bool = False
    sandbox_bash: bool = True
    paranoid_mode: bool = False
    network_enabled: bool = False
    approval_timeout_s: int = Field(default=300, ge=10, le=3600)
    max_turns: int | None = Field(default=None, ge=1, le=200)
    max_budget_usd: float | None = Field(default=None, gt=0)

    def check(self) -> None:
        if not self.prompt.strip():
            raise ValueError("a task needs a prompt")
        if not self.project_path.strip():
            raise ValueError("a task needs a project path")
        chosen = [
            name
            for name, value in (
                ("team_id", self.team_id),
                ("inline_team", self.inline_team),
                ("workflow_id", self.workflow_id),
            )
            if value is not None
        ]
        if not chosen:
            raise ValueError(
                "a task needs an existing team, an inline team, or a workflow"
            )
        if len(chosen) > 1:
            raise ValueError(f"pass only one of team_id, inline_team or workflow_id, got {chosen}")
        if self.inline_team is not None:
            self.inline_team.check()


class TaskOut(BaseModel):
    id: int
    name: str
    prompt: str
    project_path: str
    team_id: int | None
    team_name: str
    workflow_id: int | None
    workflow_name: str
    model_config_id: int | None
    skill_ids: list[int]
    mcp_server_ids: list[int]
    trust_project_settings: bool
    sandbox_bash: bool
    paranoid_mode: bool
    network_enabled: bool
    approval_timeout_s: int
    max_turns: int | None
    max_budget_usd: float | None

    @classmethod
    def of(cls, row) -> "TaskOut":
        return cls(
            id=row.id,
            name=row.name,
            prompt=row.prompt,
            project_path=row.project_path,
            team_id=row.team_id,
            team_name=row.team.name if row.team else "",
            workflow_id=row.workflow_id,
            workflow_name=row.workflow.name if row.workflow else "",
            model_config_id=row.model_config_id,
            skill_ids=[link.skill_id for link in row.skill_links],
            mcp_server_ids=[link.server_id for link in row.mcp_links],
            trust_project_settings=row.trust_project_settings,
            sandbox_bash=row.sandbox_bash,
            paranoid_mode=row.paranoid_mode,
            network_enabled=row.network_enabled,
            approval_timeout_s=row.approval_timeout_s,
            max_turns=row.max_turns,
            max_budget_usd=row.max_budget_usd,
        )


# ------------------------------------------------------------------- workflows


class WorkflowNodeIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    role_id: int
    instructions: str = ""
    is_start: bool = False
    max_visits: int = Field(default=3, ge=1, le=20)
    #: What this step's output is filed under for later steps. Defaults to the key.
    output_key: str | None = Field(default=None, max_length=64)
    #: Canvas position. Omitted means "lay it out automatically".
    pos_x: float | None = None
    pos_y: float | None = None


class WorkflowEdgeIn(BaseModel):
    from_key: str = Field(min_length=1)
    #: None or "END" finishes the workflow.
    to_key: str | None = None
    label: str = Field(min_length=1, max_length=64)
    condition: str = ""
    is_default: bool = False
    #: Node keys whose visit budget resets when this edge is taken.
    resets: list[str] = Field(default_factory=list)


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    max_steps: int = Field(default=20, ge=1, le=100)
    #: Where to go when a budget runs out, instead of failing the run.
    escalation_key: str | None = Field(default=None, max_length=64)
    nodes: list[WorkflowNodeIn] = Field(min_length=1)
    edges: list[WorkflowEdgeIn] = Field(default_factory=list)

    def check(self) -> None:
        from app.services import graph

        graph.validate(
            [
                graph.NodeSpec(n.key, n.is_start, n.max_visits, n.output_key)
                for n in self.nodes
            ],
            [
                graph.EdgeSpec(
                    e.from_key, e.to_key, e.label, e.condition, e.is_default,
                    tuple(e.resets),
                )
                for e in self.edges
            ],
            self.escalation_key,
        )


class WorkflowNodeOut(BaseModel):
    id: int
    key: str
    role_id: int
    role_name: str
    instructions: str
    is_start: bool
    max_visits: int
    output_key: str
    pos_x: float | None
    pos_y: float | None


class NodePosition(BaseModel):
    key: str
    x: float
    y: float


class LayoutIn(BaseModel):
    """Positions only: dragging should not rewrite or revalidate the graph."""

    positions: list[NodePosition] = Field(min_length=1)


class WorkflowEdgeOut(BaseModel):
    id: int
    from_key: str
    to_key: str | None
    label: str
    condition: str
    is_default: bool
    resets: list[str]


class WorkflowOut(BaseModel):
    id: int
    name: str
    description: str
    max_steps: int
    escalation_key: str | None
    nodes: list[WorkflowNodeOut]
    edges: list[WorkflowEdgeOut]

    @classmethod
    def of(cls, row) -> "WorkflowOut":
        by_id = {node.id: node for node in row.nodes}
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            max_steps=row.max_steps,
            escalation_key=row.escalation_key,
            nodes=[
                WorkflowNodeOut(
                    id=node.id,
                    key=node.key,
                    role_id=node.role_id,
                    role_name=node.role.name if node.role else "",
                    instructions=node.instructions,
                    is_start=node.is_start,
                    max_visits=node.max_visits,
                    output_key=node.output_key or node.key,
                    pos_x=node.pos_x,
                    pos_y=node.pos_y,
                )
                for node in row.nodes
            ],
            edges=[
                WorkflowEdgeOut(
                    id=edge.id,
                    from_key=by_id[edge.from_node_id].key if edge.from_node_id in by_id else "",
                    to_key=by_id[edge.to_node_id].key if edge.to_node_id in by_id else None,
                    label=edge.label,
                    condition=edge.condition,
                    is_default=edge.is_default,
                    resets=list(edge.resets_json or []),
                )
                for edge in row.edges
            ],
        )


# ----------------------------------------------------------------------- runs


class RunOut(BaseModel):
    model_config = ORM
    id: str
    task_id: int | None
    status: str
    sdk_session_id: str | None
    prompt_snapshot: str
    project_path_snapshot: str
    started_at: datetime
    ended_at: datetime | None
    exit_reason: str | None
    num_turns: int | None
    total_cost_usd: float | None
    duration_ms: int | None
    error_text: str | None
    output_text: str


class EventOut(BaseModel):
    seq: int
    type: str
    ts: datetime
    payload: dict[str, Any]


class DecisionOut(BaseModel):
    ts: datetime
    layer: str
    tool_name: str
    decision: str
    reason: str
    resolved_paths: list[str]


class ApprovalIn(BaseModel):
    approved: bool
    remember: bool = False
    reason: str = ""


# ------------------------------------------------------------------------ chat


class ChatStartIn(BaseModel):
    title: str = "New chat"
    message: str | None = None


class ChatMessageIn(BaseModel):
    text: str = Field(min_length=1)


class ChatConfirmIn(BaseModel):
    approved: bool
    reason: str = ""


class ChatOut(BaseModel):
    model_config = ORM
    id: str
    title: str
    status: str
    total_cost_usd: float | None
    error_text: str | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------------- fs


class PathCheck(BaseModel):
    path: str
    resolved: str | None
    exists: bool
    is_dir: bool
    writable: bool
    ok: bool
    error: str | None = None
