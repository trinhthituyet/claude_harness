Build a web application that acts as a control panel for triggering and configuring Claude Code sessions.

## Tech Stack

- **Backend:** Python + FastAPI
- **Frontend:** simple (plain HTML/JS/CSS, or a minimal framework like HTMX or React — your choice, keep it lightweight)
- **Persistence:** SQLite
- **Single local user, no auth required**

## Layout

Two-panel UI — a left sidebar with navigation categories, and a right content panel showing details/management UI for whichever category is selected.

## Left Sidebar Categories

Click to switch the right panel.

### 1. Skills
Show currently installed/available skills in the system. Also show a "Suggested Skills" section (skills not yet added). Allow the user to add new skills (via form or file upload, depending on how skills are packaged).

### 2. MCPs
Show currently installed MCP servers. Also show a "Suggested MCP Servers" section. Allow the user to add a new MCP server (e.g., by providing a config: command, args, env vars, or a registry URL).

### 3. Models
Let the user choose which LLM backend to use for a session:
- Local LLMs served via vLLM or Ollama (need endpoint URL + model name config)
- Anthropic models (model selection dropdown, API key config)

### 4. Roles
Show configured roles (e.g., Software Architect, Software Engineer, Designer, Tester). Allow creating new roles, where each role has a name, description, and an associated system prompt / instructions defining its behavior.

### 5. Teams
Show configured teams. Each team is a named group composed of one or more roles. Allow creating a new team by selecting from existing roles.

### 6. Tasks
Show configured tasks. Each task has:
- A prompt (the actual instruction/goal to run)
- A **project path** — the user selects (or types) the local filesystem directory this task should operate on
- An assigned team — either select an existing team, or define a new one inline (choosing roles, or creating new roles on the fly)
- A way to trigger execution of the task

## Trigger Mechanism (Claude Code CLI)

- Shell out to the `claude` CLI in headless mode (a subprocess per run) rather than running the session in-process via the Claude Agent SDK.
- Configure the CLI invocation with:
  - The subprocess working directory set to the task's selected **project path**
  - The task's prompt, combined with role instructions for each team member in the assigned team (composed into the system prompt flags / custom agent definitions as appropriate)
  - The selected model config (Anthropic model, or local vLLM/Ollama endpoint if the CLI supports custom base URLs — flag clearly if not natively supported and propose a workaround)
  - Enabled skills and MCP servers, wired in via the CLI's supported flags and settings
- **Permissions / sandboxing:** Restrict the session so it can only write within the selected project path.
  - Use the CLI's permission configuration (permission rules and modes, and `PreToolUse` hooks) to allow file write/edit tools only when the target path resolves inside the project directory, and deny (or require explicit approval) for anything outside it.
  - Read access can be scoped similarly if the CLI supports it — call this out explicitly as a design decision.
  - Treat this as a hard security boundary, not just a UI hint: validate resolved absolute paths on every write-tool call, not just at session start.
- Stream the CLI's incremental output/events live to the frontend (via WebSocket or Server-Sent Events).
- Store task run history (prompt used, project path, output, status, timestamp) in SQLite.

## Functional Requirements

- SQLite-backed persistence for skills, mcps, models, roles, teams, tasks, and task run history — survives restarts.
- FastAPI backend exposing REST endpoints for CRUD on each entity, plus an endpoint/websocket to trigger a task and stream live output.
- Project path selection should validate the path exists and is a directory before allowing a task to run.
- Sensible empty states and validation (e.g., can't create a team with zero roles, can't run a task with no prompt, can't run a task with no project path).

## Before Writing Code, Propose:

1. The data model (tables/schema for skills, mcps, models, roles, teams, tasks, task_runs, and their relationships — include `project_path` on tasks)
2. The FastAPI project structure (routers, models, services)
3. How the CLI subprocess will be constructed and run from FastAPI (process lifecycle, sync vs async execution model, how the CLI's streaming output is bridged to WebSocket/SSE)
4. The exact permission-scoping mechanism you'll use to confine writes to the project path — inspect the actual CLI's permission flags, settings schema and hook API first rather than assuming their shape, since this is a security-critical piece