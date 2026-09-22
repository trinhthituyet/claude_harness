# Control Panel for Claude Code Sessions — Design Proposal

Status: proposal, no application code written yet.
Target of the proposal: the four items in `docs/PROMPT.md` § "Before Writing Code, Propose".

Trigger mechanism: **the Claude Agent SDK (Python), run in-process**.

Everything in § 4 (and the SDK facts in § 3) was verified empirically against
`claude-agent-sdk` 0.2.157 on this machine, not assumed. Probe results are in § 3.1
and § 4.1. The § 4.1 probes are the reason this document recommends a different layering
than the obvious one — two of them contradict the intuitive reading of the API.

---

## 0. Stack decisions

| Choice | Decision | Why |
| --- | --- | --- |
| Backend | FastAPI + `uvicorn`, fully async | The SDK is async (anyio); one event loop avoids bridging |
| Agent runtime | `claude-agent-sdk`, `ClaudeSDKClient` per run | Per the requirement. In-process permission callbacks and hooks |
| ORM | SQLAlchemy 2.0 async + `aiosqlite` | 8 tables of plain CRUD, one event loop throughout |
| Migrations | `create_all` + a `schema_version` table with hand-written steps | Single local user, no Alembic ceremony |
| Frontend | Plain HTML + ES modules + CSS, no build step | "Keep it lightweight". Two-panel layout is ~300 lines of DOM code |
| Streaming | SSE (`EventSource`) with `Last-Event-ID` resume | One-way server→browser; survives reload. Control actions (cancel) are plain REST |
| Python | 3.12 (SDK requires ≥3.10; system `python3` here is 3.8) | The project pins its own venv |

**What the SDK actually is.** It spawns the Claude Code CLI as a subprocess and speaks a
JSON control protocol over its stdio (`_internal/transport/subprocess_cli.py`). Two
consequences the design leans on: (a) anything expressible as a CLI settings file is
expressible through `options.settings`, including permission rules and sandbox config;
(b) the SDK ships a bundled `claude` binary but honours `options.cli_path`, so the harness
pins one known-good CLI and records its version on every run.

Setup note for this machine: public PyPI is proxy-blocked; `uv` with
`UV_INDEX_URL=https://pypi.apple.com/simple` works. The project ships a `uv`-based
`pyproject.toml` + lockfile.

---

## 1. Data model

SQLite, WAL mode, foreign keys ON. Timestamps are UTC ISO-8601 text.

```
skills                      mcp_servers                 model_configs
──────                      ───────────                 ─────────────
id           PK             id            PK            id              PK
name         UNIQUE         name          UNIQUE        name            UNIQUE
description                 transport     stdio|sse|http provider       anthropic|ollama|vllm|custom
status       installed      command       (stdio)       model_id        e.g. claude-opus-5
             |suggested     args_json     (stdio)       base_url        NULL for anthropic default
origin       discovered     env_json      (stdio)       api_key         nullable, see § 1.1
             |uploaded      url           (sse/http)    extra_env_json
             |suggested     headers_json  (sse/http)    is_default      bool
install_path (on disk)      scope         user|project  created_at
scope        user|project   status        installed|suggested
metadata_json (frontmatter) enabled       bool
enabled      bool           trusted       bool  ← gates MCP tool calls, § 4.7
created_at                  created_at

roles                       teams                       team_roles
─────                       ─────                       ──────────
id            PK            id          PK              team_id   FK→teams   ┐
name          UNIQUE        name        UNIQUE          role_id   FK→roles   ├ PK
description                 description                 position  int        ┘
system_prompt TEXT (req.)   created_at                  is_lead   bool
model_config_id FK NULL                                 (exactly one lead per team,
tools_json    NULL=inherit                               enforced in the service layer)
created_at
```

```
tasks                                  task_skills            task_mcp_servers
─────                                  ───────────            ────────────────
id               PK                    task_id FK ┐ PK        task_id FK ┐ PK
name                                   skill_id FK ┘          server_id FK ┘
prompt           TEXT   (required, non-empty)
project_path     TEXT   (required, absolute, validated — § 4.2)
team_id          FK→teams (required)
model_config_id  FK→model_configs (NULL ⇒ default config)
trust_project_settings  bool, default 0         ← § 4.5
sandbox_bash            bool, default 1         ← § 4.8
paranoid_mode           bool, default 0         ← § 4.9, permission_mode="dontAsk"
network_enabled         bool, default 0         ← WebFetch / WebSearch
approval_timeout_s      int, default 300        ← § 4.4, UI approval deadline
max_turns               int NULL
max_budget_usd          real NULL
created_at / updated_at
```

```
task_runs                                   run_events
─────────                                   ──────────
id                PK (uuid, also options.session_id)   id      PK
task_id           FK→tasks ON DELETE SET NULL  run_id  FK→task_runs ON DELETE CASCADE
status            queued|running|completed  seq       int   ← monotonic per run, = SSE event id
                  |failed|denied|cancelled  ts
sdk_version / cli_version                   type      assistant_text|thinking|tool_use|
options_json      the resolved options, secrets redacted   tool_result|permission|system|
settings_json     the generated settings blob (§ 4.3)      result|error|status
prompt_snapshot   TEXT                                payload_json
project_path_snapshot TEXT (the realpath'd root)      UNIQUE(run_id, seq)
team_snapshot_json    full role+prompt copy
model_snapshot_json   provider/model/base_url
skills_snapshot_json / mcps_snapshot_json
started_at / ended_at
exit_reason       success|error_max_turns|error_max_budget_usd|cancelled|preflight_failed|exception
num_turns / total_cost_usd / duration_ms
error_text
output_text       final assistant text, denormalised for the history list

permission_decisions        ← the audit log for the security boundary (§ 4)
────────────────────
id PK · run_id FK · ts · layer (hook|callback|rule)
· tool_name · tool_use_id · agent_id (subagent attribution)
· decision allow|deny · reason
· candidate_paths_json · resolved_paths_json · tool_input_json
```

