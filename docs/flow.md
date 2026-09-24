
Perfect question — this traces through every layer. Here's the complete flow.

The shape of it first

The single most important thing to notice: one click produces three separate HTTP interactions, and the real work happens outside all of them.

 BROWSER                          SERVER                        CLAUDE CLI
    │
    │ ① POST /api/chat ─────────────► create row, emit "user",
    │                                 create_task(turn) ──────────┐
    │ ◄──────── 201 {chat_id} ───────┘                            │
    │                                                    (turn runs in
    │ ② GET /api/chat/{id} ────────► row + stored messages         background,
    │ ◄──────── {chat, messages} ───┘                              outliving
    │                                                              every
    │ ③ GET /api/chat/{id}/events ─► subscribe to the bus          request)
    │ ◄═══════ SSE, stays open ═════ events stream out ◄───────────┘
    │

①  and ② are over in milliseconds. ③ stays open for as long as you're looking at the page, and that's where everything you actually see arrives.

---

Phase 1 — In the browser

1. The click. <button type="submit"> inside a <form> fires the form's submit event. Not a click handler —
the form's.

2. onsubmit runs (chat.js:39-55):

event.preventDefault();                vigating away
const text = input.value.trim();
if (!text) { toast(...); return; }
button.disabled = true;                    // (intends to block double-submit)

3. api() does the HTTP (lib.js:34-52):

await api("/api/chat", { method: "POST", body: { title: text.slice(0,80), message: text } });

Inside api, body is a plain object, so it gets JSON.stringify'd and a Content-Type: application/json
header added, then handed to fetch. Thed because this is JavaScript's singlethread too, the browser stays responsive while it waits.

Phase 2 — Into the server

4. uvicorn → ASGI → FastAPI matches POST /api/chat to start_chat (routers/chat.py:36-42).

5. Pydantic validates the body into ChatStartIn (schemas/__init__.py:360-362):

class ChatStartIn(BaseModel):
    title: str = "New chat"
    message: str | None = None

Bad JSON or a wrong type → FastAPI returns 422 and your function never runs.

▎ Notice: this endpoint has no session: AsyncSession = Depends(get_session). The request doesn't own a
▎ database session, because the work itquest — a request-scoped session wouldbe closed out from under it. The ChatManager opens its own sessions instead.

Phase 3 — Create the chat

6. manager.create(title) (chat.py:485-491):

chat_id = str(uuid.uuid4())
async with sessionmaker()() as session:
    session.add(ChatSession(id=chat_id, title=title))
    await session.commit()             urable NOW
self._chats[chat_id] = Chat(chat_id, chat_event_bus(chat_id))
return chat_id

Two things exist afterwards: a row in Sand a live Chat object in a dict (doesnot). That split is the whole basis of the restart-recovery design.

The Chat gets a fresh EventBus with seq starting at 0.

Phase 4 — Kick off the first turn

7. Because payload.message was provided (routers/chat.py:39-41):

chat = await manager.attach(chat_id)
await chat.send(payload.message)

attach finds it already in _chats and r502-504) — the revive path is for later.

8. send (chat.py:287-291):

