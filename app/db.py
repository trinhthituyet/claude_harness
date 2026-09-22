"""SQLAlchemy engine/session setup and schema creation."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.base import Base

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine():
    global _engine, _sessionmaker
    if _engine is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(settings.db_url, future=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def init_db() -> None:
    """Create the schema and apply SQLite pragmas."""
    # Import for the side effect of registering every mapper before create_all.
    from app import models  # noqa: F401

    async with engine().begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        )
        current = (await conn.execute(text("SELECT version FROM schema_version"))).scalar()
        if current is None:
            await conn.execute(text("INSERT INTO schema_version (version) VALUES (1)"))

    # The DB may hold API keys and MCP env vars; keep it owner-only.
    try:
        os.chmod(settings.db_path, 0o600)
    except OSError:
        pass


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with sessionmaker()() as session:
        # Enforced per-connection in SQLite, so set it on every session.
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session
