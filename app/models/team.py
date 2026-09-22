"""Roles, teams and their association."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text)
    model_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )

    memberships: Mapped[list["TeamRole"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class Team(Base, TimestampMixin):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    members: Mapped[list["TeamRole"]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
        order_by="TeamRole.position",
        lazy="selectin",
    )


class TeamRole(Base):
    __tablename__ = "team_roles"
    __table_args__ = (UniqueConstraint("team_id", "role_id", name="uq_team_role"),)

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_lead: Mapped[bool] = mapped_column(Boolean, default=False)

    team: Mapped[Team] = relationship(back_populates="members")
    role: Mapped[Role] = relationship(back_populates="memberships", lazy="selectin")
