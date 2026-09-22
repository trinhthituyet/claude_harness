import { api, confirmDelete, el, emptyState, field, mount, toast } from "../lib.js";

export async function render(panel) {
  const [installed, suggested] = await Promise.all([
    api("/api/skills"),
    api("/api/skills/suggested"),
  ]);
  const live = installed.filter((s) => s.status === "installed");
  const known = installed.filter((s) => s.status !== "installed");

  const form = el(
    "form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        const data = new FormData(event.target);
        const file = data.get("file");
        const body = new FormData();
        if (file && file.size) body.append("file", file);
        if (data.get("name")) body.append("name", data.get("name"));
        if (data.get("body")) body.append("body", data.get("body"));
        try {
          await api("/api/skills/import", { method: "POST", body });
          toast("Skill installed");
          render(panel);
        } catch (error) {
          toast(error.message, true);
        }
      },
    },
    el("div", { class: "row" },
      field("Upload a .zip or SKILL.md", el("input", { type: "file", name: "file",
        accept: ".zip,.md" })),
      field("Name", el("input", { name: "name", placeholder: "my-skill" }),
        "Needed only when pasting markdown or when the file has no frontmatter name.")
    ),
    field("Or paste SKILL.md content",
      el("textarea", { name: "body", placeholder: "---\nname: my-skill\ndescription: …\n---\n\nInstructions…" })),
    el("div", {}, el("button", { class: "btn", type: "submit" }, "Install skill"))
  );

  mount(panel,
    el("h2", {}, "Skills"),
    el("p", { class: "sub" },
      `Skills are directories with a SKILL.md, discovered under ~/.claude/skills. ` +
      `Selecting skills on a task passes them to the session as an explicit list.`),
    el("div", { class: "note" },
      "A skill selection is a context filter, not a sandbox: files of unselected skills " +
      "stay on disk and remain readable by the session."),

    el("h3", {}, `Installed (${live.length})`),
    live.length
      ? el("div", {}, live.map((skill) => card(skill, panel)))
      : emptyState("No skills found on disk yet. Install one below."),

    known.length ? el("h3", {}, `Known but not installed (${known.length})`) : null,
    known.length ? el("div", {}, known.map((skill) => card(skill, panel))) : null,

    el("h3", {}, "Suggested skills"),
    suggested.length
      ? el("div", {}, suggested.map((skill) =>
          el("div", { class: "card" },
            el("div", { class: "card-head" },
              el("strong", {}, skill.name),
              el("div", { class: "card-actions" },
                el("button", {
                  class: "btn ghost",
                  onclick: async () => {
                    await api("/api/skills", {
                      method: "POST",
                      body: { name: skill.name, description: skill.description },
                    });
                    toast(`Added ${skill.name} to the catalog`);
                    render(panel);
                  },
                }, "Add to catalog"))),
            el("p", {}, skill.description))))
      : emptyState("Every suggestion is already present."),

    el("h3", {}, "Install a skill"),
    form
  );
}

function card(skill, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, skill.name),
      el("span", { class: `tag ${skill.status === "installed" ? "ok" : ""}` }, skill.status),
      el("span", { class: "tag" }, skill.scope),
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`the catalog entry for ${skill.name}`)) return;
            await api(`/api/skills/${skill.id}`, { method: "DELETE" });
            toast("Removed from the catalog (files on disk are untouched)");
            render(panel);
          },
        }, "Remove"))),
    el("p", {}, skill.description || "No description."),
    skill.install_path ? el("p", { class: "mono" }, skill.install_path) : null);
}
