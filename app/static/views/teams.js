import { api, confirmDelete, el, emptyState, field, toast } from "../lib.js";

export async function render(panel, arg) {
  const [teams, roles] = await Promise.all([api("/api/teams"), api("/api/roles")]);
  const editing = arg ? teams.find((t) => String(t.id) === arg) : null;

  panel.replaceChildren(
    el("h2", {}, "Teams"),
    el("p", { class: "sub" },
      "A team is one or more roles. At run time the lead role drives the session and " +
      "delegates to the others as subagents."),

    el("h3", {}, `Configured (${teams.length})`),
    teams.length
      ? el("div", {}, teams.map((team) => card(team, panel)))
      : emptyState("No teams yet. A team needs at least one role."),

    el("h3", {}, editing ? `Edit ${editing.name}` : "Create a team"),
    roles.length
      ? form(editing, roles, panel)
      : emptyState("Add at least one role first — a team cannot be empty.")
  );
}

function card(team, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, team.name),
      el("span", { class: "tag" }, `${team.members.length} role${team.members.length > 1 ? "s" : ""}`),
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/teams/${team.id}`; },
        }, "Edit"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`team ${team.name}`)) return;
            try {
              await api(`/api/teams/${team.id}`, { method: "DELETE" });
              toast("Deleted");
              location.hash = "#/teams";
              render(panel);
            } catch (error) {
              toast(error.message, true);
            }
          },
        }, "Delete"))),
    team.description ? el("p", {}, team.description) : null,
    el("p", {}, team.members.map((member) =>
      el("span", { class: `tag ${member.is_lead ? "lead" : ""}` },
        member.is_lead ? `${member.role_name} (lead)` : member.role_name))
      .flatMap((node, index) => (index ? [" ", node] : [node]))));
}

function form(editing, roles, panel) {
  const selected = new Map(
    (editing?.members || []).map((member) => [member.role_id, member.is_lead])
  );

  const list = el("div", {});
  function draw() {
    list.replaceChildren(...roles.map((role) => {
      const checked = selected.has(role.id);
      const box = el("input", {
        type: "checkbox", checked,
        onchange: (event) => {
          if (event.target.checked) {
            selected.set(role.id, selected.size === 0);
          } else {
            const wasLead = selected.get(role.id);
            selected.delete(role.id);
            if (wasLead && selected.size) {
              const [first] = selected.keys();
              selected.set(first, true);
            }
          }
          draw();
        },
      });
      const lead = el("input", {
        type: "radio", name: "lead", checked: selected.get(role.id) === true,
        disabled: !checked,
        onchange: () => {
          for (const key of selected.keys()) selected.set(key, key === role.id);
          draw();
        },
      });
      return el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("label", { class: "check" }, box, el("strong", {}, role.name)),
          el("div", { class: "card-actions" },
            el("label", { class: "check" }, lead, el("span", { class: "hint" }, "lead")))));
    }));
  }
  draw();

  return el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      if (!selected.size) { toast("A team needs at least one role", true); return; }
      const data = new FormData(event.target);
      const body = {
        name: data.get("name"),
        description: data.get("description") || "",
        members: [...selected.entries()].map(([role_id, is_lead]) => ({ role_id, is_lead })),
      };
      try {
        if (editing) await api(`/api/teams/${editing.id}`, { method: "PUT", body });
        else await api("/api/teams", { method: "POST", body });
        toast("Saved");
        location.hash = "#/teams";
        render(panel);
      } catch (error) {
        toast(error.message, true);
      }
    },
  },
    field("Name", el("input", { name: "name", required: true, value: editing?.name || "" })),
    field("Description", el("input", { name: "description", value: editing?.description || "" })),
    el("h3", {}, "Roles"),
    list,
    el("div", {}, el("button", { class: "btn", type: "submit" },
      editing ? "Save team" : "Create team"))
  );
}
