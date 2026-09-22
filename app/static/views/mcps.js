import { api, checkbox, confirmDelete, el, emptyState, field, toast } from "../lib.js";

function parseArgs(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

function parseEnv(text) {
  const out = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.includes("=")) continue;
    const [key, ...rest] = trimmed.split("=");
    out[key.trim()] = rest.join("=").trim();
  }
  return out;
}

export async function render(panel, arg) {
  const [servers, suggested] = await Promise.all([
    api("/api/mcps"),
    api("/api/mcps/suggested"),
  ]);
  const editing = arg ? servers.find((s) => String(s.id) === arg) : null;

  panel.replaceChildren(
    el("h2", {}, "MCP servers"),
    el("p", { class: "sub" },
      "Servers selected on a task are passed to the session with strict MCP config, so a " +
      ".mcp.json inside the project cannot add servers of its own."),
    el("div", { class: "note danger" },
      "Marking a server trusted lets its tools run outside the project path boundary: the " +
      "harness cannot inspect MCP tool inputs. Untrusted servers' tools are denied."),

    el("h3", {}, `Installed (${servers.length})`),
    servers.length
      ? el("div", {}, servers.map((server) => card(server, panel)))
      : emptyState("No MCP servers configured yet."),

    el("h3", {}, "Suggested servers"),
    suggested.length
      ? el("div", {}, suggested.map((server) =>
          el("div", { class: "card" },
            el("div", { class: "card-head" },
              el("strong", {}, server.name),
              el("span", { class: "tag" }, server.transport),
              el("div", { class: "card-actions" },
                el("button", {
                  class: "btn ghost",
                  onclick: async () => {
                    await api("/api/mcps", { method: "POST", body: {
                      name: server.name,
                      description: server.description,
                      transport: server.transport,
                      command: server.command,
                      args: server.args || [],
                    } });
                    toast(`Added ${server.name}`);
                    render(panel);
                  },
                }, "Add"))),
            el("p", {}, server.description),
            el("p", { class: "mono" }, [server.command, ...(server.args || [])].join(" ")))))
      : emptyState("Every suggestion is already configured."),

    el("h3", {}, editing ? `Edit ${editing.name}` : "Add a server"),
    form(editing, panel)
  );
}

function card(server, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, server.name),
      el("span", { class: "tag" }, server.transport),
      server.trusted ? el("span", { class: "tag danger" }, "trusted") : null,
      server.enabled ? null : el("span", { class: "tag" }, "disabled"),
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn ghost",
          onclick: async () => {
            const result = await api(`/api/mcps/${server.id}/test`, { method: "POST" });
            toast(`${server.name}: ${result.detail}`, !result.ok);
          },
        }, "Test"),
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/mcps/${server.id}`; },
        }, "Edit"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`MCP server ${server.name}`)) return;
            await api(`/api/mcps/${server.id}`, { method: "DELETE" });
            toast("Deleted");
            location.hash = "#/mcps";
            render(panel);
          },
        }, "Delete"))),
    el("p", {}, server.description || "No description."),
    el("p", { class: "mono" },
      server.transport === "stdio"
        ? [server.command, ...(server.args || [])].join(" ")
        : server.url),
    server.env_keys.length ? el("p", { class: "mono" }, `env: ${server.env_keys.join(", ")}`) : null);
}

function form(editing, panel) {
  const transport = el("select", { name: "transport" },
    ["stdio", "sse", "http"].map((value) =>
      el("option", { value, selected: editing?.transport === value }, value)));
  const trusted = checkbox("Trusted (its tools bypass the path boundary)", "trusted",
    editing?.trusted);
  const enabled = checkbox("Enabled", "enabled", editing ? editing.enabled : true);

  return el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      const body = {
        name: data.get("name"),
        description: data.get("description") || "",
        transport: data.get("transport"),
        command: data.get("command") || null,
        args: parseArgs(data.get("args") || ""),
        env: parseEnv(data.get("env") || ""),
        url: data.get("url") || null,
        headers: parseEnv(data.get("headers") || ""),
        trusted: trusted.input.checked,
        enabled: enabled.input.checked,
      };
      try {
        if (editing) await api(`/api/mcps/${editing.id}`, { method: "PUT", body });
        else await api("/api/mcps", { method: "POST", body });
        toast("Saved");
        location.hash = "#/mcps";
        render(panel);
      } catch (error) {
        toast(error.message, true);
      }
    },
  },
    el("div", { class: "row" },
      field("Name", el("input", { name: "name", required: true, value: editing?.name || "" })),
      field("Transport", transport)),
    field("Description", el("input", { name: "description", value: editing?.description || "" })),
    el("div", { class: "row" },
      field("Command (stdio)", el("input", { name: "command", value: editing?.command || "",
        placeholder: "npx" })),
      field("URL (sse / http)", el("input", { name: "url", value: editing?.url || "",
        placeholder: "https://…" }))),
    el("div", { class: "row" },
      field("Args, one per line", el("textarea", { name: "args",
        value: (editing?.args || []).join("\n") })),
      field("Env, KEY=value per line", el("textarea", { name: "env" }),
        editing?.env_keys?.length
          ? `Stored: ${editing.env_keys.join(", ")}. Leave blank to keep them.`
          : "Values are stored in the local SQLite file.")),
    field("Headers, KEY=value per line", el("textarea", { name: "headers" })),
    trusted.label,
    enabled.label,
    el("div", {}, el("button", { class: "btn", type: "submit" },
      editing ? "Save server" : "Add server"))
  );
}
