"""The Chat assistant: describe what you want, it configures the harness for you.

Execution model mirrors the task runner — one ``ClaudeSDKClient`` per chat, kept
alive across turns, with events streamed to the browser over SSE. The differences
are deliberate:

* ``tools=[]`` — the chat session gets **no** built-in tools. It cannot read, write
  or execute anything. Its only capabilities are the in-process tools in
  :mod:`app.services.chat_tools`.
* Every mutating tool is confirmed by the user in the UI before it runs, through the
  same ``can_use_tool`` mechanism the task runner uses for out-of-root writes. A
  declined call never executes and the reason is fed back to the model, so "no, call
  it Reviewer instead" is a normal way to steer it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from sqlalchemy import func, select

from app.config import settings
from app.db import sessionmaker
from app.models import ChatMessage, ChatSession, ModelConfig
from app.services import chat_tools
from app.services.events import EventBus, chat_event_bus
from app.services.model_resolver import resolve, scrubbed_base_env
from app.services.settings_builder import inherited_auth_settings
from app.services.snapshot import ModelSpec

log = logging.getLogger("harness.chat")

CONFIRM_TIMEOUT_S = 300
MAX_TURNS_PER_MESSAGE = 25
#: Starting a session spawns the CLI and authenticates. If that has not happened by
#: now something is wrong with auth or the endpoint, and silence is the worst answer.
CONNECT_TIMEOUT_S = 90
#: Whole-turn ceiling. Must exceed CONFIRM_TIMEOUT_S, since a turn legitimately
#: blocks while waiting for the user to answer a confirmation.
TURN_TIMEOUT_S = 900

SYSTEM_PROMPT = """\
You are the configuration assistant for Claude Harness, a local control panel that
runs Claude Code sessions. The user tells you what they want; you set it up using
the harness tools.

How to work:

- Read the current state before changing it. Prefer `list_*` tools over guessing ids.
- Ask before you act when something material is unspecified: a role with no stated
  behaviour, a team with no obvious lead, a task with no project directory. One
  focused question is better than three, and better than a wrong guess.
- When you do have enough, act. Do not ask permission to use a tool — every change
  is confirmed by the user in the UI before it takes effect, and you will be told if
  they declined and why. If they decline with a correction, apply it and try again.
- Write real content, not placeholders. A role's system prompt should be specific
  enough to change how that role behaves.
- Always call `check_project_path` before `create_task`.
- Never invent an API key, token or secret. Ask the user, or leave the field empty
  and tell them where to fill it in.
- Say plainly what a security switch means before enabling it. `network_enabled`
  lets a session fetch the web, and reads are unconfined, so that combination lets a
  session send readable files out. `paranoid_mode` refuses out-of-root writes instead
  of asking. Turning off `sandbox_bash` leaves shell commands unconfined.
- Deletion is not available to you. If the user wants something removed, point them
  at the relevant panel.