async def send(self, text: str) -> None
    if self.busy:
        raise RuntimeError("this chat ivious message")
    await self.bus.emit("user", {"text": text})              # seq 1
    self._turn = asyncio.create_task(sefire and continue

This is the pivot of the entire flow. cl turn as an independent task andreturns immediately.

▎ Notice: the "user" event is emitted when nobody is subscribed yet — the browser hasn't opened the SSE
▎ stream. It isn't lost, because EventBat_messages table before publishing(events.py:66-71). This is why persist-before-publish isn't pedantry: at this exact moment it's the
▎ only thing keeping your own message f

9. Return 201 {"chat_id": "..."} — whilen contacted yet.

Phase 5 — Browser navigates

10. Back in the handler:

const { chat_id } = await api(...);
location.hash = `#/chat/${chat_id}`;

11. Setting location.hash fires hashchange → app.js:47 → render():

const { name, arg } = parseHash();            // {name: "chat", arg: "<uuid>"}
if (cleanup) cleanup();                evious view
cleanup = await VIEWS[name].render(panel, arg);

12. chat.js:11 — arg is truthy this time, so renderChat(panel, chatId).

Phase 6 — Load the transcript

13. GET /api/chat/{id} → get_chat (routers/chat.py:45-77).

Note the comment on line 47: it attaches before reading, because attaching is what resets a stale
thinking status. Read first and you'd rthen silently contradicts.

Returns {chat, messages, pending_confir

14. Build the DOM — transcript from detly composer, a confirmations container,a connection banner, and a status tag (chat.js:104-162).

Phase 7 — Open the live stream

15. (chat.js:199-202):

const lastSeq = detail.messages.length ? detail.messages.at(-1).seq : 0;
const source = new EventSource(`/api/chvent_id=${lastSeq}`);
const seen = new Set(detail.messages.map((m) => m.seq));                                                 
Here is the race, and how it's handled. Between step 13 (detail returns messages up to seq N) and step 15 ream connects), the background turn +1 and N+2. Two mechanisms cover it:
                                                                                                          ast_event_id=N → the server replays .py:74-80, from the ring buffer or thetable)                                                                                                  he seen Set → if the same seq arrivechat.js:247)
                                                                                                          t and braces, because the two fetche
                                                                                                           Register listeners per event type, ction:
                                                                                                          urn () => source.close();       // ←u navigate away
                                                                                                          se 8 — The server holds the stream o
                                                                                                          eam() (routers/chat.py:132-154):
                                                                                                          ue = bus.subscribe()
try:                                                                                                       for event in await bus.replay(afterap
        yield _sse(event)                                                                                  while True:
        try:                                                                                                       event = await asyncio.wait__SECONDS)
        except asyncio.TimeoutError:                                                                               yield ": keep-alive\n\n"   ing kills it
            continue                                                                                           yield _sse(event)
