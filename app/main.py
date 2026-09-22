"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import dispose_db, init_db
from app.routers import fs, mcps, models, people, runs, skills, tasks
from app.services.runner import manager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("harness")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("database ready at %s", settings.db_path)
    try:
        yield
    finally:
        await manager.shutdown()
        await dispose_db()


def create_app() -> FastAPI:
    app = FastAPI(title="Claude Harness", version="0.1.0", lifespan=lifespan)

    app.include_router(skills.router)
    app.include_router(mcps.router)
    app.include_router(models.router)
    app.include_router(people.roles_router)
    app.include_router(people.teams_router)
    app.include_router(tasks.router)
    app.include_router(runs.router)
    app.include_router(fs.router)

    @app.get("/api/health")
    async def health():
        from claude_agent_sdk import __version__ as sdk_version

        return {
            "ok": True,
            "sdk_version": sdk_version,
            "cli_path": settings.cli_path or "bundled",
            "max_concurrent_runs": settings.max_concurrent_runs,
            "live_runs": manager.live_ids(),
        }

    app.mount(
        "/static", StaticFiles(directory=str(settings.static_dir)), name="static"
    )

    @app.get("/")
    async def index():
        return FileResponse(settings.static_dir / "index.html")

    return app


app = create_app()
