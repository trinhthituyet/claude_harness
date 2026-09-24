// The workflow graph: a read-only diagram, and a draggable editing canvas.
//
// Positions come from the model when the user has placed nodes, and from an automatic
// layered layout when they have not — so a graph built before the canvas existed, or by
// the chat assistant, still opens looking sensible.

import { el, emptyState } from "../lib.js";

export const END = "END";

const SVGNS = "http://www.w3.org/2000/svg";
export const BOX_W = 168;
export const BOX_H = 56;
const COL_W = 230;
const ROW_H = 96;
const PAD = 30;
const GRID = 10;

function svgEl(tag, attrs = {}, text) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Depth from the start node, used only for nodes with no stored position. */
function autoPositions(nodes, edges) {
  const start = nodes.find((n) => n.is_start)?.key || nodes[0]?.key;
  const depth = new Map(nodes.map((n) => [n.key, null]));
  const queue = [[start, 0]];
  while (queue.length) {
    const [key, d] = queue.shift();
    if (key === undefined || depth.get(key) !== null) continue;
    depth.set(key, d);
    for (const e of edges.filter((x) => x.from_key === key)) {
      if (e.to_key && e.to_key !== END && depth.get(e.to_key) === null) {
        queue.push([e.to_key, d + 1]);
      }
    }
  }
  let maxDepth = 0;
  for (const v of depth.values()) if (v !== null) maxDepth = Math.max(maxDepth, v);
  for (const [k, v] of depth) if (v === null) depth.set(k, maxDepth + 1);

  const rows = new Map();
  const out = new Map();
  for (const node of nodes) {
    const d = depth.get(node.key);
    const row = rows.get(d) || 0;
    rows.set(d, row + 1);
    out.set(node.key, { x: PAD + d * COL_W, y: PAD + row * ROW_H });
  }
  return out;
}

/** Final positions: stored where present, auto-laid-out otherwise. */
export function positionsOf(nodes, edges) {
  const auto = autoPositions(nodes, edges);
  const out = new Map();
  for (const node of nodes) {
    const placed =
      typeof node.pos_x === "number" && typeof node.pos_y === "number"
        ? { x: node.pos_x, y: node.pos_y }
        : auto.get(node.key);
    out.set(node.key, { ...placed });
  }
  return out;
}

/** Outgoing edges of the node an edge starts from. */
export function siblingsOf(edges, edge) {
  return edges.filter((e) => e.from_key === edge.from_key);
}

/** Several bare edges out of one node: every arm runs, in parallel. */
export function fansOut(edges, fromKey) {
  const out = edges.filter((e) => e.from_key === fromKey);
  return out.length > 1 && out.every((e) => !(e.condition || "").trim() && !e.is_default);
}

/**
 * True when an edge leaves a *branching* node with nothing to decide it by.
 *
 * A node with several bare edges is a fan-out, which is fine — every arm is taken. What
 * cannot run is a node that mixes described and undescribed edges: the router would have
 * to guess whether the bare one is an arm or a fallback. Server-side validation rejects
 * that, and the canvas shows it in red rather than leaving the reason in a validation box.
 */
export function needsCondition(edges, edge) {
  if ((edge.condition || "").trim() || edge.is_default) return false;
  const siblings = siblingsOf(edges, edge);
  if (siblings.length <= 1) return false;
  return !fansOut(edges, edge.from_key);
}

/** Wrap a condition into short lines for drawing beside an edge. */
export function conditionLines(text, maxChars = 34, maxLines = 2) {
  const clean = (text || "").trim().replace(/\s+/g, " ");
  if (!clean) return [];
  const lines = [];
  let current = "";
  for (const word of clean.split(" ")) {
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length <= maxChars) {
      current = candidate;
      continue;
    }
    if (current) lines.push(current);
    current = word.length > maxChars ? `${word.slice(0, maxChars - 1)}…` : word;
    if (lines.length === maxLines) break;
  }
  if (current && lines.length < maxLines) lines.push(current);
  if (lines.length === maxLines) {
    // Signal that there is more text than the canvas shows.
    const shown = lines.join(" ");
    if (shown.replace(/…$/, "").length < clean.length && !lines[maxLines - 1].endsWith("…")) {
      lines[maxLines - 1] = `${lines[maxLines - 1]}…`;
    }
  }
  return lines;
}