`permission_decisions` is written by the hook and the permission callback, then
**reconciled** at the end of the run against `ResultMessage.permission_denials` — a
field the SDK does expose (`types.py:1355`), listing every denial from any source. A
denial present in the CLI's list but absent from ours means something was blocked by a
mechanism our code never saw, which is itself worth logging.

**Why snapshots.** A run must remain reproducible after the task, team, role prompts or
model config are edited or deleted. Everything the session was constructed from —
including the resolved options and the generated settings blob — is copied into
`task_runs` at launch. The FKs exist only for the "runs for this task" listing.

**Relationships:** `teams ↔ roles` many-to-many through `team_roles` (ordered, one lead);
`tasks → teams` many-to-one; `tasks ↔ skills` and `tasks ↔ mcp_servers` many-to-many;
`tasks → task_runs` one-to-many; `task_runs → run_events` one-to-many (the replay log).

### 1.1 Secrets

`model_configs.api_key` and `mcp_servers.env_json` hold credentials in plaintext SQLite.
For a single local user this matches the threat model, but the DB is created `0600`, the
API never echoes a stored key back (`GET` returns `"sk-ant-…abcd"`; `PUT` with the mask
means "unchanged"), and `options_json` / `settings_json` are redacted before storage.

---

## 2. FastAPI project structure

```
claude_harness/
  pyproject.toml            # uv-managed, python = ">=3.12", claude-agent-sdk pinned
  app/
    main.py                 # app factory, lifespan (db init, SDK/CLI probe, RunManager)
    config.py               # settings: db path, cli_path override, max concurrent runs
    db.py                   # async engine/session, create_all + schema_version steps
    models/                 # SQLAlchemy ORM
      skill.py  mcp.py  model_config.py  role.py  team.py  task.py  run.py
    schemas/                # Pydantic v2 request/response models (+ validators)
    routers/
      skills.py             # CRUD + POST /import (zip or SKILL.md upload) + GET /discover
      mcps.py               # CRUD + POST /test-connection
      models.py             # CRUD + POST /{id}/test  (one-token round trip)
      roles.py  teams.py    # CRUD; team creation validates ≥1 role + exactly one lead
      tasks.py              # CRUD + POST /{id}/run  + inline team creation
      runs.py               # GET list/detail, GET /{id}/events (SSE), POST /{id}/cancel
      fs.py                 # GET /fs/validate?path=…  and GET /fs/ls?path=…  (dir picker)
      catalog.py            # GET /catalog/skills, /catalog/mcps  (the "Suggested" sections)
    services/
      options_builder.py    # snapshot + policy → ClaudeAgentOptions  (§ 3.3)
      settings_builder.py   # generate the settings blob for options.settings (§ 4.3)
      runner.py             # RunManager, Run: connect, preflight, pump, cancel, finalize
      events.py             # per-run ring buffer + fan-out + persistence
      prompt_composer.py    # team + roles → system_prompt + agents={}  (§ 3.5)
      model_resolver.py     # model_config row → (model, env overrides)  (§ 3.6)
      skill_discovery.py    # scan ~/.claude/skills, <project>/.claude/skills, plugins
      skill_install.py      # validate + unpack an uploaded skill
      mcp_registry.py       # row → McpServerConfig dict
      catalog.py            # seeded suggestion lists, diffed against installed
    security/
      path_guard.py         # pure containment logic (§ 4.6). No FastAPI, no DB, no SDK
      tool_paths.py         # per-tool path extractor table
      policy.py             # RunPolicy: roots, read roots, trusted MCPs
      rules.py              # RunPolicy → permissions.deny rule strings (§ 4.3)
      gate.py               # the hook + permission callback, incl. the fail-closed wrapper (§ 4.4)
    static/
      index.html  app.js  views/{skills,mcps,models,roles,teams,tasks}.js  style.css
  tests/
    test_path_guard.py      # traversal, symlink, prefix, unicode, every tool in the table
    test_gate.py            # ← incl. "guard raises → hook still returns deny" (§ 4.4)
    test_rules.py           # rule-string generation + escaping/rejection
    test_options_builder.py # asserts no whole-tool allowed_tools entry ever escapes
    test_runner_fake_sdk.py # runner driven by a fake transport, no network
    test_api_*.py
  docs/PROMPT.md  docs/DESIGN.md
```

Layering rule: `routers` do HTTP + validation only; `services` hold behaviour; `security`
is imported by `services/runner.py` and by nothing that could weaken it.
`security/path_guard.py` has no dependency on FastAPI, the DB or the SDK, so it can be
exhaustively unit-tested.

### 2.1 API surface (abridged)

```
GET/POST/PUT/DELETE  /api/skills|mcps|models|roles|teams|tasks     standard CRUD
POST   /api/skills/import                 multipart: .zip or SKILL.md → ~/.claude/skills/<name>/
GET    /api/skills/discover               filesystem scan, reconciled into the skills table
GET    /api/catalog/skills|mcps           suggested (not-yet-installed) entries
GET    /api/fs/validate?path=…            {exists, is_dir, readable, writable, resolved}
GET    /api/fs/ls?path=…                  directory listing for the path picker
POST   /api/tasks/{id}/run                202 → {run_id}
GET    /api/runs?task_id=&limit=          run history
GET    /api/runs/{run_id}                 run detail + events + permission decisions
GET    /api/runs/{run_id}/events          text/event-stream, honours Last-Event-ID
POST   /api/runs/{run_id}/approvals/{req} {approved, remember, reason} — § 4.4a
POST   /api/runs/{run_id}/cancel          → ClaudeSDKClient.interrupt()
```

---

## 3. How the SDK session is constructed and run

### 3.1 Verified SDK facts

