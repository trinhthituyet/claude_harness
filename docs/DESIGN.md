# Control Panel for Claude Code Sessions — Design Proposal

Status: proposal, no application code written yet.
Target of the proposal: the four items in `docs/PROMPT.md` § "Before Writing Code, Propose".

Trigger mechanism: **the `claude` CLI, spawned as a subprocess per run** (headless
`--print` mode with JSON streaming). The Claude Agent SDK is not used.

Everything in § 4 (and the CLI facts in § 3) was verified empirically by running the
real CLI (`claude-code` 2.1.274) on this machine, not assumed. Probe results are in
§ 3.1 and § 4.1.

---

## 0. Stack decisions

| Choice | Decision | Why |
| --- | --- | --- |
| Backend | FastAPI + `uvicorn`, async | Subprocess I/O is naturally async; `asyncio.create_subprocess_exec` streams stdout without threads |
| Agent runtime | `claude` CLI subprocess, `-p --input-format stream-json --output-format stream-json` | Per the requirement. No `claude-agent-sdk` dependency |
| ORM | SQLAlchemy 2.0 async + `aiosqlite` | 8 tables of plain CRUD, one event loop throughout |
| Migrations | `create_all` + a `schema_version` table with hand-written steps | Single local user, no Alembic ceremony |
| Frontend | Plain HTML + ES modules + CSS, no build step | "Keep it lightweight". Two-panel layout is ~300 lines of DOM code |
| Streaming | SSE (`EventSource`) with `Last-Event-ID` resume | One-way server→browser; survives reload. Control actions (cancel) are plain REST |
| Python | 3.12 | Only needed for the harness itself; the CLI brings its own runtime |

Dropping the SDK costs little: the SDK is *itself* a subprocess wrapper around this same
CLI — its transport builds an argv of `--output-format stream-json --verbose
--input-format stream-json --settings … --permission-mode …` and speaks the same JSON
protocol (`claude_agent_sdk/_internal/transport/subprocess_cli.py:567-786`). What we give
up is in-process Python callbacks for `canUseTool` and hooks; those become CLI-native
mechanisms (§ 4), which turn out to be **stronger**, not weaker, because they include
rule-level denies the SDK path also relies on.

**CLI discovery.** At startup, resolve `claude` (config override → `$PATH` → common
install dirs), run `claude --version`, record it, and refuse to start runs if the
required flags are absent (feature-probe `--help` once, cached). The CLI binary and its
version are recorded on every run row so history stays interpretable after upgrades.

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
extra_read_paths_json   list[str], default []   ← § 4.6
trust_project_settings  bool, default 0         ← § 4.5
sandbox_bash            bool, default 1         ← § 4.8
restricted_mode         bool, default 1         ← § 4.4, the CLI's --restricted
max_turns               int NULL
max_budget_usd          real NULL
created_at / updated_at
```

```
task_runs                                   run_events
─────────                                   ──────────
id                PK (uuid, also the CLI --session-id)   id      PK
task_id           FK→tasks ON DELETE SET NULL  run_id  FK→task_runs ON DELETE CASCADE
status            queued|running|completed  seq       int   ← monotonic per run, = SSE event id
                  |failed|denied|cancelled  ts
