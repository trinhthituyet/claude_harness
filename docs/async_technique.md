# Techniques in This Codebase

Four topics, each grounded in code you can open:

| § | Topic | The question it answers |
| --- | --- | --- |
| 1–5 | **Async** | What one event loop buys, and the patterns built on it |
| 6 | **The ORM** | Why `MissingGreenlet` happens and why loaders are everywhere |
| 7 | **Layering** | Why `routers/` is the smallest layer, and what that buys |
| 8 | **Streaming** | How a run reaches the browser live and survives a reload |
| 9 | **Security** | Fail-open vs fail-closed, and why every rule here was measured |
| 10 | **Exercises** | Deliberate breaks; predict, then try |

Written to be read alongside the code. Every claim cites a file and line.

---

## 1. The decision everything else follows from

`DESIGN.md` § 0 justifies the whole stack in one line:

> Backend: FastAPI + `uvicorn`, **fully async** — The SDK is async (anyio); one event loop
> avoids bridging.

The Claude Agent SDK is async. Had the web framework been synchronous, every interaction
between the two would need a thread, a queue and `run_coroutine_threadsafe`, with the
classic failure modes that brings: callbacks firing on the wrong thread, futures resolved
from a loop that is not running, deadlocks on shutdown.

Async all the way down means there is exactly **one event loop in the process**, and
everything can simply `await` everything else. That is why:

* `aiosqlite`, not `sqlite3` (`pyproject.toml:18`)
* `httpx`, not `requests` (`pyproject.toml:25`)
* permission callbacks are plain `async def` methods that can hit the database directly
  (`app/security/gate.py:165`)
* an agent can be paused mid-tool-call until a human answers an HTTP request, in about
  fifteen lines (§ 3.5)

That last one would be a genuinely hard problem across a thread boundary. Here it is
ordinary code. One architectural decision at the top deleted a whole category of
complexity.

---

## 2. The five primitives

Everything in this codebase is built from these. Learn them as a set.

| Primitive | Means | Used here for |
| --- | --- | --- |
| `await x` | do it now, park until done | nearly every line |
| `asyncio.create_task(c)` | start it, carry on | work that outlives a request |
| `asyncio.Lock` / `Semaphore` | serialise / limit | one turn at a time; 3 runs max |
| `asyncio.Future` | park until *someone else* completes it | human approval |
| `asyncio.Queue` | hand items between coroutines | event fan-out to SSE |

The two least familiar are `Future` and `Queue`, and they are where the interesting work
happens.

---

## 3. The seven patterns

### 3.1 Background work that outlives the request

`app/services/runner.py:462-484`:

```python
async def start(self, session, task) -> str:
    snapshot = await build_snapshot(session, task)
    session.add(TaskRun(id=run_id, status="queued", ...))
    await session.commit()                       # 1. durable BEFORE spawning

    run = Run(run_id, snapshot)
    self._runs[run_id] = run                     # 2. findable
    self._tasks[run_id] = asyncio.create_task(   # 3. referenced
        self._supervise(run), name=f"run:{run_id}")   # 4. named
    return run_id                                # 202, in milliseconds
```

Four deliberate details, three of them footguns:

1. **Commit before spawning.** The client receives `run_id` and may immediately
   `GET /api/runs/{id}`. An uncommitted row returns 404.
2. **Register it.** `cancel`, `pending_approvals` and the SSE route all locate the live
   object by id.
3. **Keep the reference.** The event loop holds only a *weak* reference to tasks. Drop
   yours and the garbage collector may destroy a task mid-execution — non-deterministic,
   and effectively impossible to reproduce on demand. This is why `self._tasks[run_id] =`
   exists rather than a bare `create_task(...)`.
4. **Name it.** Free debuggability: a task dump shows `run:3f2a8c…`, not `Task-47`.

> **Rule.** `create_task` for work that must outlive its caller. Commit state first, keep
> the reference, name it.

The chat path is the same shape (`app/services/chat.py:287-291`), which is why
`POST /api/chat` can return `201 {chat_id}` before the model has been contacted at all.

### 3.2 The supervisor wrapper

`start` does not spawn `run.execute()`. It spawns `self._supervise(run)`
(`runner.py:486-498`):

```python
async def _supervise(self, run):
    try:
        async with self._semaphore:          # concurrency limit lives here
            await run.execute()
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await run._finalize("cancelled", "cancelled", None)
        raise
    except Exception:
        log.exception("supervisor for run %s failed", run.id)
    finally:
        self._runs.pop(run.id, None)         # registry cleanup, every path
        self._tasks.pop(run.id, None)
```

**The work and the bookkeeping about the work are separate functions.** `execute()` runs a
session; `_supervise` owns the semaphore, the registry and the "no matter how this ends"
guarantees.

This matters because the supervisor's `finally` is the only place that cannot be skipped.
Cleanup at the end of `execute()` would be bypassed by any early `return`.

`async with self._semaphore` (`runner.py:445`) is the concurrency limit: `Semaphore(3)`
admits three runs and parks the fourth at the `async with` until a permit frees. The task
is created immediately but waits its turn — which is what `status="queued"` means.

### 3.3 Cancellation discipline

`.cancel()` does not kill a task. It raises `CancelledError` **inside** it, at whatever
`await` it is parked on, so cleanup is possible:

```python
except asyncio.CancelledError:
    with contextlib.suppress(Exception):
        await run._finalize("cancelled", ...)   # best-effort DB update
    raise                                        # always re-raise
```

Three rules the codebase follows without exception:

* **Always re-raise.** Swallowing `CancelledError` tells the loop "I refuse to stop", and
  shutdown hangs. Same discipline at `chat.py:299-301`.
* **`contextlib.suppress` for best-effort cleanup.** During cancellation the database may
  also be going away; a failure there must not prevent the `raise`.
* **Cancellation is a normal path, not an error.** `runner.py:132-134` sets
  `self._cancelled` so `_finalize` records `cancelled` rather than `failed`.

Shutdown (`runner.py:507-515`):

```python
for run in list(self._runs.values()):      # list() — the dict mutates as we go
    with contextlib.suppress(Exception):
        await run.cancel()
for task in tasks:
    task.cancel()
await asyncio.gather(*tasks, return_exceptions=True)
```

`return_exceptions=True` collects failures instead of letting the first one abort the
wait, so one badly-behaved task cannot stop the others from shutting down.

### 3.4 Layered timeouts

Three nested `wait_for`s in a chat turn, each with a distinct meaning:

```python
# chat.py:297  — whole turn
await asyncio.wait_for(self._turn_body(text), TURN_TIMEOUT_S)              # 900s
# chat.py:311  — just starting the CLI subprocess
client = await asyncio.wait_for(self._ensure_client(), CONNECT_TIMEOUT_S)  #  90s
# chat.py:188  — waiting for a human
answer = await asyncio.wait_for(request.future, CONFIRM_TIMEOUT_S)         # 300s
```

Two points worth copying.

**The constants have a documented relationship** (`chat.py:59-61`):

```python
#: Whole-turn ceiling. Must exceed CONFIRM_TIMEOUT_S, since a turn legitimately
#: blocks while waiting for the user to answer a confirmation.
TURN_TIMEOUT_S = 900
```

Were the turn timeout shorter than the confirmation timeout, every slow human would look
like a crashed turn. That invariant is invisible unless written down.

**A distinct exception type, for a real Python 3.11 reason** (`chat.py:99-105`):

```python
class SessionStartError(RuntimeError):
    """A distinct type rather than TimeoutError: since Python 3.11
    ``asyncio.TimeoutError`` *is* ``TimeoutError``, so raising that here would be
    indistinguishable from the whole-turn timeout."""
```

In 3.11 `asyncio.TimeoutError` became an alias for the builtin, so the inner timeout
cannot re-raise a `TimeoutError` — the outer handler (`chat.py:333`) would match it and
print the wrong, generic message. A custom type preserves the actionable one.

> **Rule.** Put a timeout on every wait that depends on something outside the process.
> Decide what each one *means*, and make sure a nested timeout cannot be mistaken for its
> parent.

### 3.5 `Future` as a bridge to the outside world

The problem: the agent's permission callback must pause until a human clicks a button in a
browser. `app/security/gate.py:219-250` (and `chat.py:176-207`, same shape):

```python
loop = asyncio.get_running_loop()
request = PendingApproval(id=str(uuid.uuid4()), ..., future=loop.create_future())
self.pending[request.id] = request
await self._emit("permission_request", request.public())      # -> SSE -> modal
try:
    answer = await asyncio.wait_for(request.future, self.approval_timeout_s)
except asyncio.TimeoutError:
    return PermissionResultDeny(message="approval timed out; denied")
finally:
    self.pending.pop(request.id, None)
```