- `options.cwd` sets the subprocess working directory; the transport raises if it does not
  exist (`subprocess_cli.py:886`).
- `options.env` is merged **over** the inherited process environment
  (`subprocess_cli.py:812-816`) — the hook for per-run model/credential config.
- There is **no** `base_url` option; only `model` / `fallback_model` (§ 3.6).
- `options.settings` accepts a settings file path **or a JSON string**, and
  `options.sandbox` is merged into that same blob under `"sandbox"`
  (`subprocess_cli.py:466-516`). So permission rules and sandbox config are both reachable
  from Python.
- `hooks` are in-process async callbacks via `HookMatcher(matcher=…, hooks=[fn])`,
  dispatched **concurrently** per event.
- `can_use_tool` forces `permission_prompt_tool_name="stdio"` and is invoked only when the
  CLI's rules evaluate to *ask*.
- `ResultMessage` carries `permission_denials`, `total_cost_usd`, `num_turns`,
  `terminal_reason`, `subtype`.
- `skills=[names]` auto-sets `setting_sources=["user","project"]` when unset and injects
  `Skill(name)` entries into `--allowedTools`; `skills="all"` injects a **bare** `Skill`
  entry, which shadows `can_use_tool` (`subprocess_cli.py:520-561`).

### 3.2 Execution model

Async in-process, one `asyncio.Task` per run, inside the FastAPI event loop.

```
POST /api/tasks/{id}/run
  → validate task (prompt non-empty, team ≥1 role, project_path exists & is a dir)
  → insert task_runs row (status=queued) + snapshots
  → RunManager.spawn(run)      # asyncio.create_task, bounded by Semaphore(MAX_CONCURRENT)
  → 202 {run_id}               # the run outlives the HTTP request
```

```python
class Run:
    async def execute(self) -> None:
        policy = RunPolicy.from_snapshot(self.snapshot)          # § 4.2
        gate   = Gate(policy, self.audit, self.emit)             # § 4.4
        opts   = build_options(self.snapshot, policy, gate)      # § 3.3
        try:
            async with ClaudeSDKClient(options=opts) as client:
                self._client = client                            # cancel → interrupt()
                await client.query(self.snapshot.prompt)
                async for msg in client.receive_response():
                    if is_init(msg):
                        self.assert_config(msg, policy)           # § 3.4 preflight
                    await self.emit_message(msg)
        except Exception as exc:
            await self.fail(exc)
        finally:
            await self.reconcile_denials()                        # § 1 audit cross-check
            await self.finalize()
```

`emit()` assigns `seq`, appends to a per-run ring buffer (last 2000 events), persists to
`run_events`, then publishes to subscriber queues. Persist-before-publish means a
reconnecting `EventSource` with `Last-Event-ID: N` is served from the buffer, falling back
to the table.

`ClaudeSDKClient` is used rather than the one-shot `query()` because we need
`interrupt()`, `set_permission_mode()` and `get_mcp_status()`.

Message → event mapping: `AssistantMessage` → `assistant_text` / `thinking` / `tool_use`
per block; `UserMessage` with `ToolResultBlock` → `tool_result` (truncated to 8 KB in the
event, full text only in the transcript); `SystemMessage` → `system` (the `init` subtype
carries the session id); `ResultMessage` → `result`. `include_partial_messages=True` and
`forward_subagent_text=True` are enabled so the UI renders token-level streaming and
per-role subagent output.

Cancel → `client.interrupt()`. Shutdown cancels live runs, marks them `cancelled`, closes
clients.

### 3.3 Resolved options

```python
ClaudeAgentOptions(
    cwd              = str(policy.root),          # realpath'd, § 4.2
    add_dirs         = [],                        # reads are unconfined, § 4.6 — nothing to add
    permission_mode  = "dontAsk" if task.paranoid_mode else "default",   # § 4.9
    allowed_tools    = [],                        # deliberately empty — § 4.4
    disallowed_tools = policy.disallowed,         # e.g. WebFetch when network is off
    can_use_tool     = gate.can_use_tool,         # § 4.4, fails closed
    hooks            = {"PreToolUse": [HookMatcher(hooks=[gate.pre_tool_use])]},
    settings         = json.dumps(settings_blob),  # deny rules, § 4.3
    sandbox          = policy.sandbox,            # § 4.8 (merged into settings by the SDK)
    setting_sources  = policy.setting_sources,    # [] or ["user"] — § 4.5
    mcp_servers      = policy.mcp_servers,
    strict_mcp_config= True,
    skills           = policy.skills,             # explicit list, never "all"
    agents           = composed_agents,           # § 3.5
    system_prompt    = {"type": "preset", "preset": "claude_code", "append": lead_prompt},
    model            = model.model_id,
    env              = scrubbed_env | model.env_overrides,
    session_id       = run_id,
    max_turns        = task.max_turns,
    max_budget_usd   = task.max_budget_usd,
    include_partial_messages = True,
    forward_subagent_text    = True,
    cli_path         = config.cli_path,           # pinned, version recorded
    stderr           = self.capture_stderr,
)
```

Note `allowed_tools=[]` and `matcher=None`: both are load-bearing security choices,
explained in § 4.4.

### 3.4 Preflight — verifying the config applied

`options.settings` is a *file/JSON blob interpreted by the CLI*, and in headless mode the
CLI silently ignores settings that fail validation. So the `init` `SystemMessage` is
treated as a contract check, asserted on arrival:

| Field | Assertion |
| --- | --- |
| `cwd` | equals the realpath'd project root |
| `permissionMode` | equals the mode requested (`"default"`, or `"dontAsk"` in paranoid mode) |
| `tools` | contains no tool outside the task's allowed set |
| `mcp_servers` | exactly the servers configured (catches `strict_mcp_config` failure) |
| `skills` | exactly the selected set |
| `model` | the model requested |

