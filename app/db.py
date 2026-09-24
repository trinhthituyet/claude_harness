"""SQLAlchemy engine/session setup and schema creation."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.base import Base

log = logging.getLogger("harness.db")

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


async def _existing_columns(conn, table: str) -> dict[str, dict]:
    rows = (await conn.execute(text(f"PRAGMA table_info('{table}')"))).mappings().all()
    return {row["name"]: dict(row) for row in rows}


async def _table_exists(conn, table: str) -> bool:
    found = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": table},
        )
    ).scalar()
    return found is not None


async def _add_missing_columns(conn) -> None:
    """Bring existing tables up to the current models.

    ``create_all`` creates missing *tables* but never alters one that already
    exists, so a database from an earlier version silently lacks new columns. Only
    nullable or defaulted columns can be added this way, which is all the schema
    has needed so far.
    """
    from sqlalchemy.schema import CreateColumn

    for table in Base.metadata.sorted_tables:
        if not await _table_exists(conn, table.name):
            continue
        existing = await _existing_columns(conn, table.name)
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable and column.default is None and column.server_default is None:
                log.warning(
                    "cannot add required column %s.%s to an existing table",
                    table.name,
                    column.name,
                )
                continue
            ddl = CreateColumn(column).compile(dialect=conn.dialect).string
            # Inline REFERENCES clauses are not part of CreateColumn; SQLite is
            # relaxed about this and the ORM still enforces the relationship.
            await conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
            log.info("added column %s.%s", table.name, column.name)


async def _relax_task_team_id(conn) -> None:
    """Make ``tasks.team_id`` nullable, for tasks that run a workflow instead.

    SQLite cannot drop a NOT NULL constraint in place, so the table is rebuilt.
    Guarded so it runs once: on a database created by the current models the
    column is already nullable.
    """
    from app.models import Task

    if not await _table_exists(conn, "tasks"):
        return
    columns = await _existing_columns(conn, "tasks")
    team_id = columns.get("team_id")
    if team_id is None or not team_id.get("notnull"):
        return

    log.info("rebuilding tasks to allow workflow-only tasks")
    await conn.execute(text("ALTER TABLE tasks RENAME TO tasks__old"))
    await conn.run_sync(lambda sync_conn: Task.__table__.create(sync_conn))
    shared = [name for name in (await _existing_columns(conn, "tasks")) if name in columns]
    column_list = ", ".join(shared)
    await conn.execute(
        text(f"INSERT INTO tasks ({column_list}) SELECT {column_list} FROM tasks__old")
    )
    await conn.execute(text("DROP TABLE tasks__old"))


async def init_db() -> None:
    """Create the schema, apply SQLite pragmas, and migrate older databases."""
    # Import for the side effect of registering every mapper before create_all.
    from app import models  # noqa: F401

    async with engine().begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
        await _relax_task_team_id(conn)
        await _add_missing_columns(conn)
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        )
        current = (await conn.execute(text("SELECT version FROM schema_version"))).scalar()
        if current is None:
            await conn.execute(text("INSERT INTO schema_version (version) VALUES (2)"))
        else:
            await conn.execute(text("UPDATE schema_version SET version=2"))

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