cli_path / cli_version                      type      assistant_text|thinking|tool_use|
cli_pid / cli_exit_code                               tool_result|permission|hook|system|
argv_json         full argv, secrets redacted         result|error|status
settings_json     the generated --settings blob       payload_json
prompt_snapshot   TEXT                                UNIQUE(run_id, seq)
project_path_snapshot TEXT (the realpath'd root)
team_snapshot_json    full role+prompt copy
model_snapshot_json   provider/model/base_url
skills_snapshot_json / mcps_snapshot_json
started_at / ended_at
exit_reason       success|error_max_turns|error_max_budget_usd|cancelled|preflight_failed|crashed
num_turns / total_cost_usd / duration_ms
error_text
output_text       final assistant text, denormalised for the history list

permission_decisions        ← the audit log for the security boundary (§ 4)
────────────────────
id PK · run_id FK · ts · source (hook|rule|no_approval_surface)
· tool_name · tool_use_id · decision allow|deny · reason
· candidate_paths_json · resolved_paths_json · tool_input_json
```

`permission_decisions` is written from two places: the guard hook process (§ 4.4) writes
its own verdicts, and the runner reconciles them at the end against the
`result` event's `permission_denials[]` array, which the CLI emits for every denial from
any source (verified — see probe P6). Mismatch between the two is itself logged: it means
something was denied by a mechanism the guard did not see.

**Why snapshots.** A run must remain reproducible after the task, team, role prompts or
model config are edited or deleted. Everything the run was constructed from — including
the literal argv and settings blob — is copied into `task_runs` at launch.

**Relationships:** `teams ↔ roles` many-to-many through `team_roles` (ordered, one lead);
`tasks → teams` many-to-one; `tasks ↔ skills` and `tasks ↔ mcp_servers` many-to-many;
`tasks → task_runs` one-to-many; `task_runs → run_events` one-to-many (the replay log).

### 1.1 Secrets

`model_configs.api_key` and `mcp_servers.env_json` hold credentials in plaintext SQLite.
For a single local user this matches the threat model, but the DB is created `0600`, the
API never echoes a stored key back (`GET` returns `"sk-ant-…abcd"`; `PUT` with the mask
means "unchanged"), and both `argv_json` and `settings_json` are redacted before storage.
Per-run generated files (§ 3.3) are written `0600` into a private run directory and
deleted when the run ends.

---

## 2. FastAPI project structure

```
claude_harness/
  pyproject.toml            # uv-managed, python = ">=3.12"
  app/
    main.py                 # app factory, lifespan (db init, CLI probe, RunManager), static mount
    config.py               # settings: db path, cli path, run dir, max concurrent runs
    db.py                   # async engine/session, create_all + schema_version steps
    models/                 # SQLAlchemy ORM
      skill.py  mcp.py  model_config.py  role.py  team.py  task.py  run.py
    schemas/                # Pydantic v2 request/response models (+ validators)
    routers/
      skills.py             # CRUD + POST /import (zip or SKILL.md upload) + GET /discover
      mcps.py               # CRUD + POST /test-connection
      models.py             # CRUD + POST /{id}/test  (one-token round trip via the CLI)
      roles.py  teams.py    # CRUD; team creation validates ≥1 role + exactly one lead
      tasks.py              # CRUD + POST /{id}/run  + inline team creation
      runs.py               # GET list/detail, GET /{id}/events (SSE), POST /{id}/cancel
      fs.py                 # GET /fs/validate?path=…  and GET /fs/ls?path=…  (dir picker)
      catalog.py            # GET /catalog/skills, /catalog/mcps  (the "Suggested" sections)
    services/
      cli.py                # locate claude, probe --version/--help, build argv  (§ 3.3)
      settings_builder.py   # generate the per-run --settings JSON  (§ 3.3, § 4.3)
      stream.py             # stream-json line reader → normalised events  (§ 3.4)
      runner.py             # RunManager, Run: spawn, preflight, pump, cancel, reap
      prompt_composer.py    # team + roles → --append-system-prompt + --agents JSON (§ 3.5)
      model_resolver.py     # model_config row → (--model value, env overrides)  (§ 3.6)
      skill_discovery.py    # scan ~/.claude/skills, <project>/.claude/skills, plugins
      skill_install.py      # validate + unpack an uploaded skill
      mcp_registry.py       # row → --mcp-config JSON entry
      catalog.py            # seeded suggestion lists, diffed against installed
    security/
      path_guard.py         # pure containment logic (§ 4.6). No FastAPI, no DB
      tool_paths.py         # per-tool path extractor table
      policy.py             # RunPolicy: roots, read roots, trusted MCPs; JSON (de)serialise
      rules.py              # RunPolicy → permissions.allow/deny rule strings (§ 4.3)
      hook_guard.py         # ← ENTRY POINT run by the CLI as a PreToolUse hook (§ 4.4)
    static/
      index.html  app.js  views/{skills,mcps,models,roles,teams,tasks}.js  style.css
  tests/
    test_path_guard.py      # traversal, symlink, prefix, unicode, every tool in the table
    test_rules.py           # rule-string generation + escaping/rejection
    test_hook_guard.py      # subprocess-level: stdin payload → stdout JSON / exit 2
    test_stream.py          # stream-json fixtures → events (no network)
    test_runner_fake_cli.py # runner driven by a fake `claude` script on $PATH
    test_api_*.py
  docs/PROMPT.md  docs/DESIGN.md
```

`app/security/hook_guard.py` is deliberately a **standalone `python -m` entry point**:
the CLI executes it as an external command, so it must start fast, have no FastAPI or DB
import on its hot path, and be independently testable by piping JSON at it.

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
POST   /api/runs/{run_id}/cancel          interrupt → SIGINT → SIGTERM → SIGKILL
```

---

## 3. How the CLI subprocess is constructed and run

### 3.1 Verified CLI facts

From `claude --help` on 2.1.274 and live runs:

- `-p/--print` with `--output-format stream-json --verbose` emits one JSON object per
  line: `system/init`, `assistant`, `user` (tool results), `system/hook_started`,
  `system/hook_response`, `result`.
- `--input-format stream-json` accepts user messages as JSON lines on stdin, so the
  prompt can be sent **after** reading `init` — that is what makes preflight (§ 3.4)
  possible.
- `--settings` accepts a file path **or a raw JSON string**, and lands in the
  highest-priority user-controlled layer.
- `--setting-sources ""` disables user/project/local settings entirely. Verified: with
  it, `apiKeyHelper` must be re-supplied inside `--settings` or auth breaks.
- `--permission-prompts none` — "nobody can answer a permission prompt here" — denies
  anything that would prompt.
- `--restricted` confines file tools to the working directories, drops command-running
  tools and WebFetch unless `--tools` names them, refuses `bypassPermissions`, and
  ignores user/project/local settings files.
- `--agents <json>`, `--append-system-prompt`, `--add-dir`, `--mcp-config`,
  `--strict-mcp-config`, `--max-turns`, `--max-budget-usd`, `--model`,
  `--fallback-model`, `--session-id <uuid>`, `--include-partial-messages`,
  `--forward-subagent-text`, `--include-hook-events` all exist and are used below.
- Sandbox settings are not a flag: they go inside the `--settings` JSON under `"sandbox"`.
- ⚠ From the `--print` help text: *"Settings files that fail validation are silently
  ignored in this mode."* A typo in our generated settings would silently remove the
  entire security configuration. § 3.4 preflight exists because of this sentence.

### 3.2 Execution model

Async, one `asyncio.Task` + one subprocess per run, supervised by a `RunManager`.

```
POST /api/tasks/{id}/run
  → validate task (prompt non-empty, team ≥1 role, project_path exists & is a dir)
  → insert task_runs row (status=queued) + snapshots
  → RunManager.spawn(run)      # bounded by Semaphore(MAX_CONCURRENT)
  → 202 {run_id}               # the run outlives the HTTP request
```

```python
class Run:
    async def execute(self):
        policy   = RunPolicy.from_snapshot(self.snapshot)          # § 4
        rundir   = self.make_run_dir()                             # 0700, per run
        policy.write(rundir / "policy.json")                       # read by hook_guard
        settings = build_settings(policy, self.snapshot)           # § 3.3
        (rundir / "settings.json").write_text(json.dumps(settings))
        argv     = build_argv(self.snapshot, policy, rundir)
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(policy.root), env=scrubbed_env(self.snapshot),
            stdin=PIPE, stdout=PIPE, stderr=PIPE)
        try:
            init = await self.read_init()                          # § 3.4 preflight
            self.assert_config(init, policy)                       # abort on mismatch
            await self.send_user_message(self.snapshot.prompt)
            await self.pump()                                      # stdout lines → events
        finally:
            await self.reap(rundir)                                # exit code, cleanup
```

Three concurrent readers per run: stdout (events), stderr (captured into a bounded
buffer, surfaced on failure), and a watchdog for `max_wall_clock`.

`emit()` assigns `seq`, appends to a per-run ring buffer (last 2000 events), persists to
`run_events`, then publishes to subscriber queues. Persist-before-publish means a
reconnecting `EventSource` with `Last-Event-ID: N` is served from the buffer, falling
back to the table.

**Line-length trap:** `StreamReader.readline()` raises `LimitOverrunError` at 64 KiB by
default, and `init` alone was ~6 KB here while a large tool result easily exceeds the
limit. The subprocess is created with `limit=16 * 1024 * 1024`, and the reader still
handles `LimitOverrunError` by draining the oversized line into a truncated `error` event
rather than dying.

**Cancel:** write `{"type":"control_request","request_id":…,"request":{"subtype":"interrupt"}}`
to stdin (the protocol the SDK uses), then escalate SIGINT → SIGTERM → SIGKILL on 5 s /
5 s timers. Shutdown cancels all live runs and marks them `cancelled`.

### 3.3 The generated invocation

```
claude -p
  --input-format stream-json --output-format stream-json --verbose
  --include-partial-messages --forward-subagent-text --include-hook-events
  --settings <rundir>/settings.json
  --setting-sources ""                    # or "user" / "user,project" — § 4.5
  --permission-mode default
  --permission-prompts none               # default-deny, § 4.3
  --restricted                            # when tasks.restricted_mode, § 4.4
  --session-id <run_id>
  --add-dir <each extra read root>
  --mcp-config <rundir>/mcp.json --strict-mcp-config
  --agents <json for non-lead roles>
  --append-system-prompt <lead role prompt + team roster>
  --model <model id>  [--fallback-model …]
  [--max-turns N] [--max-budget-usd X]
  [--tools Read,Edit,Write,…]             # when the team restricts tools
```

cwd = the realpath'd project root. `settings.json` (0600) carries `permissions.allow` /
`permissions.deny`, the `PreToolUse` hook registration, `sandbox`, and `apiKeyHelper`
when one is needed.

### 3.4 Preflight — verifying the config actually applied

Because bad settings are silently ignored in `-p` mode, the runner treats the `init`
event as a **contract check** before sending the prompt. From the real `init` payload we
assert:

| Field | Assertion |
| --- | --- |
| `cwd` | equals the realpath'd project root |
| `permissionMode` | equals what we passed |
| `tools` | contains no tool outside the task's allowed set |
| `mcp_servers` | exactly the servers we configured (catches `--strict-mcp-config` failure) |
| `apiKeySource` | non-empty (catches auth silently falling back) |
| `model` | the model we asked for |

Additionally the runner fires a **canary**: the first stdin message is not the user
prompt but a probe that must trip the guard hook. If no `hook_started` /
`hook_response` pair for our guard appears, the hook did not register and the run is
aborted as `preflight_failed`. This directly addresses probe P1 (§ 4.1), where a
malformed matcher silently registered nothing.

### 3.5 Roles and teams → system prompt + subagents

A team of N roles maps onto the CLI's native multi-agent support, not N processes:

- The **lead** role's `system_prompt` plus a roster block goes to
  `--append-system-prompt`. Appending (rather than `--system-prompt`, which replaces)
  keeps Claude Code's built-in tool-use conventions, which the built-in tools depend on.
- Every **non-lead** role becomes an entry in the `--agents` JSON object:
  `{"<role name>": {"description": <role description>, "prompt": <role system prompt>,
  "model": <role model or omitted>}}`. `description` is what the lead sees when deciding
  whom to delegate to.
- The task prompt is the user turn, sent verbatim over stdin.

Subagent tool calls still pass through the same guard hook (the hook payload carries
`agent_id`/`agent_type` for subagent calls), so the security boundary covers delegation.
`--forward-subagent-text` lets the UI render each role's output in its own lane.

Single-role teams degenerate cleanly: no `--agents`, role prompt appended.

### 3.6 Models — Anthropic vs local (vLLM / Ollama)

**Anthropic**: `--model <model_id>` with `ANTHROPIC_API_KEY` in the subprocess
environment. Dropdown seeded with `claude-opus-5`, `claude-sonnet-5`,
`claude-haiku-4-5-20251001`, plus a free-text field.

**Local LLMs — flagged clearly, as the Models requirement asks.** The CLI has no
custom-base-URL flag. It reaches Anthropic via the `ANTHROPIC_BASE_URL` environment
variable, which we *can* set per subprocess. The catch is protocol, not plumbing: the CLI
speaks the **Anthropic Messages API** (tool use, streaming, prompt caching). Ollama's
native API and vLLM's OpenAI-compatible API are not that wire format, so pointing
`ANTHROPIC_BASE_URL` at `http://localhost:11434` will not work.

Proposed handling, surfaced in the UI rather than hidden:

- `provider = "anthropic"` → `env: {ANTHROPIC_API_KEY}`.
- `provider ∈ {ollama, vllm, custom}` → the config stores the URL of an
  **Anthropic-API-compatible gateway** in front of the local server (LiteLLM proxy with
  `anthropic` routes, or claude-code-router), and the runner sets
  `ANTHROPIC_BASE_URL=<gateway>` and `ANTHROPIC_AUTH_TOKEN=<token or "local">`, with
  `--model` set to the name the gateway exposes.
- The Models panel shows a banner for local providers — *"requires an
  Anthropic-API-compatible gateway; a raw Ollama/vLLM endpoint will not work"* — and a
  **Test** button that runs `claude -p --output-format json "reply with OK"` against the
  config and reports the real error.
- Second caveat shown in the UI: local models are markedly weaker at the strict tool-call
  formatting Claude Code depends on; expect degraded agentic behaviour.

### 3.7 Skills and MCP servers

- **Skills.** A skill is `~/.claude/skills/<name>/SKILL.md` (YAML frontmatter: `name`,
  `description`) plus optional `references/`, `scripts/`. Discovery scans the user dir,
  `<project_path>/.claude/skills`, and enabled plugins. Import accepts a `.zip` or a bare
  `SKILL.md`, validates frontmatter and directory name, rejects absolute paths, `..` and
  symlinks inside the archive, then unpacks under `~/.claude/skills/<name>/`.
  Skills are discovered through settings, so a task with skills selected needs
  `--setting-sources user` (§ 4.5); the ones not selected are excluded via the settings
  blob rather than `--disable-slash-commands`. The `init` event's `skills[]` array is
  asserted in preflight to confirm exactly the intended set loaded.
  Note for the UI: a skill filter is a *context* filter, not a sandbox — unselected skill
  files remain readable from disk.
- **MCP servers.** Rows serialise to a `<rundir>/mcp.json` of the standard
  `{"mcpServers": {name: {command/args/env | type/url/headers}}}` shape, passed with
  `--mcp-config` plus **`--strict-mcp-config`**, so a `.mcp.json` inside the target
  project cannot inject servers. Preflight compares `init.mcp_servers[]` against the
  intended set and records each server's `status` (`connected` / `failed` / `pending`)
  into the run log.

---

## 4. The permission-scoping mechanism (security-critical)

### 4.1 What the CLI actually does — measured

Six live probes against a throwaway project at `/tmp/cliprobe/proj`, writing to
`/tmp/cliprobe/outside`:

| # | Setup | Result |
| --- | --- | --- |
| P1 | `PreToolUse` hook with `"matcher": "*"` | **Hook never fired.** No `hook_started` event at all; the out-of-root write succeeded. `*` is not a valid matcher and is silently ignored |
| P2 | Same hook, valid matcher, hook command fails (exit 71) | `hook_response` with `outcome: "error"` — and **the tool ran anyway**. A failing hook is fail-open |
| P3 | Hook exits **2** with a message on stderr | Tool **blocked**, stderr fed back to the model as `PreToolUse:Write hook error: …`, and counted in `result.permission_denials` |
| P4 | Hook returns `{"hookSpecificOutput":{"permissionDecision":"deny",…}}`, with `--allowedTools Write` | **Blocked**, despite the tool being auto-allowed. Hook stdin payload: `session_id`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, `transcript_path` |
| P5 | `--permission-prompts none`, no allow rules | Every prompting tool denied — **including writes inside the root**: *"this session has no approval surface"* |
| P6 | `permissions.allow: ["Edit(//…/proj/**)"]`, `permissions.deny: ["Edit(//Users/**)"]`, `--permission-prompts none` | In-root write **allowed**; out-of-root write **denied** (no approval surface); `~/…` write **denied by rule**, with a distinct error: *"File is in a directory that is denied by your permission settings"* |
| P7 | Hook registered with **`matcher` omitted** | Fired for `Read`, `Bash` **and** `Write` — a true match-all chokepoint |

Conclusions that drive the design:

1. **Omit the matcher.** `"*"` silently disables the hook (P1); omitting it matches every
   tool (P7). This is the single most dangerous footgun in the whole configuration, and
   it fails silently and open — hence the § 3.4 canary.
2. **The guard must exit 2 on any internal error.** Exit 2 blocks (P3); any other nonzero
   exit is a non-blocking error and the tool proceeds (P2). A bare `except: sys.exit(1)`
   in the guard would be a security hole.
3. An `Edit(...)` rule governs the `Write` tool as well (P6) — the rule name is the
   permission family, not the literal tool.
4. `--permission-prompts none` is genuinely default-deny (P5), so allow rules must be
   generated deliberately; there is no "allowed because nobody objected" path.

### 4.2 Root establishment (once, at launch)

```python
root = Path(task.project_path).expanduser()
if not root.is_absolute(): reject("project path must be absolute")
root = Path(os.path.realpath(root))            # resolves /tmp → /private/tmp and all symlinks
if not root.is_dir(): reject("not a directory")
if root in {Path("/"), Path.home()} or root.parts[:2] == ("/", "etc"): reject("dangerous root")
if any(c in str(root) for c in "()[],\n\r"): reject("path cannot be expressed as a permission rule")
```

The realpath step is mandatory, not cosmetic: on macOS the probes ran in `/tmp/...` and
every rule and comparison had to be against `/private/tmp/...`. The last check exists
because permission rules are a `Tool(pattern)` string grammar — a project path containing
parentheses or commas cannot be encoded unambiguously, so such a path is rejected at task
creation with a clear message rather than silently producing a broken rule.

The same validation backs `GET /api/fs/validate`, so the UI can show a green/red state
before the Run button enables.

### 4.3 Layer A — generated permission rules (`--settings`)

```json
{
  "permissions": {
    "deny": [
      "Edit(//Users/<me>/.claude/**)", "Edit(//Users/<me>/.ssh/**)",
      "Edit(//etc/**)", "Edit(~/.bashrc)", "Edit(~/.zshrc)",
      "Read(//Users/<me>/.ssh/**)", "Read(//Users/<me>/.aws/**)",
      "Edit(//<root>/.git/hooks/**)", "Edit(//<root>/.claude/**)",
      "WebFetch"                                   /* unless the task enables network */
    ],
    "allow": [
      "Edit(//<root>/**)", "Read(//<root>/**)",
      "Read(//<extra read root>/**)"               /* one per extra_read_paths entry */
    ]
  },
  "sandbox": { … § 4.8 … },
  "hooks": { "PreToolUse": [ { "hooks": [ { "type": "command",
      "command": "<python> -m app.security.hook_guard <rundir>/policy.json",
      "timeout": 20 } ] } ] }
}
```

Deny rules beat allow rules and are evaluated **before** the prompt path, so they hold
even if the allow list is wrong (P6 showed the distinct denial error). The two
`Edit(//<root>/.git/hooks/**)` and `Edit(//<root>/.claude/**)` entries close the
"write something inside the root that executes later" hole, which the path boundary alone
cannot see.

Combined with `--permission-prompts none`, anything neither allowed nor denied is
**denied automatically** — the default is closed.

### 4.4 Layer B — the `PreToolUse` guard hook (hard boundary)

Registered with **no matcher** so it sees every tool call, including subagents' (P7).

```python
# app/security/hook_guard.py — entry point, run by the CLI, one process per tool call
def main() -> int:
    try:
        payload = json.load(sys.stdin)
        policy  = RunPolicy.load(sys.argv[1])          # 0600 file written at launch
        verdict = check(payload["tool_name"], payload.get("tool_input", {}), policy)
        policy.audit(payload, verdict)                 # append-only JSONL, reconciled later
        if verdict.allowed:
            print("{}")                                # no decision: normal rules still apply
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": verdict.reason}}))
        return 0
    except BaseException as exc:                       # ← fail CLOSED (probe P2 vs P3)
        print(f"harness guard internal error: {exc!r}", file=sys.stderr)
        return 2
```

Two properties are load-bearing and both are covered by `tests/test_hook_guard.py`:
the bare `except BaseException` returning **2**, and the allow path printing `{}` rather
than an `"allow"` decision — the guard only ever vetoes, so Layer A still applies
underneath it.

`--restricted` is enabled by default on top of this (`tasks.restricted_mode`). It is the
CLI's own version of the same idea — file tools confined to the working directories,
`bypassPermissions` refused, user/project/local settings ignored, command-running tools
and WebFetch dropped unless `--tools` names them. It overlaps Layers A and B deliberately:
three independent mechanisms, any two of which can fail.

### 4.5 Layer C — settings isolation

Default `--setting-sources ""`. This matters: with `project` loaded, a
`.claude/settings.local.json` **inside the target directory** could carry
`permissions.allow` rules and pre-approve tools — an untrusted checkout would be
self-granting. Consequences, both surfaced in the UI:

- project `CLAUDE.md` is not loaded,
- project-local skills are not discovered,
- and, verified in the probes, `apiKeyHelper` from `~/.claude/settings.json` is not
  loaded either, so the runner must re-supply auth (helper path or `ANTHROPIC_API_KEY`)
  inside the generated `--settings` / environment.

Tasks that enable skills use `--setting-sources user`. A per-task **"trust this project's
settings"** checkbox opts into `user,project`, with the shadowing risk stated next to it.
Layer B is unaffected either way: `--settings` (the flag layer) always wins, and nothing
in a project file can unregister our hook.

### 4.6 Path extraction and the containment test

`security/tool_paths.py` holds an explicit per-tool table; unknown tools **fail closed**.

| Tool | Path fields | Class |
| --- | --- | --- |
| `Write`, `Edit`, `NotebookEdit` | `file_path` | write |
| `Read` | `file_path` | read |
| `Glob`, `Grep` | `path` | read |
| `Bash`, `BashOutput`, `KillShell` | — (see § 4.8) | exec |
| `WebFetch`, `WebSearch` | — | network (per-task, default deny) |
| `Task`/`Agent` | — | allowed; the subagent's own calls are hooked |
| `TodoWrite`, `Skill` | — | allowed, no filesystem effect |
| `mcp__*` | unknown schema | § 4.7 |
| anything else | — | **deny**, logged as `unknown_tool` |

```python
def resolve_candidate(p: str, cwd: Path) -> Path:
    q = Path(p).expanduser()
    q = cwd / q if not q.is_absolute() else q
    # realpath the deepest existing ancestor, then re-attach the not-yet-created tail,
    # so new files are checkable and a symlinked *parent* is still caught
    anc, tail = q, []
    while not anc.exists():
        tail.append(anc.name); anc = anc.parent
    return Path(os.path.realpath(anc)).joinpath(*reversed(tail))

def contained(p: Path, root: Path) -> bool:
    return p == root or root in p.parents      # NOT str.startswith — "/proj-evil" vs "/proj"
```

- **Writes** must be contained in `root`.
- **Reads** must be contained in `root` or in one of `task.extra_read_paths` (each
  validated identically, and also passed as `--add-dir` so the CLI agrees with us).

**Explicit design decision on reads.** Read scoping is on by default, symmetrical with
writes. The CLI supports it (`Read(...)` allow/deny rules plus `--restricted` confinement),
and the hook makes it binding. The cost is real: a task needing `~/.config` or a sibling
library must list it in `extra_read_paths`. I chose confinement-by-default because the
panel's premise is unattended sessions against arbitrary local directories, and
unconfined reads plus any network tool is an exfiltration path. The per-task escape hatch
keeps it usable.

### 4.7 MCP tools

`mcp__<server>__<tool>` inputs have server-defined schemas, so paths cannot be extracted
generically. Policy: tools from a server with `trusted = 0` are **denied by the guard**.
Marking a server trusted is a deliberate UI action that states plainly that its tools run
outside the path boundary. A filesystem-type MCP server is exactly the hole that would
otherwise make § 4.6 decorative.

### 4.8 Bash — the hard case

A shell command can write anywhere and static analysis of shell is not a security
boundary. Three measures, in order of strength:

1. **OS sandbox**, in the generated settings:
   `"sandbox": {"enabled": true, "autoAllowBashIfSandboxed": false,
   "allowUnsandboxedCommands": false, "excludedCommands": []}`. This is macOS
   `sandbox-exec` / Linux bubblewrap enforcement and it is the only real containment for
   `Bash`. `allowUnsandboxedCommands: false` stops the model opting out.
   Caveat found while probing: nested sandboxing fails with
   `sandbox-exec: sandbox_apply: Operation not permitted` (exit 71) when the harness
   itself runs inside a sandbox. The runner detects that signature and fails the run with
   an actionable message instead of continuing unprotected.
2. **`--restricted`**, which drops `Bash` entirely unless `--tools` names it. For tasks
   that do not need a shell this is the cleanest answer, and it is the default for new
   tasks.
3. **Narrow `Bash(cmd:*)` allow rules plus a conservative textual screen** in the guard:
   deny commands containing absolute paths outside root, `sudo`, `curl … | sh`, or writes
   to `~/.claude`, `~/.ssh`, `/etc`. This is a speed bump, explicitly *not* the boundary,
   and labelled as such in the code.

If `sandbox_bash` is off, the UI marks the task "Bash unconfined" in red and the run
detail records it.

### 4.9 Residual risks, stated rather than hidden

- **Silent config failure.** The biggest CLI-specific risk: an invalid settings blob or
  matcher removes the guard without an error (P1, and the documented "silently ignored"
  behaviour of `-p`). Mitigated by the § 3.4 preflight assertions and the hook canary, and
  by generating settings from typed objects rather than string templates. This risk
  simply does not exist with in-process SDK callbacks, and it is the honest cost of the
  CLI approach.
- **Hook fail-open on the wrong exit code** (P2). Mitigated by the `except BaseException →
  exit 2` contract and a test that pipes garbage at the guard.
- **TOCTOU.** The guard resolves a path, the CLI writes moments later; a symlink swapped
  in between defeats the check. Layer 4.8's OS sandbox is the durable answer for `Bash`;
  for `Write`/`Edit` the window is small but real, and not closable out-of-process.
- **Per-call process cost.** The guard runs as a fresh process on every tool call; it must
  stay import-light (no FastAPI, no SQLAlchemy) or it will add visible latency. Audit rows
  are appended to a JSONL file and ingested by the runner, not written to SQLite from the
  guard.
- **Environment inheritance.** The subprocess starts from a scrubbed base (drops `AWS_*`,
  `GITHUB_TOKEN`, unrelated `ANTHROPIC_*`) with only what the model config specifies added
  back.
- `path_guard` correctness is the crux, so `tests/test_path_guard.py` covers `..`
  traversal, symlinked parent, symlinked root, `/proj` vs `/proj-evil` prefix, relative
  paths, `~` expansion, NUL bytes, non-existent deep paths, case-insensitive APFS
  collisions, and every tool in the extractor table.

---

## 5. Build order

1. Skeleton: app factory, DB, models, then `path_guard` + `rules` + `hook_guard` with
   their tests. Security first, before anything can spawn a CLI.
2. `services/cli.py` + `settings_builder.py` + `stream.py`, tested against a fake
   `claude` script and recorded stream-json fixtures — no network, no cost.
3. CRUD routers + schemas for skills / mcps / models / roles / teams.
4. Two-panel frontend shell + the five management views.
5. Tasks: CRUD, path validation endpoint + directory picker, inline team creation.
6. Runner: spawn, preflight + canary, event pump, cancel, reap.
7. SSE streaming + run history / permission-decision UI.
8. Skill import, MCP connection test, model test round-trip, suggestion catalogs.

---

## 6. Open questions

1. **Read scoping** (§ 4.6): confine reads to the project root by default as proposed, or
   confine only writes?
2. **`--restricted` by default** (§ 4.4): it drops `Bash` and `WebFetch` unless explicitly
   re-enabled per task. Safe default, but many realistic tasks need a shell — default on
   (proposed) or default off?
3. **Multi-role execution** (§ 3.5): one CLI process with the lead delegating via
   `--agents` (proposed), or one process per role run sequentially with handoff?
4. **Denied-tool behaviour**: hard-deny and let the model route around it (proposed), or
   surface an approval prompt in the UI and block the run until you answer? The latter
   means replacing `--permission-prompts none` with an MCP-backed
   `--permission-prompt-tool` that calls into the harness — real work, so worth deciding
   before step 6.
5. **Local model gateway** (§ 3.6): is standing up a LiteLLM/router proxy acceptable, or
   should the Models panel offer Anthropic only and mark local providers unsupported?
