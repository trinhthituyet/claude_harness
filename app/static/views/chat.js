import { api, confirmDelete, el, emptyState, mount, shortJson, toast, when } from "../lib.js";

const EXAMPLES = [
  "Set up a code review task for ~/code/myproject",
  "Create an architect and an engineer role, then a team with the architect leading",
  "Add the git MCP server",
  "Point me at a local Ollama model through my LiteLLM gateway on port 4000",
];

export async function render(panel, arg) {
  if (arg) return renderChat(panel, arg);

  const chats = await api("/api/chat");
  mount(panel,
    el("h2", {}, "Chat"),
    el("p", { class: "sub" },
      "Describe what you want set up and the assistant does it — creating roles, teams, " +
      "MCP servers, model configs and tasks. It asks when something is unclear, and every " +
      "change is confirmed by you before it takes effect."),
    el("div", { class: "note" },
      "The assistant has no filesystem or shell tools at all: it can only call the " +
      "harness's own configuration operations. It cannot delete anything."),

    composer(panel),

    el("h3", {}, `Conversations (${chats.length})`),
    chats.length
      ? el("div", {}, chats.map((chat) => card(chat, panel)))
      : emptyState("No conversations yet. Describe what you want above.")
  );
}

function composer(panel) {
  const input = el("textarea", {
    name: "message",
    placeholder: "e.g. Create a Tester role and a task that runs the test suite in ~/code/api",
  });
  return el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) { toast("Say what you'd like to set up", true); return; }
      const button = event.target.querySelector("button");
      button.disabled = true;
      try {
        const { chat_id } = await api("/api/chat", {
          method: "POST",
          body: { title: text.slice(0, 80), message: text },
        });
        location.hash = `#/chat/${chat_id}`;
      } catch (error) {
        toast(error.message, true);
        button.disabled = false;
      }
    },
  },
    el("label", {}, "What would you like to set up?", input),
    el("p", { class: "sub" },
      ...EXAMPLES.flatMap((example, index) => [
        index ? " " : null,
        el("button", {
          class: "btn ghost", type: "button",
          onclick: () => { input.value = example; input.focus(); },
        }, example),
      ])),
    el("div", {}, el("button", { class: "btn", type: "submit" }, "Start chat"))
  );
}

