import { api, checkbox, confirmDelete, el, emptyState, field, mount, toast } from "../lib.js";

export async function render(panel, arg) {
  const [tasks, teams, roles, models, skills, mcps, workflows] = await Promise.all([
    api("/api/tasks"),
    api("/api/teams"),
    api("/api/roles"),
    api("/api/models"),
    api("/api/skills"),
    api("/api/mcps"),
    api("/api/workflows"),
  ]);
  const editing = arg ? tasks.find((t) => String(t.id) === arg) : null;

  mount(panel,
    el("h2", {}, "Tasks"),
    el("p", { class: "sub" },
      "A task is a prompt, a project directory and a team. Running it starts one Claude " +
      "Code session confined to that directory."),

    el("h3", {}, `Configured (${tasks.length})`),
    tasks.length
      ? el("div", {}, tasks.map((task) => card(task, panel)))
      : emptyState("No tasks yet. Create one below."),

    el("h3", {}, editing ? `Edit ${editing.name}` : "Create a task"),
    teams.length || roles.length
      ? form(editing, { teams, roles, models, skills, mcps, workflows }, panel)
      : emptyState("Add at least one role first: a task needs a team, and a team needs a role.")
  );
}

function card(task, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, task.name),
      el("span", { class: task.workflow_name ? "tag lead" : "tag" },
        task.workflow_name ? `workflow: ${task.workflow_name}` : task.team_name),
      task.paranoid_mode ? el("span", { class: "tag warn" }, "paranoid") : null,
      task.sandbox_bash ? null : el("span", { class: "tag danger" }, "Bash unconfined"),
      task.network_enabled ? el("span", { class: "tag warn" }, "network") : null,
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn",
          onclick: async (event) => {
            event.target.disabled = true;
            try {
              const { run_id } = await api(`/api/tasks/${task.id}/run`, { method: "POST" });
              location.hash = `#/runs/${run_id}`;
            } catch (error) {
              toast(error.message, true);
              event.target.disabled = false;
            }
          },
        }, "Run"),
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/tasks/${task.id}`; },
        }, "Edit"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`task ${task.name}`)) return;
            await api(`/api/tasks/${task.id}`, { method: "DELETE" });
            toast("Deleted");
            location.hash = "#/tasks";
            render(panel);
          },
        }, "Delete"))),
    el("p", { class: "mono" }, task.project_path),
    el("p", {}, task.prompt.length > 220 ? `${task.prompt.slice(0, 220)}…` : task.prompt));
}

function multiSelect(name, options, selectedIds, labelOf) {
  const select = el("select", { name, multiple: true, size: Math.min(6, Math.max(2, options.length)) },
    options.map((option) =>
      el("option", {
        value: option.id,
        selected: selectedIds.includes(option.id),
      }, labelOf(option))));
  return select;
}

function form(editing, data, panel) {
  const { teams, roles, models, skills, mcps, workflows } = data;

  // --- project path, with live validation -------------------------------
  const pathInput = el("input", {
    name: "project_path", required: true, value: editing?.project_path || "",
    placeholder: "/Users/you/code/project",
  });
  const pathState = el("div", { class: "path-state" }, "");
  let pathOk = !!editing;
  let timer = null;
  async function validatePath() {
    const value = pathInput.value.trim();
    if (!value) { pathState.textContent = ""; pathState.className = "path-state"; pathOk = false; return; }
    try {
      const result = await api(`/api/fs/validate?path=${encodeURIComponent(value)}`);
      pathOk = result.ok;
      pathState.className = `path-state ${result.ok ? "ok" : "bad"}`;
      pathState.textContent = result.ok
        ? `✓ ${result.resolved}${result.writable ? "" : " (not writable)"}`
        : `✗ ${result.error}`;
    } catch (error) {
      pathOk = false;
      pathState.className = "path-state bad";
      pathState.textContent = `✗ ${error.message}`;
    }
    runButton.disabled = !pathOk;
  }
  pathInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(validatePath, 250);
  });

  // --- team: existing or inline ----------------------------------------
  const teamSelect = el("select", { name: "team_id" },
    el("option", { value: "" }, teams.length ? "— choose a team —" : "— no teams yet —"),
    teams.map((team) =>
      el("option", { value: team.id, selected: editing?.team_id === team.id },
        `${team.name} (${team.members.length})`)),
    el("option", { value: "__inline__" }, "+ define a new team inline"),
    workflows.length
      ? el("optgroup", { label: "Workflows (a graph of roles)" },
          workflows.map((w) =>
            el("option", {
              value: `wf:${w.id}`,
              selected: editing?.workflow_id === w.id,
            }, `${w.name} (${w.nodes.length} steps)`)))
      : null);

  const inlineName = el("input", { name: "inline_name", placeholder: "Feature squad" });
  const inlineRoles = multiSelect("inline_role_ids", roles, [], (r) => r.name);
  const inlineLead = el("input", { name: "inline_lead", placeholder: "Role name that leads" });
  const newRoleName = el("input", { placeholder: "New role name" });
  const newRoleDesc = el("input", { placeholder: "What this role is for" });
  const newRolePrompt = el("textarea", { placeholder: "System prompt for the new role" });
  const newRoles = [];
  const newRoleList = el("div", { class: "mono" }, "");
  const inlineBlock = el("details", { class: "editor" },
    el("summary", {}, "New team"),
    field("Team name", inlineName),
    field("Existing roles (ctrl/cmd-click for several)", inlineRoles),
    field("Lead role name", inlineLead, "Defaults to the first selected role."),
    el("h3", {}, "Create a role on the fly"),
    el("div", { class: "row" }, field("Name", newRoleName), field("Description", newRoleDesc)),
    field("System prompt", newRolePrompt),
    el("div", {}, el("button", {
      class: "btn ghost", type: "button",
      onclick: () => {
        if (!newRoleName.value.trim() || !newRolePrompt.value.trim()) {
          toast("A new role needs a name and a system prompt", true);
          return;
        }
        newRoles.push({
          name: newRoleName.value.trim(),
          description: newRoleDesc.value.trim(),
          system_prompt: newRolePrompt.value.trim(),
        });
        newRoleList.textContent = `queued: ${newRoles.map((r) => r.name).join(", ")}`;
        newRoleName.value = newRoleDesc.value = newRolePrompt.value = "";
      },
    }, "Queue this role")),
    newRoleList);
  inlineBlock.hidden = true;
  teamSelect.addEventListener("change", () => {
    inlineBlock.hidden = teamSelect.value !== "__inline__";
    if (!inlineBlock.hidden) inlineBlock.open = true;
  });

  // --- model, skills, servers ------------------------------------------
  const modelSelect = el("select", { name: "model_config_id" },
    el("option", { value: "" }, "— default model config —"),
    models.map((model) =>
      el("option", { value: model.id, selected: editing?.model_config_id === model.id },
        `${model.name} (${model.model_id})`)));

  const installedSkills = skills.filter((s) => s.status === "installed");
  const skillSelect = multiSelect("skill_ids", installedSkills,
    editing?.skill_ids || [], (s) => s.name);
  const mcpSelect = multiSelect("mcp_server_ids", mcps,
    editing?.mcp_server_ids || [], (s) => `${s.name}${s.trusted ? " (trusted)" : ""}`);

  // --- security switches ------------------------------------------------
  const paranoid = checkbox(
    "Paranoid mode", "paranoid_mode", editing?.paranoid_mode,
    "Denies anything not pre-approved. Disables approval prompts entirely: out-of-root " +
    "writes are refused rather than asked about."
  );
  const sandbox = checkbox(
    "Sandbox Bash", "sandbox_bash", editing ? editing.sandbox_bash : true,
    "OS-level sandbox. The only real containment for shell commands — leave this on."
  );
  const network = checkbox(
    "Allow WebFetch / WebSearch", "network_enabled", editing?.network_enabled,
    "Reads are unconfined, so network access means the session could send readable files out."
  );
  const trustProject = checkbox(
    "Trust this project's settings files", "trust_project_settings",
    editing?.trust_project_settings,
    "Loads the project's .claude/settings*.json — which can pre-approve tools. Only for " +
    "directories you control."
  );
  const timeout = el("input", {
    type: "number", name: "approval_timeout_s", min: 10, max: 3600,
    value: editing?.approval_timeout_s ?? 300,
  });
  const timeoutField = field("Approval timeout (seconds)", timeout,
    "An unanswered approval is denied when this expires.");
  function syncParanoid() {
    timeout.disabled = paranoid.input.checked;
    timeoutField.style.opacity = paranoid.input.checked ? 0.5 : 1;
  }
  paranoid.input.addEventListener("change", syncParanoid);
  syncParanoid();

  const runButton = el("button", { class: "btn", type: "submit" },
    editing ? "Save task" : "Create task");

  const form = el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const data_ = new FormData(event.target);
      const body = {
        name: data_.get("name"),
        prompt: data_.get("prompt"),
        project_path: pathInput.value.trim(),
        model_config_id: modelSelect.value ? Number(modelSelect.value) : null,
        skill_ids: [...skillSelect.selectedOptions].map((o) => Number(o.value)),
        mcp_server_ids: [...mcpSelect.selectedOptions].map((o) => Number(o.value)),
        paranoid_mode: paranoid.input.checked,
        sandbox_bash: sandbox.input.checked,
        network_enabled: network.input.checked,
        trust_project_settings: trustProject.input.checked,
        approval_timeout_s: Number(timeout.value) || 300,
        max_turns: null,
        max_budget_usd: null,
      };
      if (teamSelect.value.startsWith("wf:")) {
        body.workflow_id = Number(teamSelect.value.slice(3));
      } else if (teamSelect.value === "__inline__") {
        body.inline_team = {
          name: inlineName.value.trim(),
          description: "",
          role_ids: [...inlineRoles.selectedOptions].map((o) => Number(o.value)),
          new_roles: newRoles,
          lead_role_name: inlineLead.value.trim() || null,
        };
      } else if (teamSelect.value) {
        body.team_id = Number(teamSelect.value);
      } else {
        toast("Choose a team or a workflow, or define a team inline", true);
        return;
      }
      try {
        if (editing) await api(`/api/tasks/${editing.id}`, { method: "PUT", body });
        else await api("/api/tasks", { method: "POST", body });
        toast("Saved");
        location.hash = "#/tasks";
        render(panel);
      } catch (error) {
        toast(error.message, true);
      }
    },
  },
    field("Name", el("input", { name: "name", required: true, value: editing?.name || "" })),
    field("Prompt", el("textarea", { name: "prompt", required: true,
      value: editing?.prompt || "" }), "The instruction the session runs."),
    el("label", {}, "Project path",
      el("span", { class: "hint" }, "Writes are confined here. Must exist and be a directory."),
      pathInput, pathState),
    el("div", { class: "row" },
      field("Team or workflow", teamSelect,
        "A team runs one session with the lead delegating. A workflow runs the graph, " +
        "one session per step."),
      field("Model", modelSelect)),
    inlineBlock,
    el("div", { class: "row" },
      field("Skills", skillSelect, installedSkills.length ? null : "None installed."),
      field("MCP servers", mcpSelect, mcps.length ? null : "None configured.")),
    el("h3", {}, "Permissions"),
    sandbox.label,
    paranoid.label,
    network.label,
    trustProject.label,
    timeoutField,
    el("div", {}, runButton)
  );

  if (editing) validatePath();
  return form;
}