A mismatch aborts the run as `preflight_failed` before any tool can execute. This is
cheap insurance; § 4.9 explains why it is not optional.

### 3.5 Roles and teams → system prompt + subagents

A team of N roles maps onto the SDK's native multi-agent support rather than N sessions:

- The **lead** role's `system_prompt` plus a team roster is passed as
  `system_prompt={"type": "preset", "preset": "claude_code", "append": …}`. Keeping the
  `claude_code` preset matters — without it the model loses the tool-use conventions the
  built-in tools expect.
- Every **non-lead** role becomes an entry in `options.agents: dict[str, AgentDefinition]`,
  with `description` = role description (what the lead sees when choosing whom to
  delegate to), `prompt` = the role's system prompt, `model` = the role's override or
  `"inherit"`.
- The task prompt is sent as the user turn, verbatim.

Subagent tool calls pass through the same gate — the hook payload carries `agent_id` /
`agent_type` for calls made inside a subagent (`_SubagentContextMixin`), so the boundary
covers delegation, and `permission_decisions.agent_id` attributes each decision.
`forward_subagent_text=True` lets the UI render each role in its own lane.

Single-role teams degenerate cleanly: `agents={}`, role prompt appended, no Agent tool use.

### 3.6 Models — Anthropic vs local (vLLM / Ollama)

**Anthropic**: `options.model = <model_id>`, `options.env = {"ANTHROPIC_API_KEY": key}`.
Dropdown seeded with `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`,
plus a free-text field.

**Local LLMs — flagged clearly, as the Models requirement asks.** The SDK has **no**
custom-base-URL option; there is no `base_url` / `baseUrl` anywhere in `types.py` or the
transport. The underlying CLI reaches Anthropic through `ANTHROPIC_BASE_URL`, and since
`options.env` is merged over the inherited environment we *can* point a session
elsewhere. The catch is protocol, not plumbing: the CLI speaks the **Anthropic Messages
API** (tool use, streaming, prompt caching). Ollama's native API and vLLM's
OpenAI-compatible API are not that wire format, so pointing `ANTHROPIC_BASE_URL` at
`http://localhost:11434` will not work.

Proposed handling, surfaced in the UI rather than hidden:

- `provider = "anthropic"` → `env: {ANTHROPIC_API_KEY}`.
- `provider ∈ {ollama, vllm, custom}` → the config stores the URL of an
  **Anthropic-API-compatible gateway** in front of the local server (LiteLLM proxy with
  `anthropic` routes, or claude-code-router), and the runner sets
  `ANTHROPIC_BASE_URL=<gateway>` and `ANTHROPIC_AUTH_TOKEN=<token or "local">`, with
  `options.model` set to the name the gateway exposes.
- The Models panel shows a banner for local providers — *"requires an
  Anthropic-API-compatible gateway; a raw Ollama/vLLM endpoint will not work"* — and a
  **Test** button that does a one-token round trip through the SDK and reports the real
  error.
- Second caveat shown in the UI: local models are markedly weaker at the strict tool-call
  formatting Claude Code depends on; expect degraded agentic behaviour.

### 3.7 Skills and MCP servers

- **Skills.** A skill is `~/.claude/skills/<name>/SKILL.md` (YAML frontmatter: `name`,
  `description`) plus optional `references/`, `scripts/`. Discovery scans the user dir,
  `<project_path>/.claude/skills`, and enabled plugins. Import accepts a `.zip` or a bare
  `SKILL.md`, validates frontmatter and directory name, rejects absolute paths, `..` and
  symlinks inside the archive, then unpacks under `~/.claude/skills/<name>/`.
  Wiring: `options.skills = [<names selected on the task>]`, **never `"all"`** — `"all"`
  injects a bare `Skill` entry into `allowed_tools`, which shadows `can_use_tool` (the SDK
  emits `CanUseToolShadowedWarning` for exactly this case, and the harness treats that
  warning as an error in tests).
  Also surfaced in the UI: the SDK documents `skills` as a *context filter, not a
  sandbox* — unselected skill files remain readable from disk.
- **MCP servers.** Rows map 1:1 onto `McpStdioServerConfig` / `McpSSEServerConfig` /
  `McpHttpServerConfig`, passed as `options.mcp_servers = {name: config}` with
  **`strict_mcp_config=True`**, so a `.mcp.json` inside the target project cannot inject
  servers. `get_mcp_status()` after connect records per-server connection state.

---

## 4. The permission-scoping mechanism (security-critical)

### 4.1 What the SDK actually does — measured

Seven live probes against a throwaway project, writing inside and outside the root:

| # | Setup | Result |
| --- | --- | --- |
| P1 | `Write` outside root, `permission_mode="default"`, no `allowed_tools` | `PreToolUse` hook fired **first**, then `can_use_tool` with the full input and `suggestions=[addDirectories(…)]`. Deny honoured; file not created |
| P2 | `Bash` `printf hi > <outside>/x.txt`, same setup | `can_use_tool` fired with **`ctx.blocked_path`** populated — the CLI parses shell redirection targets itself. Deny honoured |
| P3 | Same, but `allowed_tools=["Bash"]` | `can_use_tool` **never called** (SDK warned `CanUseToolShadowedWarning`); the `PreToolUse` hook **still fired** |
| P4 | `Write` to `<root>/link/x.txt` where `link → <outside>`, hook denies on resolved path | Hook fired and denied. `Path(...).resolve()` exposed the escape; also `/tmp` → `/private/tmp`, so the **root itself must be realpath'd** |
| **P5** | **`PreToolUse` hook callback raises `ValueError`**, `allowed_tools=["Write"]`, target inside root | **The write SUCCEEDED.** No denial recorded. A raising hook is **fail-open** |
| **P6** | **`can_use_tool` raises `ValueError`**, target outside root | **Denied** — *"Tool permission request failed"* — and recorded in `ResultMessage.permission_denials`. A raising callback is **fail-closed** |
| **P7** | `permission_mode="dontAsk"`, no allow rules, target **inside** root | **Denied.** `dontAsk` denies anything not pre-approved, in-root writes included |
| **P8** | In-process SDK MCP tools with `tools=[]`, `can_use_tool` denying one of them | `init.tools` contained **only** `mcp__harness__*` — no built-ins at all. The callback fired for every MCP tool call with resolved arguments; the denied tool's handler **never ran**, and the denial appeared in `permission_denials`. `readOnlyHint` did not exempt a tool from the callback. Drives § 5a |

