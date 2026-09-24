"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import dispose_db, init_db
from app.routers import chat, fs, mcps, models, people, runs, skills, tasks, workflows
from app.services.chat import manager as chat_manager
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
        await chat_manager.shutdown()
        await manager.shutdown()
        await dispose_db()


class NoCacheStaticFiles(StaticFiles):
    """Serve the frontend without caching.

    This is a local, single-user dev tool whose assets change constantly. A cached
    app.js means edits appear not to take effect and a hard reload becomes a thing
    you have to remember — not worth the microseconds saved on localhost.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Claude Harness", version="0.1.0", lifespan=lifespan)

    app.include_router(skills.router)
    app.include_router(mcps.router)
    app.include_router(models.router)
    app.include_router(people.roles_router)
    app.include_router(people.teams_router)
    app.include_router(workflows.router)
    app.include_router(tasks.router)
    app.include_router(runs.router)
    app.include_router(chat.router)
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
        "/static", NoCacheStaticFiles(directory=str(settings.static_dir)), name="static"
    )

    @app.get("/")
    async def index():
        return FileResponse(
            settings.static_dir / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    return app


app = create_app()