function edgeGeometry(from, to, ends) {
  const x1 = from.x + BOX_W, y1 = from.y + BOX_H / 2;
  if (ends) {
    return { path: `M ${x1} ${y1} L ${x1 + 58} ${y1}`, lx: x1 + 26, ly: y1 - 8,
             endX: x1 + 58, endY: y1, loop: false };
  }
  // Backwards or same column: draw it as a curve above the boxes, so a loop can
  // never be mistaken for a forward step.
  if (to.x <= from.x) {
    const lift = Math.min(from.y, to.y) - 34;
    const sx = from.x + BOX_W / 2, sy = from.y;
    const ex = to.x + BOX_W / 2, ey = to.y;
    return {
      path: `M ${sx} ${sy} C ${sx} ${lift}, ${ex} ${lift}, ${ex} ${ey}`,
      lx: (sx + ex) / 2, ly: lift + 6, loop: true,
    };
  }
  const x2 = to.x, y2 = to.y + BOX_H / 2, mid = (x1 + x2) / 2;
  return {
    path: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
    lx: mid, ly: (y1 + y2) / 2 - 8, loop: false,
  };
}

/**
 * The label block beside an edge: its name, then its condition on following lines.
 *
 * A transparent rect sits behind the text as the hit target, so clicking anywhere in
 * the block selects the edge — clicking a tspan would otherwise miss.
 */
function drawEdgeLabel(edge, geo,
                       { index = null, selected = false, missing = false, parallel = false } = {}) {
  const group = svgEl("g", { class: "g-labelblock" });
  const lines = conditionLines(edge.condition);
  const caption = edge.label + (edge.is_default ? " *" : "");
  const rendered = missing
    ? [...lines, "needs a condition"]
    : parallel ? [...lines, "in parallel"] : lines;

  const widest = Math.max(caption.length, ...rendered.map((l) => l.length), 1);
  const width = widest * 5.9 + 10;
  const height = 13 + rendered.length * 11;
  if (index !== null) {
    group.append(svgEl("rect", {
      x: geo.lx - width / 2, y: geo.ly - 11, width, height, rx: 4,
      fill: "var(--bg)", "fill-opacity": 0.72, stroke: "none",
      class: "g-labelhit", "data-edge": index,
    }));
  }

  const text = svgEl("text", {
    x: geo.lx, y: geo.ly, "text-anchor": "middle",
    class: `g-label${selected ? " selected" : ""}${missing ? " missing" : ""}`,
  });
  text.append(svgEl("tspan", { x: geo.lx }, caption));
  for (const [i, line] of rendered.entries()) {
    text.append(svgEl("tspan", {
      x: geo.lx, dy: 11,
      class: missing && i === rendered.length - 1 ? "g-cond missing" : "g-cond",
    }, missing && i === rendered.length - 1 ? line : `"${line}"`));
  }
  if (edge.condition) {
    const title = svgEl("title");
    title.textContent = edge.condition;
    text.append(title);
  }
  group.append(text);
  return group;
}


function markers() {
  const defs = svgEl("defs");
  for (const [id, colour] of [["arrow", "var(--text-dim)"], ["arrow-loop", "var(--warn)"],
                              ["arrow-sel", "var(--accent)"]]) {
    const marker = svgEl("marker", {
      id, viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
    });
    marker.append(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: colour }));
    defs.append(marker);
  }
  return defs;
}

function extent(positions) {
  let maxX = 0, maxY = 0;
  for (const p of positions.values()) {
    maxX = Math.max(maxX, p.x + BOX_W);
    maxY = Math.max(maxY, p.y + BOX_H);
  }
  return { width: maxX + 110, height: maxY + PAD + 40 };
}

function drawNode(node, at, { selected = false } = {}) {
  const group = svgEl("g", {
    class: `g-node${selected ? " selected" : ""}`, "data-key": node.key,
    transform: `translate(${at.x} ${at.y})`,
  });
  group.append(svgEl("rect", {
    width: BOX_W, height: BOX_H, rx: 8,
    fill: "var(--bg-raised)",
    stroke: selected ? "var(--accent)" : node.is_start ? "var(--accent)" : "var(--border)",
    "stroke-width": selected ? 2.5 : node.is_start ? 2 : 1,
  }));
  group.append(svgEl("text", { x: 11, y: 21, class: "g-key" }, node.key));
  group.append(svgEl("text", { x: 11, y: 36, class: "g-role" },
    `${node.role_name || "?"}${node.max_visits > 1 ? `  ×${node.max_visits}` : ""}`));
  const artifact = node.output_key && node.output_key !== node.key ? node.output_key : null;
  if (artifact) {
    group.append(svgEl("text", { x: 11, y: 47, class: "g-artifact" }, `→ ${artifact}`));
  }
  if (node.is_start) {
    group.append(svgEl("text", { x: BOX_W - 8, y: 15, class: "g-start",
      "text-anchor": "end" }, "start"));
  }
  return group;
}

// ------------------------------------------------------------ read-only diagram