Completed from an entirely separate HTTP request (`gate.py:278-284`):

```python
def resolve(self, request_id, answer) -> bool:
    request = self.pending.get(request_id)
    if request is None or request.future.done():
        return False                        # -> 409, not a crash
    request.future.set_result(answer)       # the parked coroutine wakes
    return True
```

The recipe, generalised:

1. Create a `Future`, store it in a dict keyed by a unique id.
2. Send the id out — SSE, webhook, message queue, anything.
3. `await asyncio.wait_for(future, timeout)`.
4. Something external calls `set_result`; your coroutine resumes where it parked.
5. `finally` removes the entry.

Four safety properties to copy exactly:

* **Timeout denies**, never allows. A closed browser must not wedge the run, and must not
  default to yes.
* **`future.done()` guard** turns double-answering into a 409 rather than an
  `InvalidStateError`.
* **`finally` pop** on every path: timeout, answer, cancellation.
* **`fail_all_pending`** (`gate.py:286-290`) resolves everything as denied on cancel and
  shutdown, so no coroutine is left awaiting a future nobody will set. Called from
  `cancel()`, from `execute()`'s `finally`, and from `_run_turn`'s `finally`.

Note that `resolve` is a plain `def`. `set_result` does not block — it marks the future
ready and schedules the waiter — so there is no reason to make it a coroutine.

### 3.6 Fan-out with bounded queues, and async generators

Producer (`app/services/events.py:56-72`):

```python
await self._persist_one(event)                  # durable first
for queue in list(self._subscribers):
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:                   # slow consumer: drop, don't block
        pass
```

Consumer, one per open browser tab (`app/routers/chat.py:132-148`):

```python
async def live() -> AsyncIterator[str]:
    queue = bus.subscribe()
    try:
        for event in await bus.replay(after):
            yield _sse(event)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield _sse(event)
    finally:
        bus.unsubscribe(queue)
```

Four techniques stacked:

* **`put_nowait` plus `QueueFull` → drop.** `await queue.put(...)` would park the
  *producer* when a consumer is slow, so one stalled browser would freeze the agent. The
  bounded `Queue(maxsize=1000)` (`events.py:83`) makes "drop the slow consumer" an
  explicit, bounded decision instead of unbounded memory growth.
* **An async generator as an HTTP response.** `yield` inside `async def` produces an async
  iterator; `StreamingResponse` consumes it, so the body is generated lazily and
  indefinitely.
* **Timeout as a heartbeat.** `wait_for(queue.get(), 15)` is not error handling — the
  timeout *is* the feature. No event in 15s emits an SSE comment so that intermediaries
  and browsers do not declare the connection dead.
* **`finally: unsubscribe`.** A client disconnect raises inside the generator. Without
  this, every reconnect leaks a queue that the producer keeps writing into forever.

### 3.7 A session per operation, not per task

Background code cannot use the request's database session: the request ends and the
session closes. So each background operation opens its own, briefly
(`runner.py:315-320`):

```python
async def _set_status(self, status: str) -> None:
    async with sessionmaker()() as session:
        run = await session.get(TaskRun, self.id)
        if run is not None:
            run.status = status
            await session.commit()
```

The same shape appears in `_audit`, `_record_config`, `_finalize`, `_add_cost` and every
chat tool handler (`app/services/chat_tools.py:79-84`).

This looks repetitive but is correct, for a reason specific to async: **a session holds a
connection and an open transaction.** A run lasts minutes. One session for a whole run
would pin a connection and keep a write transaction open across dozens of `await` points —
in SQLite, blocking every other writer for that entire time.

> **Rule.** Open a session, do one unit of work, commit, close. Never hold one across a
> long await.

---

## 4. The rules these patterns follow

### 4.1 Never block the loop

One thread, so any wait without `await` freezes everything: `time.sleep`, `requests.get`,
`sqlite3`. This is what dictates the dependency list.

### 4.2 Do not put per-call state on `self`

From `DESIGN.md` § 4.9:

> The SDK dispatches hooks for an event **concurrently**, so the gate must be re-entrant
> and must not accumulate per-call state on `self`.

`Gate.pre_tool_use` (`gate.py:112-161`) keeps `tool_name`, `tool_input` and `verdict` as
locals. The only shared mutable state is `self.pending`, keyed by a unique uuid, so two
concurrent calls cannot collide.

> **Rule.** Locals are per-call and safe. `self.x = ...` in a method that can run
> concurrently is a data race waiting to happen.

### 4.3 You need far fewer locks than in threading

Threads can be interrupted anywhere — mid-`+=`, mid-dict-update. Coroutines are only
interrupted at an `await`. So these need no lock:

```python
self._runs.pop(run.id, None)
self._seen.add(seq)
```

The rule becomes simple and checkable: **look at the critical section; if it contains no
`await`, it is already atomic.**

### 4.4 Invariants belong in `finally`

Every guarantee in this codebase lives in a `finally`: unsubscribe the queue, pop the
registry, pop the pending confirmation, fail all pending approvals, reset the status.

---

## 5. Where it is subtler than it looks

Four honest notes, because the edges are where the understanding is.

### 5.1 A lock that is not strictly needed

```python
# events.py:57-65
async with self._lock:
    self._seq += 1
    event = {...}
    self._buffer.append(event)
```

There is no `await` inside that block, so by § 4.3 it is already atomic and the lock is
technically redundant. It is not wrong: it documents "these three lines are one unit" and
stays correct if someone later adds an `await` inside. Both facts are worth knowing.

### 5.2 Ordering comes from having one producer, not from the bus

`persist` and the queue publish happen **outside** the lock (`events.py:66-71`). Two
concurrent `emit` calls could therefore reach the subscriber queues in a different order
than their `seq` values, because each awaits a database write in between.

For replay this does not matter: `replay` orders by `seq`. For the live transcript the
browser appends in *arrival* order (`app/static/views/chat.js:251`), so lines could in
principle appear out of order.

In practice there is one producer per stream — the turn task — so it does not arise. But
the invariant lives in the caller, not in the bus. Worth a comment at the call site.

### 5.3 "Never block" means "never block for *long*"

`path_guard.resolve_candidate` calls `os.path.realpath` and `Path.exists()`
(`app/security/path_guard.py:48-68`), both blocking syscalls, on the hot path of every
tool check. `settings_builder._read_user_settings` does a synchronous `read_text`
(`app/services/settings_builder.py:28`).

These are microseconds and are fine. But be precise about the rule: the loop cannot be
interrupted, so *every* synchronous call blocks it. The only question is whether the
duration matters. Microseconds: fine. A 200 ms HTTP call: catastrophic.

### 5.4 Bridging a sync callback back into async

The SDK's `stderr` hook is a plain synchronous function that sometimes needs to trigger
async work (`runner.py:270-275`):

```python
def _capture_stderr(self, line: str) -> None:        # sync, called by the SDK
    if "sandbox_apply: Operation not permitted" in line:
        asyncio.create_task(self._abort_unsandboxed())   # schedule async work
```

`create_task` is the bridge from sync code *already running on the loop* into async work.
From a different thread you would need `run_coroutine_threadsafe` instead — and the fact
that this project never needs that is § 1 paying off.

---

## 6. The ORM: async SQLAlchemy

### 6.1 The contradiction

In SQLAlchemy, **reading an attribute can run a SQL query.** In async, a query needs
`await`. But `team.members` has nowhere to put an `await`. Resolving that contradiction is
what `lazy="selectin"`, `selectinload`, `populate_existing` and `expire_on_commit=False`
are all for.

In the sync ORM, relationships are **lazy loaded** — left empty, then fetched on first
touch through Python's attribute-access hook:

```python
team = session.get(Team, 1)          # SELECT * FROM teams WHERE id = 1
print(team.name)                     # no SQL — already loaded
print(team.members)                  # SELECT * FROM team_roles WHERE team_id = 1  <- !
```

That third line issued a query, and nothing in the syntax says so. It is also the origin of
the N+1 problem: fifty teams with three roles each is 201 queries from four lines of code.

### 6.2 Why async breaks it, and what `MissingGreenlet` means

