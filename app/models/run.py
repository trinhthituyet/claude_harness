"""Run history: task_runs, run_events and the permission audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    sdk_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cli_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sdk_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    prompt_snapshot: Mapped[str] = mapped_column(Text, default="")
    project_path_snapshot: Mapped[str] = mapped_column(Text, default="")
    team_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    skills_snapshot_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    mcps_snapshot_json: Mapped[list[str]] = mapped_column(JSON, default=list)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    num_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str] = mapped_column(Text, default="")


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PermissionDecision(Base):
    __tablename__ = "permission_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    layer: Mapped[str] = mapped_column(String(16))  # hook | callback | ui | rule
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_use_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(8))  # allow | deny | ask
    reason: Mapped[str] = mapped_column(Text, default="")
    candidate_paths_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    resolved_paths_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    tool_input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
