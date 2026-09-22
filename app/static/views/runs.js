import { api, el, emptyState, mount, shortJson, toast, when } from "../lib.js";

export async function render(panel, arg) {
  if (arg) return renderRun(panel, arg);
  const runs = await api("/api/runs");
  mount(panel,
    el("h2", {}, "Runs"),
    el("p", { class: "sub" }, "Every session the harness has started, newest first."),
    runs.length
      ? el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Started"), el("th", {}, "Status"), el("th", {}, "Project"),
            el("th", {}, "Turns"), el("th", {}, "Cost"), el("th", {}, "Prompt"))),
          el("tbody", {}, runs.map((run) =>
            el("tr", { class: "clickable", onclick: () => { location.hash = `#/runs/${run.id}`; } },
              el("td", {}, when(run.started_at)),
              el("td", {}, el("span", { class: `tag ${statusClass(run.status)}` }, run.status)),
              el("td", { class: "mono" }, run.project_path_snapshot),
              el("td", {}, run.num_turns ?? "—"),
              el("td", {}, run.total_cost_usd ? `$${run.total_cost_usd.toFixed(4)}` : "—"),
              el("td", {}, shortJson(run.prompt_snapshot, 70))))))
      : emptyState("No runs yet. Trigger a task from the Tasks panel.")
  );
}

function statusClass(status) {
  if (status === "completed") return "ok";
  if (status === "running") return "lead";
  if (status === "failed") return "danger";
  return "warn";
}

