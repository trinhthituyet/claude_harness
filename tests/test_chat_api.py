"""Chat API and the confirmation gate.

The gate tests drive ``Chat._can_use_tool`` directly, which is exactly what the SDK
calls — no model and no network involved.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.services import chat_tools
from app.services.chat import Chat, ConfirmAnswer, manager
from app.services.events import chat_event_bus


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_DB", str(tmp_path / "chat_api.db"))
    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    import app.main

    importlib.reload(app.main)

    application = app.main.create_app()
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as http:
            yield http
    await manager.shutdown()


class FakeContext:
    tool_use_id = "t1"
    agent_id = None
    blocked_path = None
    suggestions: list = []


@pytest.fixture
async def chat(client):
    """A live Chat backed by a real row, with no SDK client attached."""
    chat_id = await manager.create("gate test")
    instance = manager.get(chat_id)
    assert instance is not None
    return instance


# ------------------------------------------------------------------- the gate


async def test_read_only_tool_needs_no_confirmation(chat):
    result = await chat._can_use_tool(chat_tools.qualified("list_roles"), {}, FakeContext())
    assert result.behavior == "allow"
    assert not chat.pending


async def test_mutating_tool_waits_for_the_user(chat):
    task = asyncio.create_task(
        chat._can_use_tool(
            chat_tools.qualified("create_role"),
            {"name": "Tester", "description": "", "system_prompt": "test things"},
            FakeContext(),
        )
    )
    await asyncio.sleep(0.05)
    assert len(chat.pending) == 1
    request = chat.pending_confirmations()[0]
    # The UI shows the unqualified tool name and the exact arguments.
    assert request["tool_name"] == "create_role"
    assert request["tool_input"]["name"] == "Tester"

    assert chat.confirm(request["id"], ConfirmAnswer(approved=True))
    assert (await task).behavior == "allow"
    assert not chat.pending


async def test_decline_reason_is_handed_back_to_the_model(chat):
    task = asyncio.create_task(
        chat._can_use_tool(
            chat_tools.qualified("create_role"), {"name": "Tester"}, FakeContext()
        )
    )
    await asyncio.sleep(0.05)
    request_id = next(iter(chat.pending))
    chat.confirm(request_id, ConfirmAnswer(approved=False, reason="call it Reviewer instead"))
    result = await task
    assert result.behavior == "deny"
    # This message is what steers the model's next attempt.
    assert result.message == "call it Reviewer instead"


async def test_confirmation_timeout_denies(chat, monkeypatch):
    monkeypatch.setattr("app.services.chat.CONFIRM_TIMEOUT_S", 0.2)
    result = await chat._can_use_tool(
        chat_tools.qualified("create_task"), {"name": "x"}, FakeContext()
    )
    assert result.behavior == "deny"
    assert "did not answer in time" in result.message


async def test_pending_confirmations_are_failed_when_the_turn_ends(chat):
    task = asyncio.create_task(
        chat._can_use_tool(chat_tools.qualified("create_role"), {}, FakeContext())
    )
    await asyncio.sleep(0.05)
    chat.fail_all_pending("turn ended")
    assert (await task).behavior == "deny"


async def test_confirming_twice_is_rejected(chat):
    task = asyncio.create_task(
        chat._can_use_tool(chat_tools.qualified("create_role"), {}, FakeContext())
    )
    await asyncio.sleep(0.05)
    request_id = next(iter(chat.pending))
    assert chat.confirm(request_id, ConfirmAnswer(approved=True)) is True
    await task
    assert chat.confirm(request_id, ConfirmAnswer(approved=True)) is False


# -------------------------------------------------------------------- the hook


def decision(output: dict) -> str | None:
    return (output.get("hookSpecificOutput") or {}).get("permissionDecision")


async def test_hook_allows_our_own_tools(chat):
    output = await chat._pre_tool_use(
        {"tool_name": chat_tools.qualified("list_roles"), "tool_input": {}}, "t1", {}
    )
    assert output == {}


async def test_hook_denies_anything_that_is_not_ours(chat):
    """tools=[] should mean no built-ins, but the hook refuses them regardless."""
    for name in ("Bash", "Write", "Read", "mcp__other__thing"):
        output = await chat._pre_tool_use({"tool_name": name, "tool_input": {}}, "t1", {})
        assert decision(output) == "deny", name


async def test_hook_denies_when_it_breaks(chat, monkeypatch):
    """A raising hook is fail-open in the SDK, so it must never raise."""
    monkeypatch.setattr(chat, "_known_tools", None)  # force an internal error
    output = await chat._pre_tool_use({"tool_name": "whatever"}, "t1", {})
    assert decision(output) == "deny"
    assert "internal error" in output["hookSpecificOutput"]["permissionDecisionReason"]


# --------------------------------------------------------------------- the API


async def test_chat_lifecycle(client):
    assert (await client.get("/api/chat")).json() == []

    created = await client.post("/api/chat", json={"title": "Set up a review task"})
    assert created.status_code == 201
    chat_id = created.json()["chat_id"]

    listed = (await client.get("/api/chat")).json()
    assert [c["title"] for c in listed] == ["Set up a review task"]
    assert listed[0]["status"] == "idle"

    detail = (await client.get(f"/api/chat/{chat_id}")).json()
    assert detail["messages"] == []
    assert detail["pending_confirmations"] == []
    assert detail["busy"] is False

    assert (await client.delete(f"/api/chat/{chat_id}")).status_code == 204
    assert (await client.get("/api/chat")).json() == []


async def test_unknown_chat_is_404(client):
    assert (await client.get("/api/chat/nope")).status_code == 404
    assert (await client.delete("/api/chat/nope")).status_code == 404
    assert (
        await client.post("/api/chat/nope/messages", json={"text": "hi"})
    ).status_code == 404
    assert (
        await client.post("/api/chat/nope/confirmations/x", json={"approved": True})
    ).status_code == 404


async def test_empty_message_is_rejected(client):
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    response = await client.post(f"/api/chat/{chat_id}/messages", json={"text": "   "})
    assert response.status_code == 422


async def test_answering_an_unknown_confirmation_conflicts(client):
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    await manager.attach(chat_id)
    response = await client.post(
        f"/api/chat/{chat_id}/confirmations/no-such-request", json={"approved": True}
    )
    assert response.status_code == 409


async def test_transcript_is_persisted_and_replayed_from_the_table(client):
    """After a revive the in-memory buffer is empty, so replay must hit the table."""
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    await chat.bus.emit("user", {"text": "hello"})
    await chat.bus.emit("assistant", {"text": "hi, what shall I set up?"})

    detail = (await client.get(f"/api/chat/{chat_id}")).json()
    assert [m["type"] for m in detail["messages"]] == ["user", "assistant"]

    await manager.close(chat_id)
    manager._chats.pop(chat_id, None)
    revived = await manager.attach(chat_id)

    replayed = await revived.bus.replay(1)
    assert [e["payload"]["text"] for e in replayed] == ["hi, what shall I set up?"]
    assert [e["seq"] for e in replayed] == [2]


async def test_live_bus_replays_and_fans_out(client):
    """The live branch's mechanics, tested on the bus directly.

    Driving the live SSE endpoint over ASGI would block: an open chat's stream stays
    open for the next turn by design, so there is nothing to await. The endpoint is a
    thin wrapper over exactly these three operations.
    """
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    bus = chat.bus

    await bus.emit("assistant", {"text": "first"})
    queue = bus.subscribe()

    # A late subscriber gets the backlog via replay, not via the queue.
    assert [e["payload"]["text"] for e in await bus.replay(0)] == ["first"]
    assert queue.empty()

    # Anything emitted after subscribing is fanned out live.
    await bus.emit("assistant", {"text": "second"})
    event = await asyncio.wait_for(queue.get(), 2)
    assert event["payload"]["text"] == "second"
    assert event["seq"] == 2

    # Closing the chat terminates every attached stream.
    bus.close()
    assert (await asyncio.wait_for(queue.get(), 2))["type"] == "_eof"

    bus.unsubscribe(queue)
    assert not bus._subscribers


async def test_a_revived_chat_continues_the_sequence(client):
    """Re-attaching after a restart must not reuse sequence numbers."""
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    await chat.bus.emit("user", {"text": "one"})
    await chat.bus.emit("user", {"text": "two"})

    await manager.close(chat_id)
    revived = await manager.attach(chat_id)
    assert revived.bus.seq == 2
    await revived.bus.emit("user", {"text": "three"})

    detail = (await client.get(f"/api/chat/{chat_id}")).json()
    assert [m["seq"] for m in detail["messages"]] == [1, 2, 3]


async def test_a_chat_interrupted_by_a_restart_is_recovered(client):
    """A process restart drops the in-memory turn; the row must not stay 'thinking'.

    Otherwise the panel shows a chat frozen mid-turn forever, which is exactly the
    failure this reproduces.
    """
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    await chat.bus.emit("user", {"text": "add the git mcp server"})
    await chat._set_status("thinking")

    # A restart drops the in-memory object and leaves the row exactly as it was —
    # so pop it rather than closing it, which would tidy the status on the way out.
    manager._chats.pop(chat_id, None)

    revived = await manager.attach(chat_id)
    assert revived.busy is False

    detail = (await client.get(f"/api/chat/{chat_id}")).json()
    assert detail["chat"]["status"] == "idle"
    notes = [
        m["payload"].get("note")
        for m in detail["messages"]
        if m["type"] == "status" and m["payload"].get("note")
    ]
    assert any("interrupted" in (note or "") for note in notes), notes


async def test_a_turn_that_never_connects_fails_visibly(client, monkeypatch):
    """A stalled session must surface an actionable error, not sit silent forever."""
    monkeypatch.setattr("app.services.chat.CONNECT_TIMEOUT_S", 0.2)
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)

    async def never_connects():
        await asyncio.sleep(30)

    monkeypatch.setattr(chat, "_ensure_client", never_connects)
    await chat.send("hello")
    await asyncio.wait_for(chat._turn, 10)

    detail = (await client.get(f"/api/chat/{chat_id}")).json()
    assert detail["chat"]["status"] == "failed"
    assert "did not start within" in detail["chat"]["error_text"]
    errors = [m for m in detail["messages"] if m["type"] == "error"]
    assert errors and "ANTHROPIC_API_KEY" in errors[0]["payload"]["message"]


async def test_a_failed_turn_drops_the_broken_client(client, monkeypatch):
    """Otherwise the next message reuses a half-connected session and stalls again."""
    monkeypatch.setattr("app.services.chat.CONNECT_TIMEOUT_S", 0.2)
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    chat._client = object()  # pretend a previous connect half-succeeded

    async def never_connects():
        await asyncio.sleep(30)

    monkeypatch.setattr(chat, "_ensure_client", never_connects)
    await chat.send("hello")
    await asyncio.wait_for(chat._turn, 10)
    assert chat._client is None


async def test_opening_an_interrupted_chat_recovers_it_on_first_load(client):
    """GET /api/chat/{id} must not render a stale 'thinking' the stream contradicts."""
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    await chat.bus.emit("user", {"text": "add the git mcp server"})
    await chat._set_status("thinking")
    manager._chats.pop(chat_id, None)

    detail = (await client.get(f"/api/chat/{chat_id}")).json()
    assert detail["chat"]["status"] == "idle"
    assert detail["busy"] is False
    notes = [
        m["payload"].get("note")
        for m in detail["messages"]
        if m["type"] == "status" and m["payload"].get("note")
    ]
    assert any("interrupted" in (note or "") for note in notes), notes


async def test_the_event_stream_revives_a_chat_that_is_only_in_the_database(client):
    """The browser's stream must stay live, not end instantly on a cold chat.

    Serving it from the table would emit _eof, the browser would close the
    connection, and every later event would be lost — the chat would look stuck.
    """
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    await chat.bus.emit("user", {"text": "hello"})
    await manager.close(chat_id)
    manager._chats.pop(chat_id, None)
    assert manager.get(chat_id) is None

    # Reading the stream would block (it stays open), so drive the handler's
    # dependencies the way the endpoint does and check it attached.
    from app.routers import chat as chat_router

    assert chat_router.manager.get(chat_id) is None
    revived = await chat_router.manager.attach(chat_id)
    assert chat_router.manager.get(chat_id) is revived
    # A live bus means the stream loop stays open instead of emitting _eof.
    assert revived.bus.closed is False


async def test_pending_confirmations_endpoint_sees_every_chat(client):
    chat_id = (await client.post("/api/chat", json={})).json()["chat_id"]
    chat = await manager.attach(chat_id)
    task = asyncio.create_task(
        chat._can_use_tool(chat_tools.qualified("create_role"), {"name": "X"}, FakeContext())
    )
    await asyncio.sleep(0.05)
    pending = (await client.get("/api/chat/pending-confirmations")).json()
    assert len(pending) == 1
    assert pending[0]["chat_id"] == chat_id
    assert pending[0]["tool_name"] == "create_role"

    answered = await client.post(
        f"/api/chat/{chat_id}/confirmations/{pending[0]['id']}",
        json={"approved": False, "reason": "not now"},
    )
    assert answered.status_code == 200
    assert (await task).behavior == "deny"
