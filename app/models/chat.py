"""Chat sessions: the conversational configuration assistant."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    # idle | thinking | awaiting_confirmation | closed | failed
    status: Mapped[str] = mapped_column(String(24), default="idle")
    sdk_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatMessage(Base):
    """One entry of the transcript. Doubles as the SSE replay log."""

    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("chat_id", "seq", name="uq_chat_message_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # user | assistant | thinking | tool_use | tool_result |
    # confirmation_request | confirmation_resolved | status | error | result
    type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