Be concise. Report what you changed, with the ids, and what you would do next.\
"""


@dataclass
class ConfirmAnswer:
    approved: bool
    reason: str = ""


class SessionStartError(RuntimeError):
    """The CLI session never came up.

    A distinct type rather than TimeoutError: since Python 3.11 ``asyncio.TimeoutError``
    *is* ``TimeoutError``, so raising that here would be indistinguishable from the
    whole-turn timeout and the actionable message would be replaced by the generic one.
    """


@dataclass
class PendingConfirmation:
    id: str
    tool_name: str
    tool_input: dict[str, Any]
    deadline: datetime
    future: asyncio.Future[ConfirmAnswer]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_name": self.tool_name.removeprefix(f"mcp__{chat_tools.SERVER_NAME}__"),
            "tool_input": self.tool_input,
            "deadline": self.deadline.isoformat(),
        }


class Chat:
    def __init__(self, chat_id: str, bus: EventBus) -> None:
        self.id = chat_id
        self.bus = bus
        self.pending: dict[str, PendingConfirmation] = {}
        self._client: ClaudeSDKClient | None = None
        self._turn_lock = asyncio.Lock()
        self._known_tools = {chat_tools.qualified(t.name) for t in chat_tools.ALL_TOOLS}
        self._turn: asyncio.Task[None] | None = None
        self._stderr: list[str] = []

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    # --------------------------------------------------------------- guarding

    async def _pre_tool_use(self, payload, tool_use_id, context) -> dict[str, Any]:
        """Defence in depth: refuse anything that is not one of our own tools.

        ``can_use_tool`` below is the confirmation surface, but it is only consulted
        for calls that would prompt. This hook sees every call, so an unexpected tool
        cannot slip through. It vetoes only; it never grants.
        """
        try:
            name = payload.get("tool_name", "")
            if name in self._known_tools:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"{name!r} is not a harness configuration tool; the chat assistant "
                        "has no other tools available"
                    ),
                }
            }
        except BaseException as exc:  # noqa: BLE001 - a raising hook is fail-open
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"chat guard internal error: {exc!r}",
                }
            }

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context):
        if not chat_tools.is_mutating(tool_name):
            return PermissionResultAllow()

        loop = asyncio.get_running_loop()
        request = PendingConfirmation(
            id=str(uuid.uuid4()),
            tool_name=tool_name,
            tool_input=tool_input,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=CONFIRM_TIMEOUT_S),
            future=loop.create_future(),
        )
        self.pending[request.id] = request
        await self._set_status("awaiting_confirmation")
        await self.bus.emit("confirmation_request", request.public())
        try:
            answer = await asyncio.wait_for(request.future, CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.bus.emit(
                "confirmation_resolved", {"id": request.id, "outcome": "timeout"}
            )
            return PermissionResultDeny(message="the user did not answer in time; not applied")
        finally:
            self.pending.pop(request.id, None)
            await self._set_status("thinking")

        outcome = "approved" if answer.approved else "declined"
        await self.bus.emit(
            "confirmation_resolved",
            {"id": request.id, "outcome": outcome, "reason": answer.reason},
        )
        if answer.approved:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=answer.reason or "the user declined this change"
        )

    # ---------------------------------------------------------------- session

    async def _options(self) -> ClaudeAgentOptions:
        model_id = None
        async with sessionmaker()() as session:
            default = (
                await session.execute(select(ModelConfig).where(ModelConfig.is_default.is_(True)))
            ).scalars().first()
            spec = (
                ModelSpec(
                    name=default.name,
                    provider=default.provider,
                    model_id=default.model_id,
                    base_url=default.base_url,
                    api_key=default.api_key,
                    extra_env=dict(default.extra_env_json or {}),
                )
                if default is not None
                else ModelSpec()
            )
        model_id, model_env = resolve(spec)

        # Carry the user's auth settings (never their permissions) into the session,
        # minus anything the model config sets itself. See settings_builder.
        blob = inherited_auth_settings(exclude_env=frozenset(model_env))

        return ClaudeAgentOptions(
            cwd=str(settings.static_dir.parent.parent),
            tools=[],  # no built-in tools: this session cannot touch the filesystem
            mcp_servers={chat_tools.SERVER_NAME: chat_tools.build_server()},
            strict_mcp_config=True,
            permission_mode="default",
            setting_sources=list(settings.setting_sources or []),
            settings=json.dumps(blob) if blob else None,
            can_use_tool=self._can_use_tool,
            hooks={"PreToolUse": [HookMatcher(hooks=[self._pre_tool_use])]},
            system_prompt=SYSTEM_PROMPT,
            model=model_id,
            env=scrubbed_base_env() | model_env,
            max_turns=MAX_TURNS_PER_MESSAGE,
            stderr=self._capture_stderr,
        )

    def _capture_stderr(self, line: str) -> None:
        """Surface subprocess trouble, which is otherwise invisible from the UI."""
        self._stderr.append(line)
        del self._stderr[:-50]
        log.warning("chat %s stderr: %s", self.id, line.rstrip()[:400])

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is not None:
            return self._client
        options = await self._options()
        async with sessionmaker()() as session:
            row = await session.get(ChatSession, self.id)
            previous = row.sdk_session_id if row else None
        if previous:
            # Continue the model's own conversation across a harness restart.
            options.resume = previous
        client = ClaudeSDKClient(options=options)
        try:
            await client.connect()
        except Exception:
            if not previous:
                raise
            log.warning("chat %s could not resume %s; starting fresh", self.id, previous)
            await self.bus.emit(
                "status",
                {"state": "thinking", "note": "previous conversation could not be resumed"},
            )
            options.resume = None
            client = ClaudeSDKClient(options=options)
            await client.connect()
        self._client = client
        return client

    # ------------------------------------------------------------------ turns

    async def send(self, text: str) -> None:
        if self.busy:
            raise RuntimeError("this chat is still working on the previous message")
        await self.bus.emit("user", {"text": text})
        self._turn = asyncio.create_task(self._run_turn(text), name=f"chat:{self.id}")

    async def _run_turn(self, text: str) -> None:
        async with self._turn_lock:
            await self._set_status("thinking")
            try:
                await asyncio.wait_for(self._turn_body(text), TURN_TIMEOUT_S)
                await self._set_status("idle")
            except asyncio.CancelledError:
                await self._set_status("idle")
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                await self._fail_turn(exc)
            finally:
                self.fail_all_pending("turn ended")

    async def _turn_body(self, text: str) -> None:
        """One turn, with each phase logged so a stall is locatable, not silent."""
        log.info("chat %s: turn started, connecting session", self.id)
        try:
            client = await asyncio.wait_for(self._ensure_client(), CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise SessionStartError(
                f"the Claude Code session did not start within {CONNECT_TIMEOUT_S}s. "
                "This usually means authentication or the API endpoint: check that "
                "ANTHROPIC_API_KEY is set in the environment the harness runs in, or "
                "that the default model config's gateway is reachable."
            ) from exc

        log.info("chat %s: session connected, sending prompt", self.id)
        await client.query(text)
        log.info("chat %s: prompt sent, awaiting the model", self.id)

        first = True
        async for message in client.receive_response():
            if first:
                log.info("chat %s: first response received (%s)", self.id, type(message).__name__)
                first = False
            await self._handle(message)
        log.info("chat %s: turn finished", self.id)

    async def _fail_turn(self, exc: BaseException) -> None:
        if isinstance(exc, asyncio.TimeoutError):
            detail = (
                f"the turn did not finish within {TURN_TIMEOUT_S}s and was abandoned. "
                "The server log shows which phase it reached."
            )
        else:
            detail = f"{type(exc).__name__}: {exc}"
        log.exception("chat %s turn failed: %s", self.id, detail)
        if self._stderr:
            detail += "\n" + "".join(self._stderr[-5:])
        # A half-connected client will not recover; drop it so the next message
        # starts a fresh session rather than reusing a broken one.
        self._client = None
        await self.bus.emit("error", {"message": detail})
        await self._set_status("failed", error=detail)

    async def _handle(self, message: Any) -> None:
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                data = message.data or {}
                await self._store_sdk_session(data.get("session_id"))
                await self.bus.emit(
                    "status",
                    {"state": "thinking", "tools": data.get("tools"),
                     "model": data.get("model")},
                )
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    await self.bus.emit("assistant", {"text": block.text})
                elif isinstance(block, ThinkingBlock):
                    await self.bus.emit("thinking", {"text": block.thinking[:2000]})
                elif isinstance(block, ToolUseBlock):
                    await self.bus.emit(
                        "tool_use",
                        {
                            "id": block.id,
                            "name": block.name.removeprefix(
                                f"mcp__{chat_tools.SERVER_NAME}__"
                            ),
                            "input": block.input,
                        },
                    )
        elif isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        await self.bus.emit(
                            "tool_result",
                            {
                                "id": block.tool_use_id,
                                "is_error": bool(block.is_error),
                                "content": _stringify(block.content)[:4000],
                            },
                        )
        elif isinstance(message, ResultMessage):
            await self.bus.emit(
                "result",
                {
                    "subtype": message.subtype,
                    "num_turns": message.num_turns,
                    "total_cost_usd": message.total_cost_usd,
                },
            )
            await self._add_cost(message.total_cost_usd)

    # ------------------------------------------------------------ bookkeeping

    async def _store_sdk_session(self, sdk_session_id: str | None) -> None:
        if not sdk_session_id:
            return
        async with sessionmaker()() as session:
            row = await session.get(ChatSession, self.id)
            if row is not None and row.sdk_session_id != sdk_session_id:
                row.sdk_session_id = sdk_session_id
                await session.commit()

    async def _add_cost(self, cost: float | None) -> None:
        if not cost:
            return
        async with sessionmaker()() as session:
            row = await session.get(ChatSession, self.id)
            if row is not None:
                row.total_cost_usd = (row.total_cost_usd or 0.0) + cost
                await session.commit()

    async def _set_status(self, status: str, error: str | None = None) -> None:
        async with sessionmaker()() as session:
            row = await session.get(ChatSession, self.id)
            if row is not None:
                row.status = status
                if error:
                    row.error_text = error
                await session.commit()
        await self.bus.emit("status", {"state": status})

    # --------------------------------------------------------------- controls

    def confirm(self, request_id: str, answer: ConfirmAnswer) -> bool:
        request = self.pending.get(request_id)
        if request is None or request.future.done():
            return False
        request.future.set_result(answer)
        return True

    def fail_all_pending(self, reason: str) -> None:
        for request in list(self.pending.values()):
            if not request.future.done():
                request.future.set_result(ConfirmAnswer(approved=False, reason=reason))

    def pending_confirmations(self) -> list[dict[str, Any]]:
        return [r.public() for r in self.pending.values()]

    async def close(self) -> None:
        self.fail_all_pending("chat closed")
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self.bus.close()


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class ChatManager:
    def __init__(self) -> None:
        self._chats: dict[str, Chat] = {}

    def get(self, chat_id: str) -> Chat | None:
        return self._chats.get(chat_id)

    async def create(self, title: str = "New chat") -> str:
        chat_id = str(uuid.uuid4())
        async with sessionmaker()() as session:
            session.add(ChatSession(id=chat_id, title=title))
            await session.commit()
        self._chats[chat_id] = Chat(chat_id, chat_event_bus(chat_id))
        return chat_id

    async def attach(self, chat_id: str) -> Chat:
        """Return the live chat, reviving one that exists only in the database.

        A chat can outlive the process that was running it — a restart (``--reload``
        in development is the common case) drops every in-memory turn. The row is
        then left claiming to be mid-turn forever, and any confirmation it was
        waiting on is gone. Reviving it resets that state and says so in the
        transcript, rather than leaving the UI watching a chat that will never speak.
        """
        existing = self._chats.get(chat_id)
        if existing is not None:
            return existing
        interrupted = False
        async with sessionmaker()() as session:
            row = await session.get(ChatSession, chat_id)
            if row is None:
                raise KeyError(chat_id)
            last_seq = (
                await session.execute(
                    select(func.max(ChatMessage.seq)).where(ChatMessage.chat_id == chat_id)
                )
            ).scalar() or 0
            if row.status in {"thinking", "awaiting_confirmation"}:
                interrupted = True
            if row.status != "idle":
                row.status = "idle"
                await session.commit()
        chat = Chat(chat_id, chat_event_bus(chat_id, start_seq=last_seq))
        self._chats[chat_id] = chat
        if interrupted:
            await chat.bus.emit(
                "status",
                {
                    "state": "idle",
                    "note": "the previous turn was interrupted when the server restarted; "
                            "send your message again",
                },
            )
        return chat

    def pending_confirmations(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for chat_id, chat in self._chats.items():
            for request in chat.pending_confirmations():
                out.append({"chat_id": chat_id, **request})
        return out

    async def close(self, chat_id: str) -> None:
        chat = self._chats.pop(chat_id, None)
        if chat is not None:
            await chat.close()
        async with sessionmaker()() as session:
            row = await session.get(ChatSession, chat_id)
            if row is not None:
                row.status = "closed"
                await session.commit()

    async def shutdown(self) -> None:
        for chat_id in list(self._chats):
            chat = self._chats.pop(chat_id)
            try:
                await chat.close()
            except Exception:  # noqa: BLE001
                pass


manager = ChatManager()
