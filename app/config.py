"""Application settings."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _resolve_cli_path() -> str | None:
    """Which ``claude`` binary sessions should run.

    Defaults to the one on PATH rather than the SDK's bundled copy. The user's own
    install is already configured with their authentication, proxy and certificates;
    the bundled binary is not, and when it cannot reach the API it *hangs* rather
    than failing. Set ``HARNESS_CLI_PATH=bundled`` to force the SDK's copy.
    """
    raw = os.environ.get("HARNESS_CLI_PATH")
    if raw:
        return None if raw == "bundled" else raw
    return shutil.which("claude")


def _resolve_setting_sources() -> list[str] | None:
    """Override which filesystem settings layers sessions load.

    Unset (the default) means the harness decides per task: nothing, or ``user`` when
    skills are in play. ``HARNESS_SETTING_SOURCES=user`` forces the user layer for
    every session, which some installations need for authentication; ``none`` forces
    isolation. Never include ``project`` unless you trust the directories you run in —
    a project's settings can pre-approve tools.
    """
    raw = os.environ.get("HARNESS_SETTING_SOURCES")
    if raw is None:
        return None
    if raw.strip().lower() in {"", "none"}:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: _env_path("HARNESS_DB", ROOT_DIR / "data" / "harness.db"))
    static_dir: Path = field(default_factory=lambda: ROOT_DIR / "app" / "static")
    skills_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "skills")
    # None means "let the SDK use its bundled CLI".
    cli_path: str | None = field(default_factory=_resolve_cli_path)
    # Carried into the per-run settings blob when set (or auto-detected from
    # ~/.claude/settings.json), because sessions run with setting_sources=[].
    api_key_helper: str | None = field(
        default_factory=lambda: os.environ.get("HARNESS_API_KEY_HELPER")
    )
    setting_sources: list[str] | None = field(default_factory=_resolve_setting_sources)
    max_concurrent_runs: int = field(default_factory=lambda: _env_int("HARNESS_MAX_RUNS", 3))
    event_buffer_size: int = 2000
    # Environment variables never passed through to a session subprocess.
    env_denylist: tuple[str, ...] = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "HARNESS_DB",
    )

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()