Four conclusions, two of them counter-intuitive:

1. **The hook is the universal chokepoint but the weaker one.** It fires for every tool
   call — including calls already auto-approved by `allowed_tools` (P3) and calls inside
   subagents — yet if its callback raises, the tool proceeds (P5). Any `except` in the
   guard that does not itself produce a *deny* is a security hole.
2. **`can_use_tool` is the narrower gate but the safer one.** It is skipped whenever a
   rule already says allow (P3), yet it fails closed when it errors (P6).
   These two facts point in opposite directions, so the design uses **both** and makes the
   hook's failure mode match the callback's by construction (§ 4.4).
3. Rely on neither for path truth: re-derive and `realpath` every candidate path
   ourselves (P4). `ctx.blocked_path` is a useful signal, not a boundary.
4. `dontAsk` (P7) gives a config-level closed default, but it also means `can_use_tool` is
   never consulted — so it is *not* used as the primary mode; see § 4.9 for why it remains
   the recommended fallback switch.

### 4.2 Root establishment (once, at launch)

```python
root = Path(task.project_path).expanduser()
if not root.is_absolute(): reject("project path must be absolute")
root = Path(os.path.realpath(root))            # resolves /tmp → /private/tmp and all symlinks
if not root.is_dir(): reject("not a directory")
if root in {Path("/"), Path.home()} or root.parts[:2] == ("/", "etc"): reject("dangerous root")
if any(c in str(root) for c in "()[],\n\r"): reject("path cannot be expressed as a permission rule")
```

The realpath step is mandatory, not cosmetic: the probes ran under `/tmp/...` and every
comparison had to be against `/private/tmp/...` (P4). The last check exists because
permission rules (§ 4.3) are a `Tool(pattern)` string grammar — a path containing
parentheses or commas cannot be encoded unambiguously, so it is rejected at task creation
with a clear message rather than silently producing a broken rule.

The same validation backs `GET /api/fs/validate`, so the UI shows a green/red state before
the Run button enables.

### 4.3 Layer A — deny rules in the generated settings blob

```json
{
  "permissions": {
    "deny": [
      "Edit(//Users/<me>/.claude/**)", "Edit(//Users/<me>/.ssh/**)", "Edit(//etc/**)",
      "Edit(~/.bashrc)", "Edit(~/.zshrc)",
      "Read(//Users/<me>/.ssh/**)", "Read(//Users/<me>/.aws/**)",
      "Read(//Users/<me>/.claude/**)", "Read(//Users/<me>/.config/gh/**)",
      "Edit(//<root>/.git/hooks/**)", "Edit(//<root>/.claude/**)"
    ]
  },
  "sandbox": { … § 4.8 … }
}
```

Note what is **not** here: there is no `"allow"` list. Allow rules would shadow
`can_use_tool` exactly the way `allowed_tools` does (P3), removing the fail-closed layer.
So the harness ships deny rules only and lets everything else fall through to the
callback.

Deny rules are evaluated before the prompt path, so they hold even if both callbacks are
broken. The two `.git/hooks/**` and `.claude/**` entries close the "write something inside
the root that executes later" hole, which a path-containment check cannot see because
those paths are legitimately inside the project.

An `Edit(...)` rule covers the whole file-editing family including `Write`.

### 4.4 Layer B — the gate: hook + permission callback

```python
class Gate:
    async def pre_tool_use(self, inp, tool_use_id, ctx):
        try:
            tool, ti = inp["tool_name"], inp.get("tool_input", {})
            verdict = self.guard.check(tool, ti, agent_id=inp.get("agent_id"))
            await self.audit("hook", tool, tool_use_id, verdict)
            if verdict.allowed:
                return {}                          # no decision: normal rules still apply
            return deny(verdict.reason)
        except BaseException as exc:               # ← P5: a raise here would FAIL OPEN
            await self.audit_error("hook", exc)
            return deny(f"harness guard internal error: {exc!r}")

    async def can_use_tool(self, tool, ti, ctx):
        verdict = self.guard.check(tool, ti, agent_id=ctx.agent_id,
                                   blocked_path=ctx.blocked_path)
        await self.audit("callback", tool, ctx.tool_use_id, verdict)
        return (PermissionResultAllow() if verdict.allowed
                else PermissionResultDeny(message=verdict.reason))
```

where

```python
def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}
```

Three properties are load-bearing, each with a named test in `tests/test_gate.py`:

- **`except BaseException` returns a deny, not a re-raise and not `{}`.** This is the
  direct fix for P5 and the single most important line in the security code. The test
  monkeypatches `guard.check` to raise and asserts the returned dict is a deny.
- **The allow path returns `{}`, never an `"allow"` decision.** The gate only ever vetoes,
  so Layer A's deny rules still apply underneath it. Returning `allow` would also skip
  `can_use_tool` entirely.
- **`can_use_tool` deliberately has no `try`/`except`.** P6 shows an exception there is
  already a denial, and swallowing it would convert a fail-closed path into whatever the
  handler decided.

Which layer sees what, in `permission_mode="default"`:

