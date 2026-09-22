"""Diagnose a hanging chat, in the environment the harness actually runs in.

Run this from the same terminal you start uvicorn from:

    .venv/bin/python scripts/doctor.py

It checks, in order, the things that can make a session hang instead of answering,
printing timings and any subprocess stderr as it goes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STEP_TIMEOUT_S = 60


def mask(value: str) -> str:
    return f"{value[:12]}…{value[-4:]}" if len(value) > 20 else value


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_environment() -> None:
    section("environment")
    interesting = sorted(
        k for k in os.environ
        if k.startswith(("ANTHROPIC_", "CLAUDE_", "HARNESS_", "APPLE_CLAUDE"))
        or k.lower() in {"http_proxy", "https_proxy", "no_proxy"}
    )
    if not interesting:
        print("  (no ANTHROPIC_/CLAUDE_/proxy variables set)")
    for key in interesting:
        print(f"  {key}={mask(os.environ[key])}")

    print(f"  claude on PATH: {shutil.which('claude') or 'NO'}")

    from app.config import settings
    from app.services.settings_builder import resolve_api_key_helper

    print(f"  sessions will use: {settings.cli_path or 'the SDK bundled CLI'}")
    helper = resolve_api_key_helper()
    print(f"  apiKeyHelper: {helper or '(none found)'}")
    if helper:
        script = helper.split()[0]
        print(f"    exists: {Path(script).exists()}  executable: {os.access(script, os.X_OK)}")
    if not helper and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  !! no apiKeyHelper and no ANTHROPIC_API_KEY: sessions cannot authenticate")


async def check_api_key_helper() -> None:
    section("api key helper")
    from app.services.settings_builder import resolve_api_key_helper

    helper = resolve_api_key_helper()
    if not helper:
        print("  skipped (none configured)")
        return
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_shell(
            helper, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    except asyncio.TimeoutError:
        proc.kill()
        print(f"  *** the helper did not return within 30s — THIS is the hang.")
        print("      A session cannot start until it produces a token.")
        return
    elapsed = time.time() - t0
    token = out.decode().strip()
    print(f"  exit={proc.returncode} in {elapsed:.1f}s, token={'yes' if token else 'EMPTY'}"
          f" ({len(token)} chars)")
    if err.strip():
        print(f"  stderr: {err.decode().strip()[:300]}")
    if proc.returncode != 0 or not token:
        print("  !! the helper did not produce a token: sessions will fail to authenticate")


async def timed(label: str, coro) -> bool:
    t0 = time.time()
    try:
        await asyncio.wait_for(coro, STEP_TIMEOUT_S)
        print(f"  {label}: OK in {time.time() - t0:.1f}s")
        return True
    except asyncio.TimeoutError:
        print(f"  {label}: *** STILL HANGING after {STEP_TIMEOUT_S}s ***")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  {label}: FAILED after {time.time() - t0:.1f}s — {type(exc).__name__}: {exc}")
        return False


async def check_plain_session(setting_sources: list[str] | None = None) -> bool:
    label = (
        "a minimal session (no tools, no MCP)"
        if setting_sources is None
        else f"the same session with setting_sources={setting_sources}"
    )
    section(label)
    import json

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    from app.config import settings
    from app.security.policy import RunPolicy
    from app.services.model_resolver import scrubbed_base_env
    from app.services.settings_builder import build_settings

    blob = build_settings(RunPolicy(root=ROOT))
    blob.pop("permissions", None)
    print(f"    carrying over: {sorted(blob.keys())}"
          f" (env: {sorted((blob.get('env') or {}).keys())[:4]}…)")

    async def run():
        async for message in query(
            prompt="Say OK.",
            options=ClaudeAgentOptions(
                tools=[], max_turns=1,
                setting_sources=setting_sources if setting_sources is not None else [],
                system_prompt="Reply with OK.",
                env=scrubbed_base_env(),
                settings=json.dumps(blob) if blob else None,
                cli_path=settings.cli_path,
                stderr=lambda line: print(f"    stderr: {line.rstrip()[:200]}"),
            ),
        ):
            if isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(f"the model returned an error: {message.result}")
                print(f"    model replied: {str(message.result)[:80]!r}")

    return await timed("session", run())


async def check_chat_session() -> bool:
    section("a chat session (in-process MCP tools, exactly as the Chat panel runs it)")
    import tempfile

    os.environ["HARNESS_DB"] = str(Path(tempfile.mkdtemp(prefix="harness-doctor-")) / "d.db")
    import importlib

    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    await app.db.init_db()

    from app.services.chat import manager

    chat_id = await manager.create("doctor")
    chat = await manager.attach(chat_id)
    queue = chat.bus.subscribe()

    async def run():
        await chat.send("List the roles.")
        while True:
            event = await queue.get()
            payload = str(event.get("payload"))[:90]
            print(f"    [{event['type']}] {payload}")
            if event["type"] == "status" and event["payload"].get("state") in {"idle", "failed"}:
                return

    ok = await timed("chat session", run())
    await manager.shutdown()
    await app.db.dispose_db()
    return ok


async def main() -> int:
    print("Claude Harness doctor")
    print(f"  python {sys.version.split()[0]}  cwd {Path.cwd()}")
    check_environment()
    await check_api_key_helper()

    plain = await check_plain_session()
    with_user_settings = False
    if not plain:
        # The isolated configuration failed. Some installations keep credentials in
        # ~/.claude/settings.json beyond the pieces we copy across; try loading it.
        with_user_settings = await check_plain_session(["user"])

    chat = await check_chat_session() if plain else False

    section("verdict")
    if plain and chat:
        print("  Both sessions completed. The harness can talk to the model from this")
        print("  terminal, so a hang in the browser is not auth or networking — capture")
        print("  the last 'harness.chat:' line from the uvicorn log when it stalls.")
        return 0

    if with_user_settings:
        print("  The isolated session failed, but loading your user settings fixed it.")
        print("  Start the harness with that layer enabled:")
        print("    HARNESS_SETTING_SOURCES=user \\")
        print("      .venv/bin/python -m uvicorn app.main:app --port 8000")
        print("  Trade-off: allow rules in ~/.claude/settings.json then apply to")
        print("  sessions too. That is your own file rather than a project's, but real.")
        return 1

    if not plain:
        from app.config import settings

        print("  No session could reach the model, so the Chat panel cannot either.")
        print("  This is authentication or network reachability, below the harness.")
        print(f"  Sessions used: {settings.cli_path or 'the SDK bundled CLI'}")
        if settings.cli_path:
            print("  Compare against the same binary by hand:")
            print(f"    {settings.cli_path} -p 'say OK'")
            print("  If that works and this does not, send me both outputs.")
        else:
            print("  The bundled CLI has none of your own auth or proxy config. Use yours:")
            print(f"    HARNESS_CLI_PATH={shutil.which('claude') or '/path/to/claude'} \\")
            print("      .venv/bin/python -m uvicorn app.main:app --port 8000")
        return 1

    print("  The minimal session worked but the chat session did not — that points at")
    print("  the in-process MCP tools rather than auth. Send me this output.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
