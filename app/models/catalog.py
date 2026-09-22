"""Catalog entities: skills, MCP servers, model configs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # "installed" (present on disk) or "suggested" (catalog entry, not installed)
    status: Mapped[str] = mapped_column(String(16), default="installed")
    # "discovered" | "uploaded" | "suggested"
    origin: Mapped[str] = mapped_column(String(16), default="discovered")
    install_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(String(16), default="user")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class McpServer(Base, TimestampMixin):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(16), default="stdio")  # stdio|sse|http
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    env_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    scope: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[str] = mapped_column(String(16), default="installed")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Trusted servers may call tools the path guard cannot inspect. See DESIGN 4.7.
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelConfig(Base, TimestampMixin):
    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # anthropic | ollama | vllm | custom
    provider: Mapped[str] = mapped_column(String(16), default="anthropic")
    model_id: Mapped[str] = mapped_column(String(128))
    # For non-anthropic providers this is the Anthropic-compatible gateway URL.
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_env_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