| Call | Hook | Callback | Notes |
| --- | --- | --- | --- |
| Write/Edit anywhere | ✓ | ✓ | writes always reach *ask* (P7 confirms they are not pre-approved) |
| Read inside root | ✓ | ✗ | auto-allowed by the CLI, so the hook is the only gate |
| Read outside root | ✓ | ✓ | prompts, so the callback sees it too |
| Bash | ✓ | ✓ | with `ctx.blocked_path` when the CLI spots an out-of-root target (P2) |
| Subagent tool calls | ✓ | ✓ | `agent_id` present |
| Anything auto-allowed by a rule | ✓ | ✗ | why the harness ships no allow rules (§ 4.3) |

`allowed_tools` is kept **empty** for the same reason, and
`tests/test_options_builder.py` asserts no whole-tool entry can ever reach it — including
via the `skills="all"` path, which appends a bare `Skill` entry.

### 4.4a Interactive approval (decision 4)

A gate verdict has three outcomes, not two: `allow`, `deny`, and **`ask`**. `ask` is what
the gate returns for a write outside the root — instead of a flat denial, the run blocks
until the user answers in the UI.

```python
async def can_use_tool(self, tool, ti, ctx):
    verdict = self.guard.check(tool, ti, …)
    if verdict.decision == "allow":
        return PermissionResultAllow()
    if verdict.decision == "deny":                       # hard denials never ask
        return PermissionResultDeny(message=verdict.reason)
    return await self.request_approval(tool, ti, ctx, verdict)   # ask
```

```python
async def request_approval(self, tool, ti, ctx, verdict):
    req = PendingApproval(id=uuid4(), tool=tool, input=ti, reason=verdict.reason,
                          paths=verdict.resolved_paths)
    self.pending[req.id] = req
    await self.emit("permission_request", req.public())        # → SSE → UI modal
    try:
        answer = await asyncio.wait_for(req.future, self.timeout)   # approval_timeout_s
    except asyncio.TimeoutError:
        await self.emit("permission_timeout", {"id": str(req.id)})
        return PermissionResultDeny(message="approval timed out")
    finally:
        self.pending.pop(req.id, None)
    if answer.approved:
        if answer.remember:                 # "allow this directory for the rest of the run"
            self.policy.add_session_root(answer.root)
        return PermissionResultAllow()
    return PermissionResultDeny(message=answer.reason or "denied by user")
```

`POST /api/runs/{run_id}/approvals/{request_id}` carries `{approved, remember, reason}` and
resolves the future. Three rules keep this from becoming a new hole:

- **Timeout denies.** A browser that closed, or a user who walked away, must not leave the
  run wedged forever — and must not default to allow. The deadline is
  `tasks.approval_timeout_s` (default 300 s).
- **`deny` is not askable.** Anything a § 4.3 deny rule covers, and anything from an
  untrusted MCP server (§ 4.7), returns `deny` outright. Only the path-containment failure
  is escalated to the user, so the modal stays rare and meaningful.
- **Cancel resolves everything.** Cancelling a run fails all pending approvals as denials
  before calling `interrupt()`, so no task is left awaiting a future.

Every approval and its answer is written to `permission_decisions` with `layer = "ui"`,
so the run history shows exactly what was waved through and why.

**Interaction with paranoid mode (decisions 2 + 4).** These two features are mutually
exclusive by construction: `permission_mode="dontAsk"` means nothing ever prompts, so
`can_use_tool` is never consulted and there is no approval surface at all. In paranoid mode
an out-of-root write is denied outright. The UI states this on the toggle — *"paranoid mode
disables approval prompts; out-of-root writes are denied, not asked"* — and the task form
greys out `approval_timeout_s` when it is on.

The UI also needs the **unattended** case: a run whose browser tab is gone still streams
to `run_events`, and a pending approval is visible on the runs list with a countdown, so it
can be answered from any tab before the deadline.

### 4.5 Layer C — settings isolation

Default `setting_sources=[]`. With `"project"` loaded, a `.claude/settings.local.json`
**inside the target directory** could carry `permissions.allow` rules that pre-approve
tools and shadow `can_use_tool` — an untrusted checkout would be self-granting.
Consequences, both surfaced in the UI:

- project `CLAUDE.md` is not loaded (it requires `"project"`),
- project-local skills are not discovered.

Tasks that select skills pass `setting_sources=["user"]` explicitly rather than letting
the SDK default to `["user","project"]`. A per-task **"trust this project's settings"**
checkbox opts into `["user","project"]`, with the shadowing risk stated next to it.
Layers A and B are unaffected either way: `options.settings` is the highest-priority
user-controlled layer, and no settings file can unregister an in-process callback.

### 4.6 Path extraction and the containment test

`security/tool_paths.py` holds an explicit per-tool table; unknown tools **fail closed**.

| Tool | Path fields | Class |
| --- | --- | --- |
| `Write`, `Edit`, `NotebookEdit` | `file_path` | write |
| `Read` | `file_path` | read |
| `Glob`, `Grep` | `path` | read |
| `Bash`, `BashOutput`, `KillShell` | — (see § 4.8) | exec |
| `WebFetch`, `WebSearch` | — | network (per-task, default deny) |
| `Agent`/`Task` | — | allowed; the subagent's own calls are hooked |
| `TodoWrite`, `Skill` | — | allowed, no filesystem effect |
| `mcp__*` | unknown schema | § 4.7 |
| anything else | — | **deny**, logged as `unknown_tool` |

```python
def resolve_candidate(p: str, cwd: Path) -> Path:
    q = Path(p).expanduser()
    q = cwd / q if not q.is_absolute() else q
    # realpath the deepest existing ancestor, then re-attach the not-yet-created tail,
    # so new files are checkable and a symlinked *parent* is still caught (P4)
    anc, tail = q, []
    while not anc.exists():
        tail.append(anc.name); anc = anc.parent
    return Path(os.path.realpath(anc)).joinpath(*reversed(tail))

def contained(p: Path, root: Path) -> bool:
    return p == root or root in p.parents      # NOT str.startswith — "/proj-evil" vs "/proj"
```

