"""Model configs: Anthropic models and local gateways."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ModelConfig
from app.schemas import MASK, ModelIn, ModelOut
from app.services import catalog

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[ModelOut])
async def list_models(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(ModelConfig).order_by(ModelConfig.name))
    ).scalars().all()
    return [ModelOut.of(r) for r in rows]


@router.get("/anthropic-catalog")
async def anthropic_catalog():
    return catalog.ANTHROPIC_MODELS


async def _clear_other_defaults(session: AsyncSession, keep_id: int | None) -> None:
    stmt = update(ModelConfig).values(is_default=False)
    if keep_id is not None:
        stmt = stmt.where(ModelConfig.id != keep_id)
    await session.execute(stmt)


@router.post("", response_model=ModelOut, status_code=201)
async def create_model(payload: ModelIn, session: AsyncSession = Depends(get_session)):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if (
        await session.execute(select(ModelConfig).where(ModelConfig.name == payload.name))
    ).scalars().first():
        raise HTTPException(409, f"a model config named {payload.name!r} already exists")
    row = ModelConfig(
        name=payload.name,
        provider=payload.provider,
        model_id=payload.model_id,
        base_url=payload.base_url,
        api_key=payload.api_key,
        extra_env_json=dict(payload.extra_env),
        is_default=payload.is_default,
    )
    session.add(row)
    await session.flush()
    if payload.is_default:
        await _clear_other_defaults(session, row.id)
    await session.commit()
    return ModelOut.of(row)


@router.put("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int, payload: ModelIn, session: AsyncSession = Depends(get_session)
):
    try:
        payload.check()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = await session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(404, "model config not found")
    row.name = payload.name
    row.provider = payload.provider
    row.model_id = payload.model_id
    row.base_url = payload.base_url
    # A masked key means "unchanged"; an empty string means "clear it".
    if payload.api_key is not None and not payload.api_key.startswith(MASK):
        row.api_key = payload.api_key or None
    row.extra_env_json = dict(payload.extra_env)
    row.is_default = payload.is_default
    if payload.is_default:
        await _clear_other_defaults(session, row.id)
    await session.commit()
    return ModelOut.of(row)


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(404, "model config not found")
    await session.delete(row)
    await session.commit()


@router.post("/{model_id}/test")
async def test_model(model_id: int, session: AsyncSession = Depends(get_session)):
    """One-token round trip through the SDK, reporting the real error on failure."""
    row = await session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(404, "model config not found")

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    from app.config import settings as app_settings
    from app.services.model_resolver import ModelConfigError, resolve, scrubbed_base_env
    from app.services.snapshot import ModelSpec

    spec = ModelSpec(
        name=row.name,
        provider=row.provider,
        model_id=row.model_id,
        base_url=row.base_url,
        api_key=row.api_key,
        extra_env=dict(row.extra_env_json or {}),
    )
    try:
        resolved_model, env = resolve(spec)
    except ModelConfigError as exc:
        return {"ok": False, "detail": str(exc)}

    options = ClaudeAgentOptions(
        model=resolved_model,
        tools=[],
        max_turns=1,
        setting_sources=[],
        system_prompt="Reply with the single word OK.",
        env=scrubbed_base_env() | env,
        cli_path=app_settings.cli_path,
    )
    try:
        async for message in query(prompt="Say OK.", options=options):
            if isinstance(message, ResultMessage):
                if message.is_error:
                    return {"ok": False, "detail": message.result or message.subtype}
                return {"ok": True, "detail": (message.result or "OK").strip()[:200]}
        return {"ok": False, "detail": "no result returned"}
    except Exception as exc:  # noqa: BLE001 - the real error is what the user needs
        hint = ""
        if row.provider != "anthropic":
            hint = (
                " — a raw Ollama/vLLM endpoint will not work here; point base_url at an "
                "Anthropic-API-compatible gateway (LiteLLM or claude-code-router)"
            )
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}{hint}"}
