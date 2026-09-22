# Claude Harness

A local control panel for configuring and triggering Claude Code sessions. Two panels:
categories on the left, management UI on the right. FastAPI + SQLite + plain ES modules,
with sessions run in-process through the **Claude Agent SDK**.

Design and the reasoning behind the security model: [`docs/DESIGN.md`](docs/DESIGN.md).
Original requirements: [`docs/PROMPT.md`](docs/PROMPT.md).

## Run it

```bash
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:8000>. Single local user, no auth.

```bash
.venv/bin/python -m pytest              # unit + API tests, no network
.venv/bin/python scripts/e2e_check.py   # real SDK session; costs a few cents
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_DB` | `data/harness.db` | SQLite file (created `0600`) |
| `HARNESS_CLI_PATH` | SDK's bundled CLI | Pin a specific `claude` binary |
| `HARNESS_MAX_RUNS` | `3` | Concurrent sessions |
| `HARNESS_API_KEY_HELPER` | auto-detected | See "Authentication" below |

## The panels

1. **Skills** — installed skills discovered under `~/.claude/skills`, a suggested list, and
   import from a `.zip`, a `SKILL.md` upload, or pasted markdown.
2. **MCPs** — stdio / SSE / HTTP servers, a suggested list, a reachability test, and the
   **trusted** flag (see below).
3. **Models** — Anthropic models, or a local vLLM/Ollama gateway. `Test` does a one-token
   round trip and reports the real error.
4. **Roles** — name, description and system prompt. The description is what the lead role
   sees when deciding whom to delegate to.
5. **Teams** — one or more roles, exactly one lead.
6. **Tasks** — prompt, project path (validated live), team (existing or defined inline,
   including roles created on the fly), skills, MCP servers, permission switches, and Run.
7. **Runs** — live event stream over SSE, approval prompts, the permission audit trail, and
   the resolved session options for every past run.

## How a run is confined

Writes are confined to the task's project path. Reads are not — see the trade-off below.

- The project path is `realpath`'d before anything else, so a symlinked root or macOS's
  `/tmp` → `/private/tmp` cannot slip past a comparison.
- A `PreToolUse` hook sees **every** tool call, including ones inside subagents. It vetoes
  but never grants.
- `can_use_tool` is the second, independent check. An out-of-root write returns `ask`,
  which **blocks the run** and raises an approval prompt in the UI; an unanswered prompt is
  **denied** when `approval_timeout_s` expires.
- Deny rules in a generated settings blob cover credential directories, shell rc files,
  `<root>/.git/hooks` and `<root>/.claude` — the paths that are inside the project yet can
  execute later.
- `allowed_tools` is always empty and `skills` is always an explicit list: either would
  auto-approve a tool before `can_use_tool` is consulted.
- `setting_sources` is empty by default, so a `.claude/settings.local.json` inside the
  target project cannot pre-approve tools.
- Bash is confined by the OS sandbox, which is the only real containment for a shell.

Two findings from probing the SDK shaped this, and both are counter-intuitive:

- **A hook callback that raises is fail-open** — the tool runs anyway. So the hook catches
  every exception and converts it into an explicit deny. `tests/test_gate.py` pins this.
- **A `can_use_tool` that raises is fail-closed.** The two mechanisms fail in opposite
  directions, which is why the harness uses both rather than picking one.

### Accepted trade-offs

- **Reads are unconfined.** A session can read anything you can. What limits the damage:
  the credential deny rules, `WebFetch`/`WebSearch` off unless a task enables them, and the
  read audit trail. Enabling network access on a task means the session could send any
  readable file out — the UI says so.
- **A trusted MCP server bypasses the path boundary.** Its tool inputs have server-defined
  schemas the harness cannot inspect, so untrusted servers' tools are denied outright.
- **Paranoid mode** (`permission_mode="dontAsk"`) is closed even if the gate breaks, but it
  disables approval prompts entirely: out-of-root writes are refused, not asked about. It
  is a per-task opt-in.
- **TOCTOU.** A path is resolved, then the tool writes moments later. The OS sandbox covers
  Bash; for `Write`/`Edit` the window is small but real and not closable in-process.

## Authentication

Sessions run with `setting_sources=[]`, which also means `~/.claude/settings.json` is not
loaded. If your installation authenticates through `apiKeyHelper` rather than
`ANTHROPIC_API_KEY`, the harness carries just that one field into the generated settings
blob — auto-detected, or set `HARNESS_API_KEY_HELPER` explicitly.

## Local models (vLLM / Ollama)

The SDK has no custom base-URL option. `ANTHROPIC_BASE_URL` works as plumbing, but the
session speaks the **Anthropic Messages API**, which neither Ollama's native API nor vLLM's
OpenAI-compatible API provides. So a local provider must point at an Anthropic-compatible
gateway — `litellm.config.example.yaml` in this repo is a starting point:

```bash
litellm --config litellm.config.example.yaml --port 4000
```

Then create a model config with provider `ollama`, base URL `http://localhost:4000`, and
the model name the gateway exposes. Expect weaker tool-call behaviour than Claude models.