- **Writes** must be contained in `root`.
- **Reads** are **not** path-confined (decision 1). Any read the gate sees is allowed
  unless a § 4.3 deny rule covers it.

**Explicit design decision on reads.** Writes are confined; reads are not. The SDK could
scope reads (`add_dirs` plus `Read` deny rules), and § 4.4's table shows in-root reads
never reach `can_use_tool` anyway, so confining them would rest entirely on the hook. We
are deliberately not doing it: an agent that cannot read a sibling library, a
`~/.config` file or a system header is crippled for most real tasks, and the
approval-prompt flow (§ 4.4) would turn every such read into a modal.

The residual exposure is stated plainly rather than papered over: **reads are unconfined,
so a session can read any file the harness user can read.** Three things limit the damage,
and they are the only things that do:

1. The § 4.3 `Read(...)` deny rules cover the credential set — `~/.ssh`, `~/.aws`,
   `~/.claude`, `~/.config/gh` — and are the one hard boundary on reads.
2. `WebFetch` / `WebSearch` are denied unless `tasks.network_enabled` is set, which closes
   the read-then-exfiltrate path for the default configuration.
3. Every read the gate sees is audited in `permission_decisions`, so a run's read
   footprint is reviewable after the fact.

If a task enables network access, the UI states that the combination — unconfined reads
plus network — means the session can send any readable file anywhere, and recommends
`paranoid_mode` for untrusted prompts.

### 4.7 MCP tools

`mcp__<server>__<tool>` inputs have server-defined schemas, so paths cannot be extracted
generically. Policy: tools from a server with `trusted = 0` are **denied by the gate**.
Marking a server trusted is a deliberate UI action that states plainly that its tools run
outside the path boundary. A filesystem-type MCP server is exactly the hole that would
otherwise make § 4.6 decorative.

### 4.8 Bash — the hard case

A shell command can write anywhere and static analysis of shell is not a security
boundary. Three measures, in order of strength:

1. **OS sandbox**: `sandbox={"enabled": True, "autoAllowBashIfSandboxed": False,
   "allowUnsandboxedCommands": False, "excludedCommands": []}`. This is macOS
   `sandbox-exec` / Linux bubblewrap enforcement and the only real containment for `Bash`.
   `allowUnsandboxedCommands: False` stops the model opting out via
   `dangerouslyDisableSandbox`.
   Caveat found while probing: nested sandboxing fails with
   `sandbox-exec: sandbox_apply: Operation not permitted` (exit 71) when the harness
   itself runs inside a sandbox. The runner detects that signature and fails the run with
   an actionable message rather than continuing unprotected.
2. **`ctx.blocked_path`** from `can_use_tool` (P2) — when the CLI's own shell analysis
   flags an out-of-root target, deny immediately.
3. **A conservative textual screen** in the gate: deny commands containing absolute paths
   outside root, `sudo`, `curl … | sh`, or writes to `~/.claude`, `~/.ssh`, `/etc`. A speed
   bump, explicitly *not* the boundary, and labelled as such in the code.

If `sandbox_bash` is off, the UI marks the task "Bash unconfined" in red and the run
detail records it.

### 4.9 Residual risks, stated rather than hidden

- **Hook fail-open (P5) is the sharpest edge in this design.** In-process callbacks feel
  safer than external hook commands, but the failure semantics are the same: an error in
  the hook lets the tool through. Mitigated by the `except BaseException → deny` wrapper
  and its test. For tasks where that is not reassuring enough, the fallback switch is
  `permission_mode="dontAsk"` (P7): a config-level closed default where even a completely
  broken gate cannot grant anything. The cost is losing `can_use_tool` entirely — no
  per-call decisions, no `blocked_path`, and no path to interactive approval — so it is
  offered as an opt-in "paranoid mode" per task rather than the default.
- **Reads are unconfined by decision** (§ 4.6). The gate allows any read a deny rule does
  not cover, so a compromised or misdirected prompt can read anything the harness user can.
  This is an accepted trade-off, not an oversight; the mitigations are the credential deny
  rules, network tools off by default, and the read audit trail.
- **A blocked run holds a slot.** An approval awaiting an answer occupies one of the
  `MAX_CONCURRENT` semaphore permits for up to `approval_timeout_s`. The runs list shows
  pending approvals prominently so a forgotten modal does not quietly starve the queue.
- **Silent settings failure.** `options.settings` is interpreted by the CLI, which ignores
  invalid settings in headless mode. A typo would remove the deny rules without an error.
  Mitigated by generating the blob from typed objects and by the § 3.4 preflight
  assertions.
- **TOCTOU.** The gate resolves a path, the tool writes moments later; a symlink swapped
  in between defeats the check. The OS sandbox (§ 4.8) is the durable answer for `Bash`;
  for `Write`/`Edit` the window is small but real and not closable in-process.
- **Concurrent hook dispatch.** The SDK dispatches hooks for an event concurrently, so the
  gate must be re-entrant and must not accumulate per-call state on `self`. Audit writes
  go through a single queue.
- **Environment inheritance.** The SDK passes the harness's whole environment to the
  subprocess. The runner starts from a scrubbed base (drops `AWS_*`, `GITHUB_TOKEN`,
  unrelated `ANTHROPIC_*`) and adds back only what the model config specifies.
- `path_guard` correctness is the crux, so `tests/test_path_guard.py` covers `..`
  traversal, symlinked parent (P4), symlinked root, `/proj` vs `/proj-evil` prefix,
  relative paths, `~` expansion, NUL bytes, non-existent deep paths, case-insensitive APFS
  collisions, and every tool in the extractor table.

---

## 5. Build order

1. Skeleton: app factory, DB, models, then `path_guard` + `rules` + `gate` with their
   tests — including the fail-closed hook test. Security first, before anything can open
   an SDK session.
