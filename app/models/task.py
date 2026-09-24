"""Tasks and their many-to-many links to skills and MCP servers."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TaskSkill(Base):
    __tablename__ = "task_skills"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[int] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )


class TaskMcpServer(Base):
    __tablename__ = "task_mcp_servers"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True
    )


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    project_path: Mapped[str] = mapped_column(Text)
    # A task runs either a flat team (one session, lead delegates) or a workflow
    # (a graph of roles, one session per step). Exactly one is set.
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True
    )
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=True
    )
    model_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )

    # --- security switches, see docs/DESIGN.md section 4 ---
    trust_project_settings: Mapped[bool] = mapped_column(Boolean, default=False)
    sandbox_bash: Mapped[bool] = mapped_column(Boolean, default=True)
    paranoid_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    network_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_timeout_s: Mapped[int] = mapped_column(Integer, default=300)

    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    team = relationship("Team", lazy="selectin")
    workflow = relationship("Workflow", lazy="selectin")
    skill_links: Mapped[list[TaskSkill]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    mcp_links: Mapped[list[TaskMcpServer]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