async function renderRun(panel, runId) {
  const detail = await api(`/api/runs/${runId}`);
  const run = detail.run;

  const stream = el("div", { class: "stream" },
    detail.events.map((event) => eventLine(event)));
  const approvals = el("div", {});
  const status = el("span", { class: `tag ${statusClass(run.status)}` }, run.status);

  const cancelButton = el("button", {
    class: "btn danger",
    onclick: async () => {
      try {
        await api(`/api/runs/${runId}/cancel`, { method: "POST" });
        toast("Interrupt sent");
      } catch (error) {
        toast(error.message, true);
      }
    },
  }, "Cancel run");
  cancelButton.hidden = !detail.live;

  mount(panel,
    el("div", { class: "card-head" },
      el("h2", {}, "Run"), status,
      el("div", { class: "card-actions" },
        el("button", { class: "btn ghost", onclick: () => { location.hash = "#/runs"; } },
          "All runs"),
        cancelButton)),
    el("p", { class: "sub mono" },
      `${run.project_path_snapshot} · ${when(run.started_at)}` +
      (run.exit_reason ? ` · ${run.exit_reason}` : "")),
    detail.team?.lead
      ? el("p", {}, el("span", { class: "tag lead" }, `lead: ${detail.team.lead}`),
          " ",
          ...(detail.team.roles || [])
            .filter((role) => !role.is_lead)
            .map((role) => el("span", { class: "tag" }, role.name)))
      : null,
    run.error_text ? el("div", { class: "note danger" }, run.error_text) : null,

    approvals,
    el("h3", {}, "Output"),
    stream,

    el("h3", {}, "Permission decisions"),
    detail.decisions.length
      ? el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "When"), el("th", {}, "Layer"), el("th", {}, "Tool"),
            el("th", {}, "Decision"), el("th", {}, "Why"))),
          el("tbody", {}, detail.decisions.map((decision) =>
            el("tr", {},
              el("td", {}, when(decision.ts)),
              el("td", {}, decision.layer),
              el("td", { class: "mono" }, decision.tool_name),
              el("td", {}, el("span", {
                class: `tag ${decision.decision === "deny" ? "danger"
                  : decision.decision === "ask" ? "warn" : "ok"}`,
              }, decision.decision)),
              el("td", {}, decision.reason)))))
      : emptyState("No tool calls were gated yet."),

    el("h3", {}, "Session options"),
    el("pre", { class: "mono" }, JSON.stringify(detail.options || {}, null, 2))
  );

  function drawApprovals(list) {
    approvals.replaceChildren(...list.map((request) =>
      el("div", { class: "approval" },
        el("h4", {}, "Approval needed"),
        el("p", {}, request.reason),
        el("p", { class: "mono" }, `${request.tool_name} → ${(request.resolved_paths || []).join(", ")}`),
        el("div", { class: "card-actions" },
          el("button", {
            class: "btn",
            onclick: () => answer(request.id, { approved: true, remember: false }),
          }, "Allow once"),
          el("button", {
            class: "btn ghost",
            onclick: () => answer(request.id, { approved: true, remember: true }),
          }, "Allow this directory for the run"),
          el("button", {
            class: "btn danger",
            onclick: () => answer(request.id, { approved: false, reason: "denied by user" }),
          }, "Deny")))));
  }

  async function answer(requestId, body) {
    try {
      await api(`/api/runs/${runId}/approvals/${requestId}`, { method: "POST", body });
      approvals.replaceChildren();
    } catch (error) {
      toast(error.message, true);
    }
  }

  drawApprovals(detail.pending_approvals || []);

  // --- live stream ------------------------------------------------------
  const lastSeq = detail.events.length ? detail.events[detail.events.length - 1].seq : 0;
  const source = new EventSource(`/api/runs/${runId}/events?last_event_id=${lastSeq}`);
  const pending = new Map();

  source.addEventListener("_eof", () => {
    source.close();
    cancelButton.hidden = true;
    api(`/api/runs/${runId}`).then((fresh) => {
      status.textContent = fresh.run.status;
      status.className = `tag ${statusClass(fresh.run.status)}`;
    }).catch(() => {});
  });

  source.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    append(event);
  };
  for (const type of ["assistant_text", "thinking", "tool_use", "tool_result", "system",
                      "status", "result", "error", "permission"]) {
    source.addEventListener(type, (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch { return; }
      append(event);
    });
  }
  source.addEventListener("permission_request", (message) => {
    const event = JSON.parse(message.data);
    pending.set(event.payload.id, event.payload);
    drawApprovals([...pending.values()]);
    append(event);
  });
  source.addEventListener("permission_resolved", (message) => {
    const event = JSON.parse(message.data);
    pending.delete(event.payload.id);
    drawApprovals([...pending.values()]);
    append(event);
  });

  const seen = new Set(detail.events.map((e) => e.seq));
  function append(event) {
    if (event.seq < 0 || seen.has(event.seq)) return;
    seen.add(event.seq);
    const atBottom = stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 30;
    stream.append(eventLine(event));
    if (atBottom) stream.scrollTop = stream.scrollHeight;
    if (event.type === "status") {
      status.textContent = event.payload.state || status.textContent;
      status.className = `tag ${statusClass(event.payload.state)}`;
      if (event.payload.state !== "running") cancelButton.hidden = true;
    }
  }

  return () => source.close();
}

function eventLine(event) {
  const payload = event.payload || {};
  let text;
  switch (event.type) {
    case "assistant_text": text = payload.text; break;
    case "thinking": text = payload.text; break;
    case "tool_use": text = `${payload.name} ${shortJson(payload.input, 300)}`; break;
    case "tool_result":
      text = `${payload.is_error ? "error: " : ""}${shortJson(payload.content, 400)}`;
      break;
    case "permission_request": text = `needs approval — ${payload.reason}`; break;
    case "permission_resolved": text = `approval ${payload.outcome}`; break;
    case "permission": text = `${payload.decision || payload.layer}: ${payload.reason || payload.note || ""}`; break;
    case "status": text = `${payload.state}${payload.exit_reason ? ` (${payload.exit_reason})` : ""}`; break;
    case "result":
      text = `${payload.subtype} · ${payload.num_turns} turns · ` +
        `$${(payload.total_cost_usd || 0).toFixed(4)}`;
      break;
    case "error": text = payload.message; break;
    case "system": text = `${payload.subtype} ${shortJson(payload.data, 240)}`; break;
    default: text = shortJson(payload, 240);
  }
  return el("div", { class: `ev ${event.type}` },
    el("span", { class: "k" }, event.type), text || "");
}