```python
team = await session.get(Team, 1)    # fine — awaited
print(team.members)                  # boom
```

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called;
can't call await_only() here. Was IO attempted in an unexpected place?
```

**Python attribute access cannot be awaited.** `team.members` goes through `__getattr__`,
and there is no `__await_getattr__`; `await team.members` is not a thing, because
`members` is not a coroutine.

So SQLAlchemy's async support is not a rewrite. It is the same sync ORM with a shim: when
the sync ORM needs I/O, it calls into a **greenlet** — a lightweight coroutine that can
suspend from anywhere, including from inside `__getattr__` — which hands the awaitable out
to the real event loop, waits, and resumes the sync code as if nothing happened.

That works only *inside a greenlet context*, which exists only while you are inside an
awaited SQLAlchemy call (`await session.execute(...)`, `await session.get(...)`,
`await session.refresh(...)`, `await conn.run_sync(...)`). Touch a lazy attribute outside
one and there is no greenlet to suspend into — hence "greenlet_spawn has not been called".
This is also why `greenlet>=3.0` is a direct dependency (`pyproject.toml:17`) although no
project code imports it.

**The practical consequence: lazy loading is effectively unavailable.** Every relationship
you intend to read must be loaded while you are awaiting a query, which means declaring it
up front.

### 6.3 The four ways this codebase declares it

**Way 1 — on the relationship.** `lazy="selectin"` means "whenever this parent is loaded,
immediately load this collection too":

```python
# app/models/team.py:34-39
members: Mapped[list["TeamRole"]] = relationship(
    back_populates="team",
    cascade="all, delete-orphan",
    order_by="TeamRole.position",
    lazy="selectin",
)
```

It is the default throughout: `Team.members`, `TeamRole.role`, `Task.team`,
`Task.workflow`, `Task.skill_links`, `Task.mcp_links` (`app/models/task.py:62-69`),
`Workflow.nodes`, `Workflow.edges`, `WorkflowNode.role`, `WorkflowEdge.from_node`,
`WorkflowEdge.to_node` (`app/models/workflow.py:29-42`, `70`, `101-102`).

> **Rule.** In async SQLAlchemy, make eager the default at the model level. Lazy-by-default
> is a footgun you will rediscover in production.

**Way 2 — per query.** Chained `selectinload` names a path down the object graph
(`app/services/crud.py:392-397`):

```python
def task_loaders():
    """Eager-load everything TaskOut and the run snapshot read."""
    return (
        selectinload(Task.team).selectinload(Team.members).selectinload(TeamRole.role),
        selectinload(Task.workflow).selectinload(Workflow.nodes).selectinload(WorkflowNode.role),
        selectinload(Task.workflow).selectinload(Workflow.edges),
        selectinload(Task.skill_links),
        selectinload(Task.mcp_links),
    )
```

Read the first line as `Task -> team -> members -> role`. That is four queries total
(tasks, teams, team_roles, roles) **regardless of how many tasks**, because `selectinload`
collects the parent ids and issues `WHERE id IN (...)`.

Note that chaining and listing mean different things: `selectinload(a).selectinload(b)`
walks a path two levels deep, whereas two separate tuple entries load two *sibling*
relationships. `Workflow.nodes` and `Workflow.edges` are siblings, so they are two entries.

Since the relationships above are already `lazy="selectin"`, these explicit loaders are
largely redundant on their own. What makes them load-bearing is the combination with
Way 3.

**Way 3 — `populate_existing`, and the identity-map trap.** A `Session` keeps an identity
map: a cache of "object with this primary key". A later query for an object already in the
session returns *the same Python object*, and by default does **not** overwrite its loaded
state with the new result. So a loader can appear to be ignored:

```python
team = await crud.create_team(session, payload)   # Team(id=1) cached, members NOT loaded
team = await crud.load_team(session, team.id)     # query asks for selectinload(members)...
team.members                                      # still unloaded -> MissingGreenlet
```

`crud.load_team` (`crud.py:79-95`) fixes it, and the docstring says why:

```python
stmt = (
    select(Team)
    .options(selectinload(Team.members).selectinload(TeamRole.role))
    .where(Team.id == team_id)
    .execution_options(populate_existing=True)    # refresh the cached instance
)
```

Every loader function here pairs the two: `load_team` (`crud.py:86-92`), `load_workflow`
(`crud.py:253-263`), `load_task` (`crud.py:406`).

> **Rule.** To re-read an object *with relationships* after creating or modifying it, use
> `select(...).options(...).execution_options(populate_existing=True)` — not `session.get()`.

**Way 4 — sidestep the ORM.** Sometimes the fix is to touch no relationship at all
(`crud.py:302-314`):

```python
async def set_task_links(session, task, payload) -> None:
    """Replace the skill / MCP links.

    Done with DELETE statements rather than by clearing the ORM collections: on a
    freshly flushed Task those collections are unloaded, and touching them would
    trigger a lazy load that async SQLAlchemy cannot perform.
    """
    await session.execute(delete(TaskSkill).where(TaskSkill.task_id == task.id))
```

The "natural" ORM version — `task.skill_links.clear()` — *reads* the collection before
clearing it. A bulk `delete()` statement loads nothing.

> **Rule.** For bulk changes prefer explicit `delete()` / `update()` statements over
> mutating ORM collections. Faster, and it cannot trip the greenlet problem.

### 6.4 `expire_on_commit=False` is close to mandatory

```python
# app/db.py:26
_sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
```

By default `commit()` marks every loaded attribute as **expired**, so the next access
re-fetches. Sound reasoning in sync; in async that re-fetch is a lazy load, so with the
default every endpoint would break on the way out:

```python
row = await crud.create_role(session, payload)
await session.commit()
return row                       # FastAPI serialises it -> reads row.name -> boom
```

The routers add belt-and-braces anyway (`app/routers/people.py:84-86`): build the Pydantic
object *before* committing, so what is returned has nothing ORM-attached left to expire.
Strictly unnecessary given the flag, but it makes the ordering explicit and the endpoint
immune to session configuration.

### 6.5 `selectinload` versus the alternatives

| Strategy | SQL | Good for | Watch out |
| --- | --- | --- | --- |
| `selectinload` | 2 queries: parents, then `WHERE parent_id IN (...)` | **collections** (one-to-many) | one extra round trip per level |
| `joinedload` | 1 query with `LEFT OUTER JOIN` | **scalars** (many-to-one) | duplicates parent rows per child |
| `subqueryload` | 2 queries, child uses a subquery | legacy | slow with large IN lists |

`joinedload` on a one-to-many multiplies result rows: a team with five roles returns five
copies of the team, de-duplicated in Python. Nested collections make it a cartesian
product — ten tasks x five nodes x three edges is 150 rows carrying the same task fifteen
times. `selectinload` trades one extra round trip for flat results, and on local SQLite
round trips are nearly free. Hence its uniform use here.

### 6.6 Relationship patterns in `workflow.py`

The workflow graph is the richest ORM code in the project. Four techniques:

**`back_populates` — one relationship, two directions.**

```python
# Workflow
nodes: Mapped[list["WorkflowNode"]] = relationship(back_populates="workflow", ...)
# WorkflowNode
workflow: Mapped[Workflow] = relationship(back_populates="nodes", ...)
```

The two attributes are the same relationship seen from either end, so appending to
`workflow.nodes` sets `node.workflow` in memory with no round trip. Omit it and you get two
independent relationships that can silently disagree.

**`foreign_keys=` — required when the FK is ambiguous** (`workflow.py:83-102`):

```python
from_node_id: Mapped[int] = mapped_column(ForeignKey("workflow_nodes.id", ...))
to_node_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_nodes.id", ...))

from_node = relationship("WorkflowNode", foreign_keys=[from_node_id], lazy="selectin")
to_node   = relationship("WorkflowNode", foreign_keys=[to_node_id],   lazy="selectin")
```

`WorkflowEdge` has **two** foreign keys to the same table. SQLAlchemy normally infers the
join condition from the FK, but with two candidates it raises `AmbiguousForeignKeysError`
rather than guess. This is also a **self-referential** relationship, and `to_node_id` is
nullable by design (`workflow.py:86-89`): `NULL` means the edge finishes the workflow. The
terminal state is the absence of a target, not a magic row.

**`order_by` as a string.** `order_by="WorkflowNode.position"` is resolved lazily, after
all mappers are configured, so `Workflow` can reference `WorkflowNode` before that class
exists. Same reason the annotation is `Mapped[list["WorkflowNode"]]` in quotes. This is how
circular references in ORM definitions are broken.

**`cascade` and `ondelete` are different mechanisms, and both are present.**

```python
workflow_id: Mapped[int] = mapped_column(
    ForeignKey("workflows.id", ondelete="CASCADE"), index=True)     # database level
nodes: Mapped[list["WorkflowNode"]] = relationship(
    cascade="all, delete-orphan", ...)                              # ORM level
