// Shared helpers: tiny DOM builder, API client, toasts.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  const formField = tag === "input" || tag === "textarea" || tag === "select";
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "value" && formField) {
      // textarea and select ignore a value *attribute*; set the property.
      node.value = value;
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function toast(message, bad = false) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.className = bad ? "toast bad" : "toast";
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { box.hidden = true; }, bad ? 7000 : 3500);
}

export async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(
      typeof detail === "string" ? detail : JSON.stringify(detail ?? `HTTP ${response.status}`)
    );
  }
  return data;
}

export function emptyState(message) {
  return el("div", { class: "empty" }, message);
}

export function field(labelText, control, hint) {
  return el("label", {}, labelText, hint ? el("span", { class: "hint" }, hint) : null, control);
}

export function checkbox(labelText, name, checked, hint) {
  const input = el("input", { type: "checkbox", name, checked: !!checked });
  const label = el("label", { class: "check" }, input, el("span", {}, labelText));
  if (hint) label.append(el("span", { class: "hint" }, hint));
  return { label, input };
}

export function confirmDelete(what) {
  return window.confirm(`Delete ${what}? This cannot be undone.`);
}

export function shortJson(value, limit = 160) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? {});
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function when(timestamp) {
  if (!timestamp) return "—";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? String(timestamp) : date.toLocaleString();
}
