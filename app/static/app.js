// App shell: sidebar navigation, hash routing, pending-approval badge.

import { api, el, toast } from "./lib.js";
import * as chat from "./views/chat.js";
import * as skills from "./views/skills.js";
import * as mcps from "./views/mcps.js";
import * as models from "./views/models.js";
import * as roles from "./views/roles.js";
import * as teams from "./views/teams.js";
import * as tasks from "./views/tasks.js";
import * as runs from "./views/runs.js";

const VIEWS = { chat, skills, mcps, models, roles, teams, tasks, runs };
const panel = document.getElementById("panel");
const nav = document.getElementById("nav");
let cleanup = null;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = raw.split("/");
  return { name: VIEWS[name] ? name : "tasks", arg: rest.join("/") || null };
}

async function render() {
  const { name, arg } = parseHash();
  for (const button of nav.querySelectorAll("button")) {
    if (button.dataset.view === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  panel.replaceChildren(el("p", { class: "sub" }, "Loading…"));
  try {
    cleanup = (await VIEWS[name].render(panel, arg)) || null;
  } catch (error) {
    panel.replaceChildren(
      el("h2", {}, "Something went wrong"),
      el("p", { class: "sub" }, error.message)
    );
  }
}

nav.addEventListener("click", (event) => {
  const view = event.target.dataset?.view;
  if (view) location.hash = `#/${view}`;
});

window.addEventListener("hashchange", render);

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    document.getElementById("health").textContent =
      `SDK ${health.sdk_version} · CLI ${health.cli_path} · ${health.live_runs.length} live`;
  } catch {
    document.getElementById("health").textContent = "backend unreachable";
  }
}

async function refreshApprovals() {
  const badge = document.getElementById("approval-badge");
  try {
    const [runs_, chats] = await Promise.all([
      api("/api/runs/pending-approvals"),
      api("/api/chat/pending-confirmations"),
    ]);
    const total = runs_.length + chats.length;
    if (!total) { badge.hidden = true; return; }
    badge.hidden = false;
    const target = runs_.length
      ? `#/runs/${runs_[0].run_id}`
      : `#/chat/${chats[0].chat_id}`;
    const what = runs_.length && chats.length
      ? "approvals and confirmations"
      : runs_.length
        ? `approval${runs_.length > 1 ? "s" : ""}`
        : `confirmation${chats.length > 1 ? "s" : ""}`;
    badge.textContent = `${total} ${what} waiting — click to open`;
    badge.onclick = () => { location.hash = target; };
  } catch {
    badge.hidden = true;
  }
}

if (!location.hash) location.hash = "#/tasks";
render();
refreshHealth();
refreshApprovals();
setInterval(refreshApprovals, 4000);
setInterval(refreshHealth, 15000);

window.addEventListener("unhandledrejection", (event) => {
  toast(event.reason?.message || String(event.reason), true);
});