```

* `ondelete="CASCADE"` is a SQL constraint. It fires on any `DELETE FROM workflows`,
  whatever issued it — and in SQLite only when `PRAGMA foreign_keys=ON` (`db.py:116`,
  `db.py:148`).
* `cascade="all, delete-orphan"` is ORM bookkeeping. It fires on
  `await session.delete(workflow)` with objects loaded in the session, and it handles
  *orphans*: removing a node from `workflow.nodes` deletes it, which no database constraint
  can express.

Having both is correct, not redundant. And note the third policy, chosen deliberately:
`ondelete="RESTRICT"` on `WorkflowNode.role_id` (`workflow.py:55`) and `Task.team_id`
(`app/models/task.py:42-44`) *refuses* the delete, because losing a role should not
silently gut a workflow — while `TaskRun.task_id` uses `SET NULL`
(`app/models/run.py:18-20`) so run history survives deleting its task. Three policies,
three meanings; choosing per relationship is the actual modelling work.

### 6.7 `flush()` versus `commit()`

`crud.py` ends nearly every function with `await session.flush()` and never commits; the
routers commit (`app/routers/people.py:40`).

* **`flush()`** sends pending SQL and populates `row.id` from the autoincrement. The
  transaction stays open.
* **`commit()`** flushes, then ends the transaction — durable and visible to others.

That split is what lets `create_task` build a team, several new roles, a task row and its
link rows as **one atomic unit** (`crud.py:327-342`, `crud.py:147-171`). Each step flushes
so the next can use the generated id; nothing is durable until the router commits, so a
failure at step four means nothing happened at all.

> **Rule.** Services flush, the caller commits. A service that commits cannot be composed
> into a larger transaction.

### 6.8 Schema migration without Alembic

`create_all` creates missing *tables* but never alters an existing one, so a database from
an earlier version silently lacks new columns. Two functions close that gap.

**`_add_missing_columns`** (`db.py:51-79`) introspects each table with
`PRAGMA table_info`, diffs it against `Base.metadata`, and issues
`ALTER TABLE ... ADD COLUMN` for anything missing — compiling the DDL from the model's own
column definition via `CreateColumn`, so there is no hand-written SQL to drift. Its
limitation is stated in the code: a `NOT NULL` column with no default cannot be added to a
table with rows, so it logs a warning rather than failing.

**`_relax_task_team_id`** (`db.py:82-106`) handles the harder case — making
`tasks.team_id` nullable, now that a task may run a workflow instead of a team. SQLite
cannot drop a `NOT NULL` constraint in place, so the table is rebuilt:

```python
ALTER TABLE tasks RENAME TO tasks__old
Task.__table__.create(sync_conn)                   # fresh table from the model
INSERT INTO tasks (shared columns) SELECT ... FROM tasks__old
DROP TABLE tasks__old
```

Three details worth copying:

* **Guarded to run once** — it checks `notnull` on the existing column and returns early if
  already relaxed. Migrations must be idempotent.
* **`shared = [name for name in new if name in old]`** — copies only the intersection, so
  it works whether the old table had more or fewer columns.
* **`await conn.run_sync(...)`** — `Table.create()` is a sync API, and `run_sync` runs it
  inside the greenlet context. The same mechanism as § 6.2, used deliberately.

### 6.9 Symptom to fix

| Symptom | Cause | Fix |
| --- | --- | --- |
| `MissingGreenlet` on attribute access | lazy load outside an awaited query | `lazy="selectin"` or `selectinload()` |
| `MissingGreenlet` right after `commit()` | attributes expired | `expire_on_commit=False` |
| Loader "ignored"; collection still unloaded | identity map returned a cached object | `populate_existing=True` |
| `MissingGreenlet` clearing a collection | `.clear()` reads first | bulk `delete()` statement |
| `AmbiguousForeignKeysError` | two FKs to the same table | `foreign_keys=[col]` |
| 200 queries for one list page | N+1 lazy loading | eager load the path you will touch |

---

## 7. Layering

### 7.1 The map, measured

Four layers, one rule: **dependencies point inward only.**

```
routers/    1,139 lines   HTTP in, HTTP out. Knows FastAPI.
services/   3,883 lines   Behaviour. Knows the DB and the SDK. Knows nothing about HTTP.
security/     670 lines   Decisions. Pure. Knows nothing about anything.
models/                   Tables.
```

`DESIGN.md` § 2 states it as a rule:

> `routers` do HTTP + validation only; `services` hold behaviour; `security` is imported by
> `services/runner.py` and by nothing that could weaken it.

Note the ratio: **routers are the smallest layer.** In most FastAPI codebases they are the
biggest, because route functions accumulate logic until they are eighty lines each. That
inversion is the point.

### 7.2 Anatomy of a thin router

A complete endpoint (`app/routers/people.py:34-41`):

```python
@roles_router.post("", response_model=RoleOut, status_code=201)
async def create_role(payload: RoleIn, session: AsyncSession = Depends(get_session)):
    try:
        row = await crud.create_role(session, payload)
    except crud.CrudError as exc:
        raise _http(exc) from exc
    await session.commit()
    return row
```

Eight lines doing exactly five jobs, all of them HTTP jobs:

| Job | Mechanism |
| --- | --- |
| Route the URL | `@roles_router.post("")` |
| Parse and validate the body | `payload: RoleIn` (Pydantic) |
| Obtain a session | `Depends(get_session)` |
| Translate domain error to status code | `except CrudError` → `_http(exc)` |
| Own the transaction | `await session.commit()` |

There is no business logic: no duplicate-name check, no "a team needs exactly one lead", no
path validation. All of that lives in `crud.py`. The seam between domain and HTTP is one
three-line function (`people.py:19-20`, and the same shape at
`app/routers/workflows.py:18-19`):

```python
def _http(exc: crud.CrudError) -> HTTPException:
    return HTTPException(409 if exc.conflict else 422, str(exc))
```

### 7.3 Why services must not know about HTTP

It would be shorter for `crud.create_role` to raise `HTTPException(409, ...)` directly.
Here is the proof that it must not: `crud.py` has **two** callers. The REST router, and the
chat assistant's MCP tools (`app/services/chat_tools.py:76-90`):

```python
def _handler(fn):
    """Run a tool body in its own session, turning CrudError into a tool error."""
    async def wrapper(args):
        try:
            async with sessionmaker()() as session:
                result = await fn(session, args)
                await session.commit()
                return result
        except crud.CrudError as exc:
            return fail(str(exc))          # the MODEL reads this and adjusts
    return wrapper
```

Same exception, entirely different translation: the router produces a 409, the tool
produces text a language model reads and retries from. Were `crud` raising
`HTTPException`, the chat assistant would be catching web-framework exceptions to show an
LLM, and `exc.status_code` would be meaningless noise in its context. `crud.py:1-7` says so
directly.

> **Rule.** A layer must not name the protocol of the layer above it. The moment
> `services/` imports `fastapi`, it can only ever be called over HTTP.

### 7.4 The pure core

`app/services/graph.py` is 205 lines of real logic — graph validation, reachability,
termination analysis, fan-out versus branch semantics. Its imports, in full:

```python
from __future__ import annotations
import re
from dataclasses import dataclass, field
```

No FastAPI, no SQLAlchemy, no SDK, no `app.models`. It operates on frozen dataclasses
(`NodeSpec`, `EdgeSpec`), not ORM rows, and its docstring states the intent
(`graph.py:3-5`):

> Pure functions over light tuples so they can be unit-tested and reused by both the REST
> layer and the chat assistant's tools.

`security/` is the same, verified import by import:

```
path_guard.py   os, dataclasses, pathlib, typing  + its own siblings
policy.py       os, dataclasses, pathlib
tool_paths.py   dataclasses, typing
rules.py        pathlib                            + its own sibling
```

Zero infrastructure across the most security-critical code in the project, by design
(`path_guard.py:3-5`).

This matters for more than tidiness. A pure module is cheap to test, so it actually *gets*
tested exhaustively: `tests/test_path_guard.py` has 25 cases covering `..` traversal,
symlinked parents, symlinked roots, `/proj` versus `/proj-evil`, NUL bytes, `~` expansion
and every tool in the extractor table. Nobody writes 25 cases for something that needs a
database fixture.

### 7.5 The payoff, measured

Running the two halves of the suite separately:

| | Tests | Time | Per test |
| --- | --- | --- | --- |
| **Pure** (`test_graph`, `test_path_guard`, `test_options_builder`) | 65 | **0.62 s** | ~9.5 ms |
| **API** (`test_api`, `test_workflow_api`) | 46 | **3.07 s** | ~67 ms |

Seven times per test, and the pure figure is mostly pytest's own overhead. The API cost is
visible in the fixture (`tests/test_api.py:12-31`): per test it reloads three modules,
builds a fresh app, runs the full lifespan (schema creation, pragmas, migrations) and opens
an HTTP client — `--durations` shows `0.15s setup`, `0.07s setup`.

The whole suite is **197 tests in 6.9 s**, and the slowest single test is 1.00 s:
`test_gate.py::test_timeout_denies`, which genuinely sleeps because it is asserting a
timeout. That speed is not an accident of project size; it follows from most logic being
callable as a plain function.

### 7.6 Worked example: one rule change, two layers

A real episode from this repo, worth keeping because it shows what the layering is *for*.

The graph rules were changed to LangGraph's superstep model, documented in the module
docstring (`graph.py:7-12`): a node either **fans out** (every edge unconditional, all
taken in parallel) or **branches** (every edge conditional or default), and mixing the two
is rejected. Three tests still encoded the previous rule, and failed.

What the API test reported:

```
>       assert bad.status_code == 422
E       assert 200 == 422
```

Almost no information. A request expected to be rejected succeeded — somewhere across
routing, Pydantic, the router, `crud`, `graph` or the database.

What the pure test reported, from two lines of setup and no infrastructure:

```python
with pytest.raises(GraphError, match="every edge needs a condition"):
    validate(nodes("a", "b", "c"), [edge("a", "b", "x"), edge("a", "c", "y")])