function card(chat, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, chat.title),
      el("span", { class: `tag ${statusClass(chat.status)}` }, chat.status),
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/chat/${chat.id}`; },
        }, "Open"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`the conversation "${chat.title}"`)) return;
            await api(`/api/chat/${chat.id}`, { method: "DELETE" });
            toast("Conversation deleted");
            render(panel);
          },
        }, "Delete"))),
    el("p", { class: "mono" },
      `${when(chat.updated_at)}` +
      (chat.total_cost_usd ? ` · $${chat.total_cost_usd.toFixed(4)}` : "")));
}

function statusClass(status) {
  if (status === "thinking") return "lead";
  if (status === "awaiting_confirmation") return "warn";
  if (status === "failed") return "danger";
  return "";
}

async function renderChat(panel, chatId) {
  const detail = await api(`/api/chat/${chatId}`);

  const transcript = el("div", { class: "stream" },
    detail.messages.map((message) => line(message)));
  const confirmations = el("div", {});
  const connection = el("div", { class: "note danger" }, "");
  connection.hidden = true;
  const status = el("span", { class: `tag ${statusClass(detail.chat.status)}` },
    detail.chat.status);

  function setConnection(state, message) {
    if (state === "open") {
      connection.hidden = true;
      return;
    }
    connection.hidden = false;
    connection.textContent = message;
  }

  const input = el("textarea", {
    name: "text",
    placeholder: "Reply, or ask for the next change…",
    onkeydown: (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    },
  });
  const sendButton = el("button", { class: "btn", type: "submit" }, "Send");
  const form = el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      sendButton.disabled = true;
      try {
        await api(`/api/chat/${chatId}/messages`, { method: "POST", body: { text } });
        input.value = "";
      } catch (error) {
        toast(error.message, true);
      } finally {
        sendButton.disabled = false;
      }
    },
  }, input, el("div", { class: "card-actions" },
    sendButton,
    el("span", { class: "hint" }, "⌘/Ctrl + Enter to send")));

  mount(panel,
    el("div", { class: "card-head" },
      el("h2", {}, detail.chat.title), status,
      el("div", { class: "card-actions" },
        el("button", { class: "btn ghost", onclick: () => { location.hash = "#/chat"; } },
          "All chats"))),
    detail.chat.error_text ? el("div", { class: "note danger" }, detail.chat.error_text) : null,
    connection,
    confirmations,
    transcript,
    form
  );

  function drawConfirmations(list) {
    confirmations.replaceChildren(...list.map((request) =>
      el("div", { class: "approval" },
        el("h4", {}, `Apply this change? ${request.tool_name}`),
        el("pre", { class: "mono" }, JSON.stringify(request.tool_input, null, 2)),
        el("div", { class: "card-actions" },
          el("button", {
            class: "btn",
            onclick: () => answer(request.id, { approved: true }),
          }, "Apply"),
          el("button", {
            class: "btn danger",
            onclick: () => {
              const reason = window.prompt(
                "Not applying it. Tell the assistant why, or what to do instead:",
                ""
              );
              if (reason === null) return;
              answer(request.id, { approved: false, reason });
            },
          }, "No — tell it why")))));
  }

  async function answer(requestId, body) {
    try {
      await api(`/api/chat/${chatId}/confirmations/${requestId}`, { method: "POST", body });
      confirmations.replaceChildren();
    } catch (error) {
      toast(error.message, true);
    }
  }

  drawConfirmations(detail.pending_confirmations || []);

  // --- live stream ------------------------------------------------------
  const lastSeq = detail.messages.length
    ? detail.messages[detail.messages.length - 1].seq
    : 0;
  const source = new EventSource(`/api/chat/${chatId}/events?last_event_id=${lastSeq}`);
  const pending = new Map();
  const seen = new Set(detail.messages.map((m) => m.seq));

  const TYPES = ["user", "assistant", "thinking", "tool_use", "tool_result", "status",
                 "result", "error"];
  for (const type of TYPES) {
    source.addEventListener(type, (message) => handle(message));
  }
  source.onmessage = (message) => handle(message);
  source.addEventListener("confirmation_request", (message) => {
    const event = JSON.parse(message.data);
    pending.set(event.payload.id, event.payload);
    drawConfirmations([...pending.values()]);
    append(event);
  });
  source.addEventListener("confirmation_resolved", (message) => {
    const event = JSON.parse(message.data);
    pending.delete(event.payload.id);
    drawConfirmations([...pending.values()]);
    append(event);
  });
  source.addEventListener("_eof", () => {
    source.close();
    setConnection("closed", "The server closed this stream. Reload to reconnect.");
  });
  source.onopen = () => setConnection("open");
  source.onerror = () => {
    // EventSource retries on its own unless the connection is CLOSED. Either way the
    // user should see it: a silently dead stream looks exactly like a stuck chat.
    setConnection(
      source.readyState === EventSource.CLOSED ? "closed" : "retrying",
      source.readyState === EventSource.CLOSED
        ? "Disconnected from the server. Reload to reconnect."
        : "Reconnecting…"
    );
  };

  function handle(message) {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    append(event);
  }

  function append(event) {
    if (event.seq < 0 || seen.has(event.seq)) return;
    seen.add(event.seq);
    const atBottom = transcript.scrollTop + transcript.clientHeight
      >= transcript.scrollHeight - 30;
    transcript.append(line(event));
    if (atBottom) transcript.scrollTop = transcript.scrollHeight;
    if (event.type === "status" && event.payload.state) {
      status.textContent = event.payload.state;
      status.className = `tag ${statusClass(event.payload.state)}`;
    }
  }

  input.focus();
  return () => source.close();
}

function line(event) {
  const payload = event.payload || {};
  let text;
  let label = event.type;
  switch (event.type) {
    case "user": text = payload.text; label = "you"; break;
    case "assistant": text = payload.text; break;
    case "thinking": text = payload.text; break;
    case "tool_use": text = `${payload.name} ${shortJson(payload.input, 300)}`; break;
    case "tool_result":
      text = `${payload.is_error ? "error: " : ""}${shortJson(payload.content, 300)}`;
      break;
    case "confirmation_request":
      text = `waiting for you to approve ${payload.tool_name}`;
      break;
    case "confirmation_resolved":
      text = `${payload.outcome}${payload.reason ? ` — ${payload.reason}` : ""}`;
      break;
    case "status": text = payload.state + (payload.note ? ` (${payload.note})` : ""); break;
    case "result":
      text = `${payload.num_turns} turns · $${(payload.total_cost_usd || 0).toFixed(4)}`;
      break;
    case "error": text = payload.message; break;
    default: text = shortJson(payload, 200);
  }
  const cssType = event.type === "confirmation_request" ? "permission"
    : event.type === "user" ? "assistant_text" : event.type;
  return el("div", { class: `ev ${cssType}` }, el("span", { class: "k" }, label), text || "");
}
