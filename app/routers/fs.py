"""Filesystem helpers for the project-path picker."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.schemas import PathCheck
from app.security.policy import PolicyError, resolve_project_root

router = APIRouter(prefix="/api/fs", tags=["fs"])


@router.get("/validate", response_model=PathCheck)
async def validate(path: str):
    """Is this a usable project root? Drives the green/red state on the task form."""
    expanded = Path(path).expanduser()
    exists = expanded.exists()
    is_dir = expanded.is_dir()
    try:
        resolved = resolve_project_root(path)
    except PolicyError as exc:
        return PathCheck(
            path=path,
            resolved=str(Path(os.path.realpath(expanded))) if exists else None,
            exists=exists,
            is_dir=is_dir,
            writable=False,
            ok=False,
            error=str(exc),
        )
    return PathCheck(
        path=path,
        resolved=str(resolved),
        exists=True,
        is_dir=True,
        writable=os.access(resolved, os.W_OK),
        ok=True,
    )


@router.get("/ls")
async def ls(path: str = "~"):
    """List subdirectories, for click-through navigation."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise HTTPException(422, "path must be absolute")
    if not target.is_dir():
        raise HTTPException(404, f"not a directory: {target}")
    try:
        entries = sorted(
            (e for e in target.iterdir() if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower(),
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "dirs": [{"name": e.name, "path": str(e)} for e in entries[:500]],
    }