```

`validate` did not raise. The question is immediately narrow: *why does `validate` accept
two unconditional edges from one node?* Answer: because that is now a fan-out, deliberately
legal — `fans_out` returns `True` (`graph.py:74-77`), and the rejection only fires on a
genuine mix (`graph.py:179-187`):

```python
plain     = [e.label for e in out if not e.conditional and not e.is_default]
described = [e.label for e in out if e.conditional or e.is_default]
if plain and described:                    # only when BOTH are non-empty
```

So the tests were stale, not the code. The fix was to invert the test, add the missing
fan-out coverage, and give the API tests a graph that is actually invalid under the new
rules — which is what `test_several_unconditional_edges_are_accepted_as_a_fan_out` and
`test_mixing_conditional_and_unconditional_edges_is_rejected` now do.

**The lesson.** Both layers failed for one reason. The pure test localised it to a single
function in under a second; the API test reported a wrong status code. Both are worth
having — but note which one you want first, and note how much harder the API failure would
have been to diagnose alone.

### 7.7 Which layer does a test belong in?

| Test this at… | When checking… | Example |
| --- | --- | --- |
| **Pure core** | a rule, a calculation, a decision | "a mixed node is rejected" |
| **Service** | orchestration across entities | "inline team creates roles on the fly" |
| **API** | the HTTP contract | "a duplicate name returns 409" |

The mistake to avoid is testing a *rule* through HTTP. If the API tests were the only
coverage of graph validation you would have slow tests reporting status codes instead of
fast tests reporting reasons. Test the rule where the rule lives; test at the API layer
that the rule is *wired up*. That is exactly what
`test_mixing_conditional_and_unconditional_edges_is_rejected` does — one test for the
`GraphError` → 422 translation, with the exhaustive cases in `test_graph.py`.

### 7.8 The honest smudges

**Routers issue their own read queries.** `app/routers/workflows.py:22-29`:

```python
rows = (
    await session.execute(
        select(Workflow).options(*crud.workflow_loaders()).order_by(Workflow.name)
    )
).scalars().all()
```

The router knows `select()` and `Workflow`, so "routers do HTTP only" is bent. It is
defensible — a simple list read does not earn a service function, and it borrows
`crud.workflow_loaders()` so the eager-loading rules stay in one place. But note the
asymmetry: **writes go through `crud` without exception, reads often do not.** A reasonable
line, worth drawing consciously.

**`routers/workflows.py:32-60` holds a data blob.** The `/examples` endpoint returns
hardcoded workflow shapes — content, not HTTP. `app/services/catalog.py` already exists for
exactly this kind of seeded list (it is where `DEFAULT_ROLES` lives, used at
`people.py:28-31`).

**`crud.py` is 500+ lines.** One module for roles, teams, MCP servers, models, tasks and
workflows. Fine at this size, and the section banners keep it navigable; it splits by
entity when it stops being findable.

---

## 8. Streaming

### 8.1 Why SSE, and the split that makes it work

`DESIGN.md` § 0:

> Streaming: SSE (`EventSource`) with `Last-Event-ID` resume — One-way server→browser;
> survives reload. **Control actions (cancel) are plain REST.**

The second sentence is the decision. A run needs a **data plane** (a firehose of events,
server to browser) and a **control plane** (cancel, answer an approval — rare, needs a
response). WebSockets would carry both over one socket, and you would then hand-roll a
message envelope, request/response correlation ids, reconnection with backoff, and
replay-after-gap — all of which HTTP already has.

So the firehose is SSE and the control actions are ordinary `POST` endpoints
(`app/routers/runs.py:158`, `174`). SSE also gives two things free that are annoying to
build: automatic reconnection, and `Last-Event-ID` — on reconnect the browser returns the
id of the last event it received, so the server knows where to resume.

> **Rule.** Do not make a problem bidirectional because one direction is busy. Match the
> transport to the dominant flow and use plain HTTP for the rest.

### 8.2 The wire format

The entire encoder (`runs.py:88-89`):

```python
def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
```

| Field | Effect in the browser |
| --- | --- |
| `id: 42` | stored; sent as the `Last-Event-ID` header on reconnect |
| `event: tool_use` | dispatched to `source.addEventListener("tool_use", …)` |
| `data: {…}` | the payload, as `message.data` (a string — you parse it) |
| `\n\n` | **frame terminator.** Omit it and the browser buffers forever |

Plus one special form (`runs.py:142`): a line beginning `:` is a comment the browser
ignores, used purely to put bytes on the wire (§ 8.8).

### 8.3 The EventBus: sequence, persist, fan out

```python
# app/services/events.py:56-72
async def emit(self, type_: str, payload: dict) -> dict:
    async with self._lock:
        self._seq += 1                                  # 1. sequence
        event = {"seq": self._seq, "type": type_,
                 "ts": datetime.now(timezone.utc).isoformat(), "payload": payload}
        self._buffer.append(event)
    await self._persist_one(event)                      # 2. persist
    for queue in list(self._subscribers):               # 3. fan out
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
    return event
```

**The envelope is generic**: `{seq, type, ts, payload}`. The bus knows nothing about
assistant text, tool calls or workflow nodes — which is what § 8.10 pays off.

Persistence and replay are **injected as callables**, not inherited (`events.py:21-22`,
`28-36`). Runs and chats share the bus through two factory functions supplying different
closures: `run_event_bus` (`events.py:108-132`) writes `RunEvent` rows; `chat_event_bus`
(`events.py:135-161`) writes `ChatMessage` rows, because "the chat transcript is also its
event log, so one table serves both". No abstract base class, no subclassing. In Python the
strategy pattern is usually just a function parameter.

`chat_event_bus` also takes `start_seq`, because a revived chat must continue its numbering
— restarting at 1 would collide with stored rows and corrupt replay.

### 8.4 Persist before publish

The database write happens **before** the fan-out, and that ordering is the guarantee:

> If a client has seen event N, it can always ask for N+1 later and get it.

By the time any subscriber could see event N, N is already in SQLite. This is why
`POST /api/chat` can emit the `"user"` event before the browser has opened the stream
(`app/services/chat.py:290`) with zero subscribers, and nothing is lost.

> **Rule.** "Nobody is listening yet" and "the listener reconnected" are the same case.
> Persist first and both work.

### 8.5 Two-tier replay

```python
# events.py:74-80
buffered = [e for e in self._buffer if e["seq"] > after_seq]
oldest = self._buffer[0]["seq"] if self._buffer else None
if oldest is not None and after_seq + 1 >= oldest:
    return buffered
return await self._replay_stored(after_seq)
```

A `deque(maxlen=2000)` (`events.py:41-43`) serves the common case from memory; the table is
the fallback for a client that has been away too long. The line worth staring at is
`after_seq + 1 >= oldest` — *"is the very next event I need still in the buffer?"* Get this
off-by-one wrong and you silently drop events, which is the worst kind of bug because the
stream still looks healthy.

### 8.6 Three kinds of stream

**A live run** (`runs.py:133-149`) — subscribe to the bus, replay the gap, then loop.

**A finished run** (`runs.py:109-126`) — read from the table, then `yield "event: _eof"`.

**A chat** (`app/routers/chat.py:125-128`) — *always* live, because it attaches rather than
looks up:

```python
try:
    chat = await manager.attach(chat_id)