2. `options_builder` + `settings_builder`, with a test asserting no whole-tool entry ever
   reaches `allowed_tools`.
3. CRUD routers + schemas for skills / mcps / models / roles / teams.
4. Two-panel frontend shell + the five management views.
5. Tasks: CRUD, path validation endpoint + directory picker, inline team creation.
6. Runner: `RunManager`, connect, preflight, event normalisation, cancel, denial
   reconciliation.
7. SSE streaming + run history / permission-decision UI.
8. Skill import, MCP connection test, model test round-trip, suggestion catalogs.

---

## 5a. Chat: configuring the harness by asking for it

A seventh panel. The user describes what they want — "set up a review task for
~/code/api" — and the assistant does it, asking when something is unclear and
confirming every change before it lands.

### 5a.1 Mechanism

The assistant is another SDK session, but with a deliberately inverted tool set:

```python
ClaudeAgentOptions(
    tools=[],                                     # no built-in tools at all
    mcp_servers={"harness": chat_tools.build_server()},   # in-process SDK MCP server
    strict_mcp_config=True,
    permission_mode="default",
    setting_sources=[],
    can_use_tool=self._can_use_tool,              # the confirmation surface
    hooks={"PreToolUse": [HookMatcher(hooks=[self._pre_tool_use])]},
    system_prompt=SYSTEM_PROMPT,                  # plain string, not the claude_code preset
)
```

`tools=[]` is the load-bearing line: the session has **no** Read, Write, Edit or Bash.
Its only capabilities are the fifteen tools in `app/services/chat_tools.py`, which wrap
the harness's own operations. Verified live — the `init` event's tool list contains
nothing but `mcp__harness__*`.

The tools are defined with the SDK's `@tool` decorator and served over an in-memory
transport by `create_sdk_mcp_server`, so they run in the harness process with direct
access to the database. No subprocess, no IPC, no second copy of the validation rules:
both the REST routers and these tools go through `app/services/crud.py`.

### 5a.2 Confirmation

Measured first (probe P8): `can_use_tool` **is** invoked for in-process MCP tool calls,
with the resolved arguments; a denial stops the handler from running at all and is
reported in `permission_denials`. `readOnlyHint` does not exempt a tool from the
callback, so the harness decides what needs confirming.

```python
async def _can_use_tool(self, tool_name, tool_input, context):
    if not chat_tools.is_mutating(tool_name):
        return PermissionResultAllow()          # browsing config is fluent
    ...emit confirmation_request, await the user's answer...
```

Three properties, each with a test:

- **Gating is an allowlist of read-only tools, not a denylist of mutating ones.** A tool
  added later without being classified is confirmed by default. The first version had
  this inverted and a test caught it: an unclassified tool in our own namespace would
  have run unconfirmed.
- **The read-only allowlist must equal the set advertised as `readOnlyHint`.** A test
  asserts the two agree, so the model's view of a tool and the harness's gating cannot
  drift apart.
- **A declined call's reason is handed back to the model** as the denial message, which
  makes "no, call it Reviewer instead" an ordinary way to steer it. Confirmed live: the
  assistant acknowledged the decline and left the database untouched.

A `PreToolUse` hook additionally refuses anything that is not one of our own tools —
redundant while `tools=[]` holds, and the cheap insurance if that ever changes. It uses
the same fail-closed wrapper as the task gate (§ 4.4), for the same reason (P5).

### 5a.3 Asking rather than guessing

The system prompt tells the assistant to read state before changing it, to ask when
something material is unspecified, and to act once it has enough. Verified live: asked
to "add a role called Foo" with no further detail, it asked what the role should do and
created nothing; given the detail in the next message, it raised a confirmation.

Two deliberate restrictions on what it can do:

- **No deletion.** Destructive, irreversible, and the panels already offer it behind an
  explicit confirm. There is a test asserting no delete-shaped tool exists.
- **No secrets.** The prompt forbids inventing an API key; `add_model_config` takes no
  key parameter at all, so the assistant physically cannot set one. The user fills keys
  in via the Models panel.

### 5a.4 Persistence and streaming

`chat_sessions` plus `chat_messages`, where the transcript *is* the event log — one
table serves both the durable conversation and SSE replay, indexed by the same `seq`
the `Last-Event-ID` header carries. `app/services/events.py` was generalised into an
`EventBus` with injected persist/replay so runs and chats share the fan-out logic.

A chat outlives the browser tab, and `ChatManager.attach` revives one that exists only
in the database, resuming the model's own conversation via `options.resume` and
continuing the sequence numbers from the stored maximum (a test pins this — reusing a
sequence number would corrupt replay). If the resume is rejected, it starts fresh and
says so in the stream rather than failing the chat.

One asymmetry with runs worth knowing: a finished run's SSE stream terminates, but an
open chat's does not — it stays open waiting for the next turn. That is correct, and it
is why the live branch is tested at the bus level rather than over HTTP.

---

## 6. Decisions

Settled, and reflected above:

1. **Writes are confined to the project root; reads are not** (§ 4.6). The credential deny
   rules and the default-off network tools are what limit read exposure, and the doc says
   so explicitly rather than implying reads are safe.
2. **Paranoid mode (`permission_mode="dontAsk"`) is a per-task opt-in**, not the default
   (§ 4.9). It is mutually exclusive with approval prompts (§ 4.4a).
3. **One session per run, lead role delegating to subagents** via `options.agents`
   (§ 3.5).
4. **Out-of-root writes surface an approval prompt in the UI and block the run** until
   answered, with a denying timeout (§ 4.4a). Hard denials and untrusted MCP tools are not
   askable.
5. **Local models go through a LiteLLM / claude-code-router gateway** (§ 3.6). The repo
   ships a sample `litellm.config.yaml` and the Models panel links to it.

Remaining implementation risks are tracked in § 4.9, not here.
