import { api, confirmDelete, el, emptyState, field, toast } from "../lib.js";

export async function render(panel, arg) {
  const [roles, suggested] = await Promise.all([
    api("/api/roles"),
    api("/api/roles/suggested"),
  ]);
  const editing = arg ? roles.find((r) => String(r.id) === arg) : null;

  panel.replaceChildren(
    el("h2", {}, "Roles"),
    el("p", { class: "sub" },
      "A role is a name, a description and the system prompt that defines its behaviour. " +
      "The description is what the lead role sees when deciding whom to delegate to."),

    el("h3", {}, `Configured (${roles.length})`),
    roles.length
      ? el("div", {}, roles.map((role) => card(role, panel)))
      : emptyState("No roles yet. Add one below, or start from a suggestion."),

    suggested.length ? el("h3", {}, "Suggested roles") : null,
    suggested.length
      ? el("div", {}, suggested.map((role) =>
          el("div", { class: "card" },
            el("div", { class: "card-head" },
              el("strong", {}, role.name),
              el("div", { class: "card-actions" },
                el("button", {
                  class: "btn ghost",
                  onclick: async () => {
                    await api("/api/roles", { method: "POST", body: role });
                    toast(`Added ${role.name}`);
                    render(panel);
                  },
                }, "Add"))),
            el("p", {}, role.description))))
      : null,

    el("h3", {}, editing ? `Edit ${editing.name}` : "Add a role"),
    form(editing, panel)
  );
}

function card(role, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, role.name),
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/roles/${role.id}`; },
        }, "Edit"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`role ${role.name}`)) return;
            try {
              await api(`/api/roles/${role.id}`, { method: "DELETE" });
              toast("Deleted");
              location.hash = "#/roles";
              render(panel);
            } catch (error) {
              toast(error.message, true);
            }
          },
        }, "Delete"))),
    el("p", {}, role.description || "No description."),
    el("details", {}, el("summary", {}, "System prompt"),
      el("pre", { class: "mono" }, role.system_prompt)));
}

function form(editing, panel) {
  return el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      const body = {
        name: data.get("name"),
        description: data.get("description") || "",
        system_prompt: data.get("system_prompt"),
      };
      try {
        if (editing) await api(`/api/roles/${editing.id}`, { method: "PUT", body });
        else await api("/api/roles", { method: "POST", body });
        toast("Saved");
        location.hash = "#/roles";
        render(panel);
      } catch (error) {
        toast(error.message, true);
      }
    },
  },
    field("Name", el("input", { name: "name", required: true, value: editing?.name || "",
      placeholder: "Software Engineer" })),
    field("Description", el("input", { name: "description", value: editing?.description || "" }),
      "One line. Shown to the lead role when it picks a teammate."),
    field("System prompt",
      el("textarea", { name: "system_prompt", required: true,
        value: editing?.system_prompt || "" }),
      "Becomes the subagent's instructions, or the appended lead prompt when this role leads."),
    el("div", {}, el("button", { class: "btn", type: "submit" },
      editing ? "Save role" : "Add role"))
  );
}