except KeyError as exc:
    raise HTTPException(404, "chat not found") from exc
```

That asymmetry is the fix recorded in `DESIGN.md` § 5a.4. A finished run is finished, so
terminating its stream is correct; an open chat is never finished, it is waiting for the
next message. An earlier version served a not-in-memory chat from the table and closed with
`_eof`, so after a `--reload` restart the browser reconnected, got nothing, and sat forever
on a chat frozen at "thinking".

**So `_eof` means "deleted or shutting down", never "not found in memory".**

The sentinel carries `seq: -1` (`events.py:24`), a value no real event can have, which is
why the client can guard with one comparison (`app/static/views/runs.js:181`):

```js
if (event.seq < 0 || seen.has(event.seq)) return;
```

### 8.7 The client side

```js
// runs.js:139-141
const lastSeq = detail.events.length ? detail.events[detail.events.length - 1].seq : 0;
const source = new EventSource(`/api/runs/${runId}/events?last_event_id=${lastSeq}`);
const seen = new Set(detail.events.map((e) => e.seq));
```

**Why `last_event_id` is also a query param:** `EventSource` cannot set headers, and sends
`Last-Event-ID` only on *re*connects. On the first connection there is no id to send — but
the client has already fetched events over `GET /api/runs/{id}`, so it passes its position
explicitly. The server reads both, header winning (`runs.py:100-101`).

**The double dedupe.** Between the detail fetch returning events up to N and the
`EventSource` connecting, the run may emit N+1 and N+2. Two mechanisms cover the race:
`last_event_id=N` makes the server replay the gap, and the `seen` Set drops anything that
arrives twice.

**Scroll behaviour** (`runs.js:183-186`): auto-scroll only when already at the bottom, so
new output does not yank the view while reading scrollback.

**Cleanup** (`runs.js:194`): the view returns `() => source.close()`, which `app.js:30`
calls on navigation. Without it every panel switch leaks an open connection *and* a
server-side queue.

**Connection state must be visible** (`app/static/views/chat.js:228-238`):

```js
source.onopen = () => setConnection("open");
source.onerror = () => {
  // EventSource retries on its own unless the connection is CLOSED. Either way the
  // user should see it: a silently dead stream looks exactly like a stuck chat.
  setConnection(source.readyState === EventSource.CLOSED ? "closed" : "retrying", …);
};
```

This is the lesson SSE teaches the hard way: a dead stream and a slow server look
identical. Without surfacing the state, every network blip looks like a hung agent.

### 8.8 Heartbeats and the things that eat your stream

```python
# runs.py:151-155
return StreamingResponse(
    live_stream(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
)
```

| Measure | Stops |
| --- | --- |
| `HEARTBEAT_SECONDS = 15` + `": keep-alive"` | proxies and browsers killing an idle connection |
| `X-Accel-Buffering: no` | **nginx buffering the stream** until its buffer fills |
| `Cache-Control: no-cache` | an intermediary caching a response that never ends |

`X-Accel-Buffering` is the one that costs a day: everything works locally, then behind
nginx events arrive in bursts minutes late, because nginx is buffering a response it takes
for a file.

Note *how* the heartbeat is implemented (`runs.py:140-143`): `wait_for(queue.get(), 15)`
and yield a comment on `TimeoutError`. The timeout is not error handling — **the timeout is
the feature**, giving a periodic tick with no second task and no timer.

### 8.9 A pure translator

`app/services/messages.py` extracts SDK-message interpretation into a pure module
(`messages.py:1-6`):

> Shared by the flat task runner and the workflow runner so both streams look the same in
> the UI. **Pure functions with no bus or DB involvement**, which makes them easy to test
> against recorded messages.

```python
# messages.py:55-103
def translate(message: Any, node: str | None = None) -> list[Event]:
    events: list[Event] = []

    def add(type_: str, payload: dict) -> None:
        if node is not None:
            payload = {**payload, "node": node}
        events.append((type_, payload))
```

Four things to notice:

* **It returns a list.** One SDK message can produce several events — an
  `AssistantMessage` may carry text, thinking and two tool-use blocks. The mapping is
  one-to-many and the signature says so.
* **It returns tuples, not emitted events.** `Event = tuple[str, dict]`. The function
  decides *what* the events are; the caller decides what to do with them, so
  `runner.py:178-180` is three lines of plumbing:

  ```python
  for type_, payload in messages.translate(message):
      await self.bus.emit(type_, payload)
  self._output.extend(messages.collect_text(message))
  ```

* **The `node` parameter.** The local `add` injects `{"node": key}` into every payload when
  a node is given. The flat runner passes nothing; the workflow runner passes the current
  step, so workflow output arrives pre-tagged for grouping.
* **Truncation is centralised** (`messages.py:22-24`): `TOOL_RESULT_LIMIT`,
  `THINKING_LIMIT`, `TOOL_INPUT_LIMIT`. Both runners get identical limits from one place.
  Previously `runner.py` and `chat.py` each carried their own `_truncate_input` and
  `_stringify` — two copies of the same logic is two places to fix a truncation bug.

### 8.10 The vocabulary grows; the transport does not

The workflow feature added a new class of events (`runs.js:159-163`):

```
workflow_started · node_started · node_finished · edge_taken
workflow_stopped · workflow_finished
```

How much streaming infrastructure changed to support them? `events.py`: nothing.
`routers/runs.py`: nothing. `_sse()`: nothing. Replay, persistence, heartbeats, dedupe and
`Last-Event-ID`: unchanged. The only edits were `workflow_runner.py` calling
`bus.emit("node_started", …)`, and `runs.js` adding the names to its listener list and
`eventLine`'s switch.

That is because the envelope is generic and `type` is just a string.

> **Rule.** Design the *envelope*, not the messages. A transport that enumerates its
> payload types must be edited every time the domain grows.

### 8.11 Honest notes

**The client hand-maintains a list of event type names.** `runs.js:159-163` enumerates
fifteen; emit a new type server-side and it silently will not render until someone adds it.
This is forced by the API rather than sloppy — **`EventSource` has no wildcard listener** —
and it is a real choice:

| Approach | Cost |
| --- | --- |
| Set `event: <type>` | per-type listeners, but the client must enumerate them |
| Omit `event:` | one `onmessage` catches everything; switch on `payload.type` yourself |

This project picked the first. If the type list keeps growing, the second becomes cheaper.

**`source.onmessage` is effectively dead code.** Both `runs.js:152` and `chat.js:211`
register it alongside the per-type listeners, but `onmessage` fires only for frames with no
`event:` field, and `_sse()` always sets one. Harmless — and `seen` would dedupe anyway —
but it looks like a live path. It would become the *only* path under the second option
above.

**The finished-run branch uses the request's session inside a generator.**
`finished_stream` (`runs.py:109-116`) reads through the `Depends(get_session)` session, but
the generator body runs *after* the route function returns. FastAPI keeps yield-dependencies
open until the response completes, so this works — worth verifying if the pattern is ported
elsewhere. Contrast the live branch, which never touches `session`: `bus.replay` →
`_replay_stored` opens its own (`events.py:122`), making that path session-independent by
construction.

---

## 9. Security

The permission boundary is the part of this codebase where the reasoning is least
guessable, so `DESIGN.md` § 4 documents it at length. This section is about the *method*
behind it, which transfers to any safety-critical code.

### 9.1 Measure the framework; do not reason about it

`DESIGN.md` § 4.1 records eight live probes against a throwaway project. Two results decide
the entire architecture, and neither is guessable from the documentation:

| Probe | Setup | Result |
| --- | --- | --- |
| **P5** | a `PreToolUse` hook callback **raises** | **the tool ran anyway** — fail-**open** |
| **P6** | a `can_use_tool` callback **raises** | **denied** — fail-**closed** |

Two callbacks in the same library, failing in opposite directions. Any design that assumed
symmetry would have a hole in it.

> **Rule.** For every safety check you write, ask *"what happens if this check itself
> crashes?"* — then write a test that crashes it. Do not reason about it; the answer is a
> property of someone else's code.

Other probes settled equally load-bearing points: an `allowed_tools` entry means
`can_use_tool` is never consulted (P3), while the hook still fires; `Path.resolve()` exposes
a symlinked escape and macOS's `/tmp` → `/private/tmp` means the **root itself** must be
realpath'd (P4); `permission_mode="dontAsk"` denies even in-root writes (P7); and
`can_use_tool` *is* invoked for in-process MCP tools, with `readOnlyHint` granting no
exemption (P8).

### 9.2 The three load-bearing properties of the gate

`app/security/gate.py:1-15` lists them in the module docstring, and each has a named test in
`tests/test_gate.py`.

**One — the hook converts every exception into a deny** (`gate.py:148-161`):

```python
except BaseException as exc:            # P5: a raise here would FAIL OPEN
    reason = f"harness guard internal error: {exc!r}"
    try:
        await self._audit(..., verdict=path_guard.Verdict("deny", reason), ...)
    except BaseException:               # auditing must never unblock a denial
        pass
    return _deny_hook(reason)
```

Note the nested `except`: if the database is down, the audit write fails too, and that must
not prevent the denial. `test_hook_denies_even_when_auditing_also_fails` pins it.

**Two — the allow path returns `{}`, never an `"allow"` decision** (`gate.py:147`). `{}`
means "no opinion", so Layer A's deny rules still apply underneath. Returning `allow` would
also skip `can_use_tool` entirely, discarding the fail-closed layer.

**Three — `can_use_tool` has no `try`/`except`** (`gate.py:171`). Deliberately. By P6 an
exception there is *already* a denial; catching it would convert a fail-closed path into
whatever the handler decided.

That third one is the counter-intuitive one worth sitting with: **adding error handling
would make the system less safe.**

### 9.3 Layers chosen for complementary failure modes

| Layer | Mechanism | Fails how |
| --- | --- | --- |
| **A** | `permissions.deny` rules in the settings blob (`app/security/rules.py`) | evaluated by the CLI before anything reaches a callback — holds even if both callbacks are broken |
| **B1** | `PreToolUse` hook | sees **every** call, including inside subagents and pre-approved ones — but fail-open, so it is wrapped (§ 9.2) |
| **B2** | `can_use_tool` | skipped when a rule already allows — but fail-closed |
| **C** | `setting_sources=[]` | a `.claude/settings.local.json` in the target project cannot pre-approve tools |
| **D** | OS sandbox (`sandbox-exec` / bubblewrap) | the only real containment for `Bash` |

The point is not "more layers is better". It is that **B1 and B2 fail in opposite
directions**, so the combination has no single failure mode. `DESIGN.md` § 4.4 tabulates
which layer sees what: an in-root `Read` is auto-allowed by the CLI and so reaches *only*
the hook, which is precisely why the hook's fail-open behaviour had to be neutralised rather
than tolerated.

Layer A also closes a hole no containment check can see (`rules.py:60-63`):

```python
# Inside the project root but able to execute later. These paths pass the
# containment check by definition, so only a rule can stop them.
for inside in (".git/hooks", ".claude"):
    rules.append(f"Edit({_abs(policy.root / inside)}/**)")
```

`<root>/.git/hooks/pre-commit` is legitimately inside the project. Path containment will
always allow it. Only a deny rule stops a session writing code that executes later.

And note what Layer A deliberately omits: **there is no `allow` list** (`rules.py:3-6`). An
allow rule would shadow `can_use_tool` exactly as a whole-tool `allowed_tools` entry does.

### 9.4 Default-deny, expressed as an allowlist

Two places, same principle.

**Unknown tools are denied** (`app/security/tool_paths.py:57-61`):

```python
def classify(tool_name: str) -> ToolSpec:
    if tool_name.startswith(MCP_PREFIX):
        return ToolSpec("mcp")
    return TOOL_TABLE.get(tool_name, ToolSpec("unknown"))    # -> deny
```

**Unclassified chat tools are confirmed** (`app/services/chat_tools.py:34-38`):

```python
#: Gating is expressed as an allowlist of read-only tools rather than a denylist of
#: mutating ones, so a tool added later without being classified is confirmed by
#: default instead of slipping through unconfirmed.
READ_ONLY_TOOLS: frozenset[str] = frozenset({...})
```

And `is_mutating` returns `True` for anything outside the harness namespace
(`chat_tools.py:58-65`) — unreachable while `tools=[]` holds, and correct the day it does
not.

`DESIGN.md` § 5a.2 records that the first version had this inverted and a test caught it.

> **Rule.** Default-deny means new code is safe by construction. A denylist requires every
> future contributor to remember to update it.

### 9.5 Path canonicalisation: four mistakes, avoided explicitly

```python
# 1. realpath, not merely absolute — /tmp is a symlink on macOS (P4)
resolved = Path(os.path.realpath(root))                      # policy.py:35

# 2. non-existent targets: realpath the deepest existing ancestor, re-attach the tail
while not anc.exists(): tail.append(anc.name); anc = anc.parent
return Path(os.path.realpath(anc)).joinpath(*reversed(tail)) # path_guard.py:48-68

# 3. components, not string prefixes
return path == root or root in path.parents                  # path_guard.py:71-77
#    "/proj-evil".startswith("/proj") is True. This is not.

# 4. NUL truncation: inspect one path, syscall sees a shorter one
if any("\x00" in p for p in raw_paths): return deny(...)      # path_guard.py:96-97
```

Number 2 is the subtle one: a file being *created* does not exist yet, so `resolve()` alone
cannot canonicalise it — but its **parent** may be a symlink out of the project. Resolving
the deepest existing ancestor and re-attaching the remaining tail catches that.

And a fifth, of a different kind (`policy.py:44-48`): a project path containing `()[],` is
**rejected at creation time**, because it cannot be expressed unambiguously in the
`Tool(pattern)` rule grammar of Layer A.

> **Rule.** When a value gets embedded in another language — a rule string, SQL, a shell
> command, HTML — either escape it properly or refuse it. Never emit something you know is
> malformed.

The same class of bug appears elsewhere: `app/services/skill_discovery.py:119` rejects
archive members that are absolute or contain `..`, because `zipfile.extractall` will
otherwise happily write outside the destination.

### 9.6 Make it impossible, not merely forbidden

The system prompt tells the chat assistant never to invent an API key. That is a request.
These are guarantees:

* **`add_model_config` has no key parameter** (`chat_tools.py:289-318`). The assistant
  cannot set a credential because there is nowhere to put one.
* **No delete-shaped tool exists** in `ALL_TOOLS` (`chat_tools.py:375-391`), and a test
  asserts it.
* **`tools=[]`** (`app/services/chat.py:237`) — the session has no `Read`, `Write`, `Edit`
  or `Bash` at all. Verified against the `init` event: its tool list contains only
  `mcp__harness__*`.
* **`_assert_safe`** runs on every built option set (`app/services/options_builder.py:33-49`),
  rejecting `permission_mode` values that bypass prompts, any whole-tool `allowed_tools`
  entry, `skills="all"`, a missing `can_use_tool`, or a missing `PreToolUse` hook.

`_assert_safe` is not defending against an attacker. It defends against a future refactor
that adds a convenience flag, which is the more likely threat.

> **Rule.** Prefer removing the capability over instructing something not to use it. A
> missing parameter cannot be prompt-injected.

### 9.7 Verify that the configuration actually applied

```python
# app/services/runner.py — _preflight()
"""The settings blob is interpreted by the CLI, which silently ignores
settings that fail validation in headless mode — so a typo could remove the
deny rules with no error anywhere."""
```

The session's `init` event is treated as a contract: `cwd` equals the realpath'd root, the
permission mode is what was requested, no network tools leaked, no unexpected MCP servers
loaded. A mismatch aborts the run as `preflight_failed` before any tool can execute.

> **Rule.** If you hand configuration to something that might ignore it, ask it back what it
> believes the configuration is.

### 9.8 Audit, then reconcile against an independent source

Every decision is written to `permission_decisions` with its layer, tool, `tool_use_id`,
`agent_id`, resolved paths and reason. Then at the end of the run, `_reconcile_denials`
compares the harness's own denial log against the SDK's `ResultMessage.permission_denials`.

A denial the CLI reports that the gate never recorded means something was blocked by a
mechanism outside the gate — a deny rule, or the CLI itself. That is surfaced as an event
rather than silently trusted.

> **Rule.** A log you never cross-check is a log you cannot trust.

### 9.9 One gate, many concurrent sessions

The workflow runner changed the concurrency picture, and the security layer absorbed it
without modification — worth understanding why.

`Run` constructs the policy and the gate **once** (`runner.py:47-60`) and hands both to the
workflow runner (`runner.py:157-162`):

```python
runner = WorkflowRunner(self.snapshot, self.policy, self.gate, self.bus, ...)
```

A superstep then runs its whole frontier **concurrently** (`workflow_runner.py:413`, bounded
by `Semaphore(MAX_PARALLEL_STEPS)` at `:157`). So a single `Gate` instance is now shared by
several simultaneously-running SDK sessions, each with its own hook and callback traffic.

This works because of the re-entrancy rule in § 4.2: `Gate.pre_tool_use` keeps everything in
locals, and the only shared mutable state is `self.pending`, keyed by uuid. What was
originally a precaution against *concurrent hook dispatch within one session* turned out to
be exactly what *concurrent sessions* needed. The workflow runner's docstring states the
intent plainly:

> Every step runs through the same `Gate` as a flat task run, so path confinement and
> approval prompts apply identically.

One consequence worth naming: `RunPolicy.add_session_root` (`policy.py:71-76`) widens
`write_roots` for the rest of the run, so approving "Allow this directory for the run" in one
step also widens it for steps running **concurrently** in the same superstep. That matches
the semantics the button states, and it is per-run and never persisted — but it is a
deliberate scope, not an accident.

> **Rule.** Code with no per-call state on `self` scales from concurrent callbacks to
> concurrent sessions for free. Code that caches request state on the instance does not.

### 9.10 State the residual risks

`DESIGN.md` § 4.9 and the README list what is *not* secured, with the mitigations that limit
each:

* **Reads are unconfined by decision** — an agent that cannot read a sibling library or a
  system header is useless for real work. Limited by the credential deny rules, network
  tools off by default, and the read audit trail.
* **A trusted MCP server bypasses the path boundary** — its tool inputs have server-defined
  schemas the harness cannot inspect, so untrusted servers' tools are denied outright and
  trusting one is an explicit UI action.
* **TOCTOU** — a path is resolved, then the tool writes moments later. The OS sandbox covers
  `Bash`; for `Write`/`Edit` the window is small, real, and not closable in-process.
* **Hook fail-open** remains the sharpest edge, mitigated by § 9.2 and its test, with
  `paranoid_mode` as the config-level fallback.

> **Rule.** Undocumented residual risk is indistinguishable from an oversight. Writing it
> down is what makes it a decision.

---

## 10. Exercises

Each one is a deliberate break; predict the outcome, then try it.

### 10.1 Concurrency

1. In `RunManager.start`, drop the `self._tasks[run_id] =` assignment so the line reads
   just `asyncio.create_task(...)`. What is the bug, how often would it bite, and why is
   it nearly impossible to reproduce on demand?
2. Change `queue.put_nowait(event)` to `await queue.put(event)` in `events.py:69`. One
   browser tab stops reading — what happens to the agent, and to every *other* tab?
3. Remove `raise` from the `except asyncio.CancelledError` in `_supervise`. Cancel a run,
   then shut the server down. What do you observe?
4. In `_request_approval`, move `self.pending.pop(request.id, None)` out of `finally` and
   onto the line after the `try` block. Which path now leaks, and what does the user see
   in the pending-approvals badge?
5. `Gate.resolve` is `def`, not `async def`. What would have to be true for it to *have*
   to be async? (What does `set_result` actually do?)
6. Set `TURN_TIMEOUT_S = 60` and leave `CONFIRM_TIMEOUT_S = 300`. Walk through what a user
   experiences when they take two minutes to answer a confirmation, which error message
   they get, and why it is misleading.

### 10.2 The ORM

1. Remove `lazy="selectin"` from `Team.members` and run the suite. Which tests fail, and
   what is the exact error? Now fix it by adding `selectinload` to the failing query
   instead — the same job done two ways.
2. Change `db.py:26` to `expire_on_commit=True`. Predict *before running*: does
   `POST /api/roles` fail? Where exactly, and why does `POST /api/teams` behave
   differently?
3. `crud.py:253-254` has two entries beginning `selectinload(Workflow.nodes)` and
   `selectinload(Workflow.edges)`. Why can that not be one chained expression? What does
   chaining mean that listing does not?
4. Delete `.execution_options(populate_existing=True)` from `load_task`, then write a test
   that fails because of it — you will need to load a task, modify it, and re-load it
   within one session.
5. `WorkflowEdge.to_node_id` is nullable and means END. What breaks if you instead create a
   real `WorkflowNode` row with `key="END"`? Consider
   `UniqueConstraint("workflow_id", "key")` and the `NOT NULL` on `role_id`.
6. `Task.team_id` uses `ondelete="RESTRICT"` but `TaskRun.task_id` uses
   `ondelete="SET NULL"`. Justify each in one sentence, then say what goes wrong if they
   are swapped.
7. `_add_missing_columns` refuses to add a `NOT NULL` column with no default. Add such a
   column to a model, start the app against an existing database, and find the warning in
   the log. What are your options for shipping that column for real?

### 10.3 Layering

1. Pick any endpoint in `app/routers/tasks.py` and count how many lines are HTTP concerns
   versus logic. Is anything there that `crud` should own?
2. Move the `/examples` blob out of `routers/workflows.py` into `services/catalog.py`,
   following how `DEFAULT_ROLES` is used at `people.py:28-31`. Does any test change? What
   does that tell you?
3. Add `from fastapi import HTTPException` to `app/services/graph.py` and raise that instead
   of `GraphError`. Now call `validate` from a chat tool. What is wrong with what the model
   receives?
4. Write a test for one graph rule twice — once in `test_graph.py` calling `validate`
   directly, once in `test_workflow_api.py` over HTTP. Time each with `--durations`. If you
   could keep only one, which, and why?
5. `security/path_guard.py` imports `app.security.policy`. Is that a layering violation?
   What makes an intra-layer import different from a cross-layer one?
6. Sketch `crud.py` split into `crud/roles.py`, `crud/teams.py`, `crud/workflows.py`. What
   breaks? Look at `materialise_inline_team` (`crud.py:147-171`) — which files would it need
   to import?

### 10.4 Streaming

1. Delete the final `\n` from `_sse`, leaving one instead of two. What does the browser
   show, and why is this a uniquely nasty bug to debug?
2. Set `HEARTBEAT_SECONDS = 600`, start a run and leave it idle. Nothing breaks locally —
   explain why, and what would break behind nginx or a corporate proxy.
3. Change `events.py:41` to `deque(maxlen=3)`, run a task, and reload mid-run. Which branch
   of `replay` serves you? Add a print to each to confirm.
4. Remove `return () => source.close()` from `runs.js`. Navigate between Runs and Tasks ten
   times with a live run. How many subscriber queues exist, and what is `emit` doing with
   them?
5. `translate()` returns `list[Event]` rather than emitting directly. Write a test for the
   `node` tagging that needs no bus, no database and no app — that is the property this
   design buys.
6. Add a `node_retried` event to `workflow_runner.py` and display it. List every file you
   touched. Now imagine the bus knew about event types — which files would join that list?
7. In `append`, the guard is `event.seq < 0 || seen.has(event.seq)`. Why must the `_eof`
   sentinel carry a negative seq rather than `0`? Consider what `start_seq=0` means for a
   fresh bus.

### 10.5 Security

1. Delete the `except BaseException` block from `Gate.pre_tool_use` and let the exception
   propagate. Which test fails? Now reason about the real consequence: with
   `allowed_tools=["Write"]` and a guard that raises, what reaches the filesystem?
2. Change the hook's allow path from `return {}` to an explicit `"allow"` decision. Nothing
   in the suite may fail — explain the hole you just opened, and which layer stopped being
   consulted.
3. Wrap `can_use_tool` in `try/except Exception: return PermissionResultAllow()`. Explain
   why this "defensive" change is the most dangerous edit in the file.
4. Add a tool named `Reticulate` to a hook payload without adding it to `TOOL_TABLE`. What
   verdict comes back, and from which line?
5. Add a new tool to `chat_tools.ALL_TOOLS` and do **not** add it to `READ_ONLY_TOOLS`. Does
   it run unconfirmed? Now invert the design in your head — a `MUTATING_TOOLS` denylist —
   and answer the same question.
6. `resolve_candidate` realpaths the deepest *existing* ancestor. Construct a case that
   escapes the root if it instead realpaths only the full path: create `<root>/link`
   pointing outside, then target `<root>/link/new.txt`.
7. Create a task whose project path contains a comma. Which layer rejects it, with what
   message, and what would the generated deny rule have looked like if it had not?
8. `_assert_safe` rejects `skills="all"`. Read `DESIGN.md` § 3.7 and explain what `"all"`
   injects and why that shadows the callback.
9. In a workflow superstep with three concurrent nodes, one triggers an out-of-root write
   and you click "Allow this directory for the run". What happens to the other two nodes'
   write boundary? Find the lines that decide it.
10. Give `Gate.pre_tool_use` a per-call attribute — `self._current_tool = tool_name` — and
    use it later in the method. Describe the failure mode under a three-node superstep, and
    why no test would reliably catch it.