export function staticDiagram(nodes, edges) {
  if (!nodes.length) return emptyState("Add a node to see the graph.");
  const positions = positionsOf(nodes, edges);
  const { width, height } = extent(positions);
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`, class: "graph", role: "img",
    "aria-label": "workflow graph",
  });
  svg.append(markers());

  for (const edge of edges) {
    const from = positions.get(edge.from_key);
    if (!from) continue;
    const ends = !edge.to_key || edge.to_key === END;
    const to = ends ? null : positions.get(edge.to_key);
    if (!ends && !to) continue;
    const geo = edgeGeometry(from, to || from, ends);
    const missing = needsCondition(edges, edge);
    const parallel = fansOut(edges, edge.from_key);
    svg.append(svgEl("path", {
      d: geo.path, fill: "none",
      stroke: missing ? "var(--danger)"
        : parallel ? "var(--ok)"
        : geo.loop ? "var(--warn)" : "var(--text-dim)",
      "stroke-width": edge.is_default ? 2 : parallel ? 1.8 : 1.4,
      "stroke-dasharray": edge.condition || parallel ? null : "5 3",
      "marker-end": geo.loop ? "url(#arrow-loop)" : "url(#arrow)",
    }));
    if (ends) {
      svg.append(svgEl("text", { x: geo.endX + 5, y: geo.endY + 4, class: "g-end" }, END));
    }
    svg.append(drawEdgeLabel(edge, geo, { missing, parallel }));
  }
  for (const node of nodes) svg.append(drawNode(node, positions.get(node.key)));
  return el("div", { class: "graph-wrap" }, svg);
}

// ------------------------------------------------------------- editing canvas

/**
 * An editable canvas.
 *
 * `model` is mutated in place — nodes carry pos_x/pos_y once dragged — and the
 * callbacks tell the panel what happened so it can re-render its side panels and
 * persist. Everything is pointer-event based, so it works with mouse, trackpad and
 * touch without a library.
 */
export function interactiveCanvas(model) {
  const { nodes, edges } = model;
  const wrap = el("div", { class: "graph-wrap editable" });
  if (!nodes.length) {
    wrap.append(emptyState("Add a node to start drawing the graph."));
    return { element: wrap, refresh: () => {} };
  }

  const positions = positionsOf(nodes, edges);
  // Write positions back so a first drag does not jump from auto-layout.
  for (const node of nodes) {
    const at = positions.get(node.key);
    if (typeof node.pos_x !== "number") { node.pos_x = at.x; node.pos_y = at.y; }
  }

  const { width, height } = extent(positions);
  const svg = svgEl("svg", {
    viewBox: `0 0 ${Math.max(width, 620)} ${Math.max(height, 240)}`,
    class: "graph", tabindex: "0",
  });
  wrap.append(svg);

  let dragging = null;   // {key, dx, dy, moved}
  let connecting = null; // {fromKey, rubber}

  function toCanvas(event) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  }

  function snap(value) {
    return Math.max(0, Math.round(value / GRID) * GRID);
  }

  function nodeAt(point) {
    return nodes.find((node) => {
      const at = positions.get(node.key);
      return point.x >= at.x && point.x <= at.x + BOX_W
        && point.y >= at.y && point.y <= at.y + BOX_H;
    });
  }

  function draw() {
    svg.replaceChildren(markers());

    // The END drop target, so "finish here" is reachable by dragging too.
    const endZone = svgEl("g", { class: "g-endzone", "data-endzone": "1" });
    const zoneX = Math.max(...[...positions.values()].map((p) => p.x)) + BOX_W + 46;
    const zoneY = PAD;
    endZone.append(svgEl("rect", {
      x: zoneX, y: zoneY, width: 58, height: 34, rx: 17,
      fill: "none", stroke: "var(--ok)", "stroke-dasharray": "4 3",
    }));
    endZone.append(svgEl("text", { x: zoneX + 29, y: zoneY + 22, class: "g-end",
      "text-anchor": "middle" }, END));
    endZone.dataset.zoneX = zoneX;
    endZone.dataset.zoneY = zoneY;
    svg.append(endZone);

    for (const [index, edge] of edges.entries()) {
      const from = positions.get(edge.from_key);
      if (!from) continue;
      const ends = !edge.to_key || edge.to_key === END;
      const to = ends ? null : positions.get(edge.to_key);
      if (!ends && !to) continue;
      const geo = edgeGeometry(from, to || from, ends);
      const selected = model.selection?.kind === "edge" && model.selection.index === index;
      const missing = needsCondition(edges, edge);
      const parallel = fansOut(edges, edge.from_key);
      svg.append(svgEl("path", {
        d: geo.path, fill: "none",
        stroke: selected ? "var(--accent)"
          : missing ? "var(--danger)"
          : parallel ? "var(--ok)"
          : geo.loop ? "var(--warn)" : "var(--text-dim)",
        "stroke-width": selected ? 2.6 : edge.is_default ? 2 : parallel ? 1.8 : 1.4,
        "stroke-dasharray": edge.condition || parallel ? null : "5 3",
        "marker-end": selected ? "url(#arrow-sel)"
          : geo.loop ? "url(#arrow-loop)" : "url(#arrow)",
      }));
      if (ends) {
        svg.append(svgEl("text", { x: geo.endX + 5, y: geo.endY + 4, class: "g-end" }, END));
      }
      svg.append(drawEdgeLabel(edge, geo, { index, selected, missing, parallel }));
    }

    for (const node of nodes) {
      const selected = model.selection?.kind === "node" && model.selection.key === node.key;
      const group = drawNode(node, positions.get(node.key), { selected });
      // The connect handle: drag from here to another node, or to END.
      const handle = svgEl("circle", {
        cx: BOX_W, cy: BOX_H / 2, r: 6.5, class: "g-handle", "data-handle": node.key,
      });
      const title = svgEl("title");
      title.textContent = "drag to connect";
      handle.append(title);
      group.append(handle);
      svg.append(group);
    }

    if (connecting?.rubber) svg.append(connecting.rubber);
  }

  function resize() {
    const { width: w, height: h } = extent(positions);
    svg.setAttribute("viewBox", `0 0 ${Math.max(w, 620)} ${Math.max(h, 240)}`);
  }

  // --- pointer handling -------------------------------------------------

  svg.addEventListener("pointerdown", (event) => {
    const handleKey = event.target.dataset?.handle;
    if (handleKey) {
      event.preventDefault();
      svg.setPointerCapture(event.pointerId);
      const from = positions.get(handleKey);
      connecting = {
        fromKey: handleKey,
        rubber: svgEl("path", {
          class: "g-rubber", fill: "none", stroke: "var(--accent)",
          "stroke-width": 1.6, "stroke-dasharray": "4 3",
          d: `M ${from.x + BOX_W} ${from.y + BOX_H / 2} L ${from.x + BOX_W} ${from.y + BOX_H / 2}`,
        }),
      };
      draw();
      return;
    }

    const edgeIndex = event.target.dataset?.edge;
    if (edgeIndex !== undefined) {
      model.onSelect?.({ kind: "edge", index: Number(edgeIndex) });
      return;
    }

    const point = toCanvas(event);
    const node = nodeAt(point);
    if (node) {
      event.preventDefault();
      svg.setPointerCapture(event.pointerId);
      const at = positions.get(node.key);
      dragging = { key: node.key, dx: point.x - at.x, dy: point.y - at.y, moved: false };
      return;
    }
    model.onSelect?.(null);
  });

  svg.addEventListener("pointermove", (event) => {
    if (dragging) {
      const point = toCanvas(event);
      const at = positions.get(dragging.key);
      const nx = snap(point.x - dragging.dx);
      const ny = snap(point.y - dragging.dy);
      if (nx !== at.x || ny !== at.y) {
        at.x = nx; at.y = ny;
        dragging.moved = true;
        const node = nodes.find((n) => n.key === dragging.key);
        node.pos_x = nx; node.pos_y = ny;
        resize();
        draw();
      }
      return;
    }
    if (connecting) {
      const point = toCanvas(event);
      const from = positions.get(connecting.fromKey);
      const sx = from.x + BOX_W, sy = from.y + BOX_H / 2;
      connecting.rubber.setAttribute("d", `M ${sx} ${sy} L ${point.x} ${point.y}`);
    }
  });

  function finishPointer(event) {
    if (dragging) {
      const { key, moved } = dragging;
      dragging = null;
      if (moved) model.onMove?.(key, positions.get(key));
      else model.onSelect?.({ kind: "node", key });
      draw();
      return;
    }
    if (connecting) {
      const point = toCanvas(event);
      const target = nodeAt(point);
      const zone = svg.querySelector("[data-endzone]");
      const zx = Number(zone?.dataset.zoneX || 0);
      const zy = Number(zone?.dataset.zoneY || 0);
      const overEnd = point.x >= zx - 6 && point.x <= zx + 64
        && point.y >= zy - 6 && point.y <= zy + 40;
      const from = connecting.fromKey;
      connecting = null;
      if (overEnd) model.onConnect?.(from, null);
      else if (target) model.onConnect?.(from, target.key);
      else draw();
    }
  }

  svg.addEventListener("pointerup", finishPointer);
  svg.addEventListener("pointercancel", () => { dragging = null; connecting = null; draw(); });

  svg.addEventListener("keydown", (event) => {
    if ((event.key === "Backspace" || event.key === "Delete") && model.selection) {
      event.preventDefault();
      model.onDelete?.(model.selection);
    }
  });

  draw();
  return {
    element: wrap,
    refresh() {
      const next = positionsOf(nodes, edges);
      positions.clear();
      for (const [key, value] of next) positions.set(key, value);
      resize();
      draw();
    },
  };
}