finally:                                                                                                   bus.unsubscribe(queue)             't leak
                                                                                                          s generator is parked on queue.get()nsuming nothing.
                                                                                                          se 9 — Meanwhile: the turn (the actu
                                                                                                           of this has been running in the bac
                                                                                                          n_turn (chat.py:293-305) takes the tking (which emits seq 2), and wraps thebody in TURN_TIMEOUT_S.                                                                                  
_turn_body (chat.py:307-330):                                                                            
client = await asyncio.wait_for(self._ensure_client(), CONNECT_TIMEOUT_S)                                 it client.query(text)
async for message in client.receive_response():                                                            await self._handle(message)
                                                                                                          sure_client (chat.py:258-283) buildse claude CLI as a subprocess — that'sthe slow part, hence its own 90s timeout with an actionable error. The options are where the confinement  es (chat.py:235-250): tools=[], the _use_tool, the PreToolUse hook.
                                                                                                          ndle (chat.py:349-398) translates SD:
                                                                                                          ─────────────────────────────┬───────────────────────┐
│          SDK message          │                   → bus event                   │                       ─────────────────────────────┼───────────────────────┤
│ SystemMessage(init)           │ status (+ stores the SDK session id for resume) │                       ─────────────────────────────┼───────────────────────┤
│ AssistantMessage → TextBlock  │ assistant                                       │                       ─────────────────────────────┼───────────────────────┤
│ → ThinkingBlock               │ thinking                                        │                       ─────────────────────────────┼───────────────────────┤
│ → ToolUseBlock                │ tool_use                                        │                       ─────────────────────────────┼───────────────────────┤
│ UserMessage → ToolResultBlock │ tool_result                                     │                       ─────────────────────────────┼───────────────────────┤
│ ResultMessage                 │ result (+ adds cost to the row)                 │                       ─────────────────────────────┴───────────────────────┘
                                                                                                           every one of those emit calls does q += 1 → build event → append to ringbuffer → persist to SQLite → put_nowait into every subscribed queue.                                     
Which unparks the SSE generator → yields id: 7\nevent: assistant\ndata: {...} → travels down the open     nection → source.addEventListener("a append(event) →transcript.append(line(event)) → text appears on screen.                                                 
That's the full pipe. SDK message → bus → SQLite + queue → SSE → DOM.                                    
One nice touch in append (chat.js:249-252):                                                              
const atBottom = transcript.scrollTop + transcript.clientHeight >= transcript.scrollHeight - 30;          nscript.append(line(event));
if (atBottom) transcript.scrollTop = transcript.scrollHeight;                                            
Auto-scroll only if you were already at the bottom — so it doesn't yank the view while you're reading     ollback.
                                                                                                          se 10 — If the model tries to change
                                                                                                           the model calls create_role. Now th
                                                                                                          The PreToolUse hook fires first (chae of our tools? Yes → {} (no decision).
2. can_use_tool (chat.py:172-207): is_mutating("mcp__harness__create_role") → True, so:                  
request = PendingConfirmation(id=uuid4(), ..., future=loop.create_future())                               f.pending[request.id] = request
await self._set_status("awaiting_confirmation")                                                           it self.bus.emit("confirmation_reque
answer = await asyncio.wait_for(request.future, CONFIRM_TIMEOUT_S)   # ← PARKED                          
3. That event reaches the browser → drawConfirmations renders the Apply / "No — tell it why" card         (chat.js:212-217, 164-185).
4. You click Apply → POST /api/chat/{id}/confirmations/{req} → chat.confirm() → future.set_result(answer) (chat.py:432-437).
5. The parked coroutine wakes up in a completely different request's stack, returns                       PermissionResultAllow(), and only thun and touch the database.
                                                                                                          his is the Future-as-bridge pattern.sk resumes a coroutine parked inanother. The tool body has not executed at all while you were deciding — a declined call changes        othing, and your reason is handed baial message.
                                                                                                          se 11 — Finish
                                                                                                          eive_response() ends → status idle (ll_pending("turn ended"). The SSE stream stays open, waiting for your next message. (Unlike a run's stream, which terminates — a deliberate        mmetry noted in DESIGN.md § 5a.4.)
                                                                                                         
                                                                                                           five lessons in this trace
                                                                                                          The response is not the result. ① reconds; the work runs for minutesafterwards. Any API that triggers slow work should look like this: create a record, return its id,     stream progress separately.
2. seq is the spine. One monotonic counter serves as the database primary ordering, the SSE id:, the      resume cursor (Last-Event-ID), and tne good identifier removed four separate problems.                                                                                              Persist before publish. Your own firre any subscriber exists. Durabilityfirst means "nobody is listening yet" and "the listener reconnected" are the same case.                State lives in two places on purposeth; the Chat object in _chats is a liveattachment to it. attach() reconciles them — which is exactly why a server restart mid-turn is         recoverable instead of a permanent h
5. A Future turns "wait for a human" into ordinary async code. No polling loop, no state machine — just   await on a future that a different H
                                                                                                         
                                                                                                          ck yourself
                                                                                                          You click "Start chat" and immediateage navigates. Is the chat created? Isyour message lost? Trace which steps ran and which didn't.                                             In step 8, emit("user", ...) publishich specific line makes that safe, andwhat would you see in the transcript if publish came before persist and the process crashed in         between?
3. You navigate to the Runs panel while the model is mid-answer, then come back. What stops the           transcript from having duplicate lin
4. Someone "simplifies" send by changing create_task(self._run_turn(text)) to await self._run_turn(text). Describe exactly what the user sees ".
5. The model calls create_role, you leave for lunch, and come back 10 minutes later. What state is the chat in, and what did the model get