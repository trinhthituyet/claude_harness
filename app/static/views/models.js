import { api, checkbox, confirmDelete, el, emptyState, field, mount, toast } from "../lib.js";

const LOCAL_NOTE =
  "Local providers need an Anthropic-API-compatible gateway (LiteLLM proxy or " +
  "claude-code-router) in front of Ollama or vLLM: the session speaks the Anthropic " +
  "Messages API, which those servers do not. Point base URL at the gateway, not at " +
  "http://localhost:11434. Expect weaker tool-call behaviour from local models.";

export async function render(panel, arg) {
  const [configs, anthropic] = await Promise.all([
    api("/api/models"),
    api("/api/models/anthropic-catalog"),
  ]);
  const editing = arg ? configs.find((c) => String(c.id) === arg) : null;

  mount(panel,
    el("h2", {}, "Models"),
    el("p", { class: "sub" }, "Which LLM backend a session uses. One config can be the default."),
    el("div", { class: "note" }, LOCAL_NOTE),

    el("h3", {}, `Configured (${configs.length})`),
    configs.length
      ? el("div", {}, configs.map((config) => card(config, panel)))
      : emptyState("No model configs yet. Add an Anthropic model below to get started."),

    el("h3", {}, editing ? `Edit ${editing.name}` : "Add a model config"),
    form(editing, anthropic, panel)
  );
}

function card(config, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, config.name),
      el("span", { class: "tag" }, config.provider),
      config.is_default ? el("span", { class: "tag lead" }, "default") : null,
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn ghost",
          onclick: async (event) => {
            const button = event.target;
            button.disabled = true;
            button.textContent = "Testing…";
            try {
              const result = await api(`/api/models/${config.id}/test`, { method: "POST" });
              toast(`${config.name}: ${result.detail}`, !result.ok);
            } finally {
              button.disabled = false;
              button.textContent = "Test";
            }
          },
        }, "Test"),
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/models/${config.id}`; },
        }, "Edit"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`model config ${config.name}`)) return;
            await api(`/api/models/${config.id}`, { method: "DELETE" });
            toast("Deleted");
            location.hash = "#/models";
            render(panel);
          },
        }, "Delete"))),
    el("p", { class: "mono" }, config.model_id),
    config.base_url ? el("p", { class: "mono" }, `gateway: ${config.base_url}`) : null,
    config.api_key_masked ? el("p", { class: "mono" }, `key: ${config.api_key_masked}`) : null);
}

function form(editing, anthropicModels, panel) {
  const provider = el("select", { name: "provider" },
    ["anthropic", "ollama", "vllm", "custom"].map((value) =>
      el("option", { value, selected: editing?.provider === value }, value)));

  const modelInput = el("input", {
    name: "model_id", required: true, value: editing?.model_id || "",
    list: "anthropic-models", placeholder: "claude-opus-5",
  });
  const datalist = el("datalist", { id: "anthropic-models" },
    anthropicModels.map((m) => el("option", { value: m.model_id }, m.label)));

  const baseUrl = el("input", {
    name: "base_url", value: editing?.base_url || "", placeholder: "http://localhost:4000",
  });
  const gatewayHint = el("span", { class: "hint" }, "");
  const isDefault = checkbox("Use as the default model", "is_default", editing?.is_default);

  function syncProvider() {
    const local = provider.value !== "anthropic";
    baseUrl.required = local;
    gatewayHint.textContent = local
      ? "Required: the Anthropic-compatible gateway URL, not the raw Ollama/vLLM endpoint."
      : "Leave blank to use the Anthropic API directly.";
  }
  provider.addEventListener("change", syncProvider);
  syncProvider();

  return el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      const body = {
        name: data.get("name"),
        provider: provider.value,
        model_id: data.get("model_id"),
        base_url: data.get("base_url") || null,
        api_key: data.get("api_key") || null,
        extra_env: {},
        is_default: isDefault.input.checked,
      };
      try {
        if (editing) await api(`/api/models/${editing.id}`, { method: "PUT", body });
        else await api("/api/models", { method: "POST", body });
        toast("Saved");
        location.hash = "#/models";
        render(panel);
      } catch (error) {
        toast(error.message, true);
      }
    },
  },
    datalist,
    el("div", { class: "row" },
      field("Name", el("input", { name: "name", required: true, value: editing?.name || "" })),
      field("Provider", provider)),
    field("Model id", modelInput),
    el("label", {}, "Gateway base URL", gatewayHint, baseUrl),
    field("API key / token",
      el("input", { name: "api_key", type: "password",
        placeholder: editing?.api_key_masked || "sk-ant-…" }),
      editing ? "Leave blank to keep the stored key." : null),
    isDefault.label,
    el("div", {}, el("button", { class: "btn", type: "submit" },
      editing ? "Save config" : "Add config"))
  );
}
