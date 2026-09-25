import { api, checkbox, confirmDelete, el, emptyState, field, mount, toast } from "../lib.js";
import { END, fansOut, interactiveCanvas, needsCondition, parseSchemaText, siblingsOf,
  staticDiagram } from "./graph.js";

export async function render(panel, arg) {
  const [workflows, roles, examples] = await Promise.all([
    api("/api/workflows"),
    api("/api/roles"),
    api("/api/workflows/examples"),
  ]);
  const editing = arg ? workflows.find((w) => String(w.id) === arg) : null;

  mount(panel,
    el("h2", {}, "Workflows"),
    el("p", { class: "sub" },
      "A workflow is a graph of roles: each node is one role doing a step, each edge a " +
      "transition. Drag nodes to arrange them, drag from a node's right-hand handle to " +
      "connect it to another node or to END, and click anything to edit it."),
    el("div", { class: "note" },
      "Each step runs as its own confined session. When a node has several outgoing " +
      "edges, the conditions decide which one is taken. An edge back to an earlier node " +
      "is a loop, bounded by that node's visit budget and the workflow's step budget."),

    el("h3", {}, `Configured (${workflows.length})`),
    workflows.length
      ? el("div", {}, workflows.map((w) => card(w, panel)))
      : emptyState("No workflows yet. Build one below, or start from an example."),

    el("h3", {}, editing ? `Edit ${editing.name}` : "Build a workflow"),
    roles.length
      ? editor(editing, roles, examples, panel)
      : emptyState("Add at least one role first — a node has to be a role.")
  );
}

function card(workflow, panel) {
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("strong", {}, workflow.name),
      el("span", { class: "tag" }, `${workflow.nodes.length} nodes`),
      el("span", { class: "tag" }, `${workflow.edges.length} edges`),
      el("div", { class: "card-actions" },
        el("button", {
          class: "btn ghost",
          onclick: () => { location.hash = `#/workflows/${workflow.id}`; },
        }, "Edit"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            if (!confirmDelete(`workflow ${workflow.name}`)) return;
            try {
              await api(`/api/workflows/${workflow.id}`, { method: "DELETE" });
              toast("Deleted");
              location.hash = "#/workflows";
              render(panel);
            } catch (error) {
              toast(error.message, true);
            }
          },
        }, "Delete"))),
    workflow.description ? el("p", {}, workflow.description) : null,
    staticDiagram(workflow.nodes, workflow.edges));
}

// ---------------------------------------------------------------- the editor

function editor(editing, roles, examples, panel) {
  const state = {
    id: editing?.id || null,
    nodes: (editing?.nodes || []).map((n) => ({ ...n })),
    edges: (editing?.edges || []).map((e) => ({ ...e })),
    selection: null,
    // Set when a drag just created a branch: the inspector opens with the condition
    // field focused, so an undecidable edge cannot be left behind by accident.
    focusCondition: false,
  };

  const nameInput = el("input", { name: "name", required: true, value: editing?.name || "" });
  const descInput = el("input", { name: "description", value: editing?.description || "" });
  const stepsInput = el("input", {
    type: "number", name: "max_steps", min: 1, max: 100, value: editing?.max_steps ?? 20,
  });
  const escalationSelect = el("select", { name: "escalation_key" });

  const canvasHost = el("div", {});
  const inspector = el("div", { class: "inspector" });
  const problems = el("div", { class: "note danger" }, "");
  problems.hidden = true;
  let canvas = null;

  function roleName(id) {
    return roles.find((r) => r.id === id)?.name || "?";
  }

  function payload() {
    return {
      name: nameInput.value.trim() || "untitled",
      description: descInput.value,
      max_steps: Number(stepsInput.value) || 20,
      escalation_key: escalationSelect.value || null,
      nodes: state.nodes.map((n) => ({
        key: n.key, role_id: n.role_id, instructions: n.instructions || "",
        is_start: !!n.is_start, max_visits: n.max_visits || 3,
        output_key: n.output_key || n.key,
        output_schema: n.output_schema ?? null,
        inputs: n.inputs || [],
        pos_x: n.pos_x ?? null, pos_y: n.pos_y ?? null,
      })),
      edges: state.edges.map((e) => ({
        from_key: e.from_key, to_key: e.to_key, label: e.label,
        condition: e.condition || "", expression: e.expression || "",
        is_default: !!e.is_default, resets: e.resets || [],
      })),
    };
  }

  let validateTimer = null;
  async function revalidate() {
    if (!state.nodes.length) { problems.hidden = true; return; }
    try {
      const result = await api("/api/workflows/validate", { method: "POST", body: payload() });
      problems.hidden = result.ok;
      if (!result.ok) {
        mount(problems, el("strong", {}, "This graph will not run:"),
          el("ul", {}, result.problems.map((p) => el("li", {}, p))));
      }
    } catch (error) {
      problems.hidden = false;
      problems.textContent = error.message;
    }
  }

  // Positions are saved on their own endpoint so dragging never trips validation
  // on a graph that is still being assembled.
  let layoutTimer = null;
  function persistLayout() {
    if (!state.id) return;
    clearTimeout(layoutTimer);
    layoutTimer = setTimeout(async () => {
      try {
        await api(`/api/workflows/${state.id}/layout`, {
          method: "PATCH",
          body: {
            positions: state.nodes
              .filter((n) => typeof n.pos_x === "number")
              .map((n) => ({ key: n.key, x: n.pos_x, y: n.pos_y })),
          },
        });
      } catch (error) {
        toast(`Could not save layout: ${error.message}`, true);
      }
    }, 500);
  }

  function refreshEscalation() {
    const current = escalationSelect.value || editing?.escalation_key || "";
    mount(escalationSelect,
      el("option", { value: "" }, "— none: a spent budget fails the run —"),
      ...state.nodes.map((n) =>
        el("option", { value: n.key, selected: n.key === current }, n.key)));
  }

  function redraw({ inspector: rebuildInspector = true } = {}) {
    refreshEscalation();
    // Role names are denormalised onto the nodes the canvas draws. Assigning onto the
    // same objects keeps state.nodes and the canvas looking at one set of nodes, so a
    // drag writes pos_x/pos_y straight into what gets saved.
    for (const node of state.nodes) node.role_name = roleName(node.role_id);
    canvas = interactiveCanvas({
      nodes: state.nodes,
      edges: state.edges,
      selection: state.selection,
      onSelect: (selection) => { state.selection = selection; redraw(); },
      onMove: () => persistLayout(),
      onConnect: (fromKey, toKey) => addEdge(fromKey, toKey),
      onDelete: (selection) => removeSelected(selection),
    });
    mount(canvasHost, canvas.element);
    // Editing a field must not recreate the inspector underneath the cursor: that is
    // what discarded half-typed JSON before.
    if (rebuildInspector) drawInspector();
    clearTimeout(validateTimer);
    validateTimer = setTimeout(revalidate, 250);
  }

  function uniqueLabel(fromKey, base) {
    let label = base;
    let n = 2;
    while (state.edges.some((e) => e.from_key === fromKey && e.label === label)) {
      label = `${base}_${n++}`;
    }
    return label;
  }

  function addEdge(fromKey, toKey) {
    const base = toKey === null ? "done" : `to_${toKey}`;
    const edge = {
      from_key: fromKey,
      to_key: toKey,
      label: uniqueLabel(fromKey, base),
      condition: "",
      is_default: false,
    };
    state.edges.push(edge);
    state.selection = { kind: "edge", index: state.edges.length - 1 };

    // A second way out of a node turns it into a branch, and a branch needs something
    // to decide by. Open straight into the condition field rather than leaving the user
    // to discover the problem in the validation box later.
    const branching = siblingsOf(state.edges, edge).length > 1;
    state.focusCondition = branching;
    redraw();
    if (branching) {
      toast(`${fromKey} now branches — describe when "${edge.label}" applies`);
    } else {
      toast("Edge created");
    }
  }

  function removeSelected(selection) {
    if (!selection) return;
    if (selection.kind === "node") {
      const key = selection.key;
      state.nodes = state.nodes.filter((n) => n.key !== key);
      state.edges = state.edges.filter((e) => e.from_key !== key && e.to_key !== key);
      if (state.nodes.length && !state.nodes.some((n) => n.is_start)) {
        state.nodes[0].is_start = true;
      }
    } else {
      state.edges.splice(selection.index, 1);
    }
    state.selection = null;
    redraw();
  }

  // --- the inspector ----------------------------------------------------

  function drawInspector() {
    const selection = state.selection;
    if (!selection) {
      mount(inspector,
        el("p", { class: "sub" },
          state.nodes.length
            ? "Click a node or an edge label to edit it. Drag a node to move it; drag its " +
              "right-hand handle onto another node — or onto END — to connect."
            : "Add your first node below."));
      return;
    }
    if (selection.kind === "node") {
      const node = state.nodes.find((n) => n.key === selection.key);
      if (!node) { state.selection = null; return drawInspector(); }
      mount(inspector, nodeInspector(node));
      return;
    }
    const edge = state.edges[selection.index];
    if (!edge) { state.selection = null; return drawInspector(); }
    const wantsFocus = state.focusCondition;
    state.focusCondition = false;
    mount(inspector, edgeInspector(edge, selection.index));
    if (wantsFocus) {
      const field = inspector.querySelector("input.needs-value")
        || inspector.querySelectorAll("input")[2];
      requestAnimationFrame(() => field?.focus());
    }
  }

  function nodeInspector(node) {
    const keyInput = el("input", { value: node.key });
    const roleSelect = el("select", {},
      roles.map((r) => el("option", { value: r.id, selected: r.id === node.role_id }, r.name)));
    const instructions = el("textarea", { value: node.instructions || "",
      placeholder: "What this role does at this step" });
    const visits = el("input", { type: "number", min: 1, max: 20, value: node.max_visits || 3 });
    const outputKey = el("input", {
      value: node.output_key || node.key,
      placeholder: node.key,
    });
    // The raw text lives on the node so a partially-typed schema survives anything
    // that repaints. Only valid JSON is promoted to node.output_schema.
    if (node.schema_text === undefined) {
      node.schema_text = node.output_schema
        ? JSON.stringify(node.output_schema, null, 2)
        : "";
    }
    const schemaText = el("textarea", {
      rows: 10, spellcheck: "false",
      value: node.schema_text,
      placeholder: '{\n  "type": "object",\n  "properties": {\n'
        + '    "approved": { "type": "boolean" },\n'
        + '    "issues": { "type": "array", "items": { "type": "string" } }\n'
        + '  },\n  "required": ["approved"]\n}',
    });
    const schemaState = el("div", { class: "path-state" }, "");
    const summaryLabel = el("span", {}, "Output shape");

    // Inputs: which earlier artifacts (or one of their fields) this step is shown.
    const inputsList = el("div", {});
    const sources = state.nodes
      .filter((n) => n !== node)
      .map((n) => ({ name: n.output_key || n.key, node: n }));
    const newFrom = el("select", {}, sources.map((srcObj) =>
      el("option", { value: srcObj.name }, srcObj.name)));
    const newPath = el("input", { placeholder: "issues (optional)" });
    const newAs = el("input", { placeholder: "label (optional)" });

    function drawInputs() {
      const inputs = node.inputs || [];
      mount(inputsList, ...(inputs.length
        ? inputs.map((input, index) =>
            el("div", { class: "card-head" },
              el("span", { class: "mono" },
                `${input.from}${input.path ? `.${input.path}` : ""}`
                + (input.as ? ` → ${input.as}` : "")),
              el("div", { class: "card-actions" },
                el("button", {
                  class: "btn danger", type: "button",
                  onclick: () => {
                    node.inputs.splice(index, 1);
                    drawInputs();
                    redraw({ inspector: false });
                  },
                }, "Remove"))))
        : [el("p", { class: "sub" },
            "Nothing selected: this step sees every artifact produced so far.")]));
    }

    const addInput = el("button", {
      class: "btn ghost", type: "button",
      onclick: () => {
        if (!sources.length) { toast("There are no other steps to take input from", true); return; }
        node.inputs = node.inputs || [];
        node.inputs.push({
          from: newFrom.value,
          path: newPath.value.trim(),
          as: newAs.value.trim(),
        });
        newPath.value = ""; newAs.value = "";
        drawInputs();
        redraw({ inspector: false });
      },
    }, "Add input");
    drawInputs();
    const schemaTemplate = el("button", {
      class: "btn ghost", type: "button",
      onclick: () => {
        schemaText.value = JSON.stringify({
          type: "object",
          properties: {
            approved: { type: "boolean" },
            issues: { type: "array", items: { type: "string" } },
            summary: { type: "string" },
          },
          required: ["approved", "issues"],
          additionalProperties: false,
        }, null, 2);
        applySchema();
        schemaText.focus();
      },
    }, "Insert a starting schema");

    function applySchema() {
      // Always keep the text, whatever state it is in.
      node.schema_text = schemaText.value;
      const result = parseSchemaText(schemaText.value);
      if (result.schema !== undefined) node.output_schema = result.schema;
      schemaState.className = `path-state ${result.ok ? (result.schema ? "ok" : "") : "bad"}`;
      schemaState.textContent = result.message;
      summaryLabel.textContent = node.output_schema
        ? `Output shape: JSON — ${result.fields.join(", ") || "no fields yet"}`
        : "Output shape: free text — click to define a JSON result";
      return result.ok;
    }
    // Parse as it is typed, so the feedback is immediate and nothing is rebuilt.
    let schemaTimer = null;
    schemaText.addEventListener("input", () => {
      clearTimeout(schemaTimer);
      schemaTimer = setTimeout(() => {
        applySchema();
        clearTimeout(validateTimer);
        validateTimer = setTimeout(revalidate, 400);
      }, 250);
    });
    applySchema();
    const start = checkbox("Start node", "is_start", node.is_start,
      "Where a run begins. Only one node can be the start.");

    function apply() {
      let keyChanged = false;
      const nextKey = keyInput.value.trim().toLowerCase();
      if (nextKey && nextKey !== node.key) {
        if (state.nodes.some((n) => n !== node && n.key === nextKey)) {
          toast(`There is already a node called ${nextKey}`, true);
          keyInput.value = node.key;
        } else {
          // Edges refer to nodes by key, so renaming has to carry them along.
          const old = node.key;
          for (const edge of state.edges) {
            if (edge.from_key === old) edge.from_key = nextKey;
            if (edge.to_key === old) edge.to_key = nextKey;
          }
          node.key = nextKey;
          state.selection = { kind: "node", key: nextKey };
          keyChanged = true;
        }
      }
      node.role_id = Number(roleSelect.value);
      node.instructions = instructions.value;
      node.max_visits = Number(visits.value) || 1;
      const artifact = outputKey.value.trim().toLowerCase();
      node.output_key = artifact || node.key;
      applySchema();
      if (start.input.checked) {
        for (const other of state.nodes) other.is_start = other === node;
      } else if (node.is_start) {
        // Refusing to leave a graph with no entry point.
        start.input.checked = true;
        toast("A workflow needs a start node — make another node the start instead", true);
      }
      redraw({ inspector: keyChanged });
    }

    for (const control of [keyInput, roleSelect, instructions, visits, outputKey,
                           start.input]) {
      control.addEventListener("change", apply);
    }

    return el("div", {},
      el("div", { class: "card-head" },
        el("strong", {}, `Node: ${node.key}`),
        el("div", { class: "card-actions" },
          el("button", {
            class: "btn danger", type: "button",
            onclick: () => removeSelected({ kind: "node", key: node.key }),
          }, "Delete node"))),
      el("div", { class: "row" },
        field("Key", keyInput, "Short identifier. Renaming updates its edges."),
        field("Role", roleSelect),
        field("Visit budget", visits, "Times this step may run in one execution.")),
      field("Output name", outputKey,
        "What later steps see this step's result as — e.g. arch_doc, design_doc, code."),
      field("Step instructions", instructions,
        "Added to the role's own system prompt for this step only."),
      el("details", { class: "editor", open: (node.inputs || []).length > 0 },
        el("summary", {},
          (node.inputs || []).length
            ? `Inputs: ${node.inputs.map((i) => i.from + (i.path ? "." + i.path : "")).join(", ")}`
            : "Inputs — everything produced so far"),
        el("p", { class: "sub" },
          "Pick what this step is shown. A path pulls one field out of a JSON result, so "
          + "a role reworking sees only the notes addressed to it."),
        inputsList,
        el("div", { class: "row" },
          field("From", newFrom),
          field("Field path", newPath, "Optional. e.g. issues, or counts.architect"),
          field("Call it", newAs),
          el("div", {}, addInput))),
      el("details", { class: "editor", open: !!node.output_schema },
        el("summary", {}, summaryLabel),
        el("p", { class: "sub" },
          "Leave empty for prose. Give a JSON Schema and this step must return data "
          + "matching it; later steps and the router then read its fields by name."),
        field("JSON Schema", schemaText),
        schemaState,
        el("div", {}, schemaTemplate)),
      start.label);
  }

  function edgeInspector(edge, index) {
    const siblings = siblingsOf(state.edges, edge);
    const labelInput = el("input", { value: edge.label });
    const targets = el("select", {},
      el("option", { value: END, selected: !edge.to_key }, "END (finish)"),
      state.nodes.map((n) =>
        el("option", { value: n.key, selected: edge.to_key === n.key }, n.key)));
    const missing = needsCondition(state.edges, edge);
    const condition = el("input", {
      value: edge.condition || "",
      class: missing ? "needs-value" : null,
      placeholder: siblings.length > 1
        ? "e.g. the tests failed, or the reviewer found something to change"
        : "when this transition applies, in plain language",
    });
    const isDefault = checkbox("Default edge", "is_default", edge.is_default,
      "Taken when no condition matches. At most one per source node.");
    const expression = el("input", {
      value: edge.expression || "",
      placeholder: "issues contains architecture",
      spellcheck: "false",
    });
    const expressionState = el("div", { class: "path-state" }, "");
    const resetsSelect = el("select", {
      multiple: true, size: Math.min(4, Math.max(2, state.nodes.length)),
    }, state.nodes.map((n) =>
      el("option", { value: n.key, selected: (edge.resets || []).includes(n.key) }, n.key)));
    const parallel = fansOut(state.edges, edge.from_key);
    const loops = edge.to_key && state.nodes.some((n) => n.key === edge.to_key);
    // Updated in place rather than by rebuilding the inspector, which would move focus
    // out of whatever field is being edited.
    const heading = el("strong", {}, `Edge: ${edge.from_key} → ${edge.to_key || END}`);
    const testSummary = el("span", {}, "");

    function apply() {
      const nextLabel = labelInput.value.trim().toLowerCase().replace(/\s+/g, "_");
      if (nextLabel && nextLabel !== edge.label) {
        if (siblings.some((e) => e !== edge && e.label === nextLabel)) {
          toast(`${edge.from_key} already has an edge called ${nextLabel}`, true);
          labelInput.value = edge.label;
        } else {
          edge.label = nextLabel;
        }
      }
      edge.to_key = targets.value === END ? null : targets.value;
      edge.condition = condition.value;
      edge.expression = expression.value.trim();
      edge.resets = [...resetsSelect.selectedOptions].map((o) => o.value);
      heading.textContent = `Edge: ${edge.from_key} → ${edge.to_key || END}`;
      describeExpression();
      if (isDefault.input.checked) {
        for (const other of state.edges) {
          if (other.from_key === edge.from_key) other.is_default = other === edge;
        }
      } else {
        edge.is_default = false;
      }
      redraw({ inspector: false });
    }

    for (const control of [labelInput, targets, condition, expression, isDefault.input,
                           resetsSelect]) {
      control.addEventListener("change", apply);
    }

    // Which JSON fields are on offer here: the source step's own schema, plus every
    // artifact by name. Without this you are guessing at field names.
    const source = state.nodes.find((n) => n.key === edge.from_key);
    const ownFields = Object.keys(source?.output_schema?.properties || {});
    const artifacts = state.nodes
      .filter((n) => n.output_schema)
      .map((n) => n.output_key || n.key);

    function describeExpression() {
      const text = expression.value.trim();
      testSummary.textContent = text
        ? `Test: ${text}`
        : "Test the result instead (no model call)";
      if (!text) {
        expressionState.className = "path-state";
        expressionState.textContent = ownFields.length
          ? `Available here: ${ownFields.join(", ")}`
          : source?.output_schema
            ? "The source step has no fields yet."
            : `${edge.from_key} returns prose, so there are no fields to test — `
              + "give it an output shape first, or use a worded condition.";
        return;
      }
      expressionState.className = "path-state ok";
      expressionState.textContent = ownFields.length
        ? `Reads: ${ownFields.join(", ")}${artifacts.length ? ` · also ${artifacts.join(", ")}` : ""}`
        : "No declared fields on the source step — this will not match anything.";
    }
    let expressionTimer = null;
    expression.addEventListener("input", () => {
      describeExpression();
      clearTimeout(expressionTimer);
      expressionTimer = setTimeout(apply, 350);
    });
    describeExpression();

    return el("div", {},
      el("div", { class: "card-head" },
        heading,
        parallel ? el("span", { class: "tag ok" }, "parallel arm")
          : siblings.length > 1 ? el("span", { class: "tag warn" }, "branch") : null,
        loops && state.nodes.findIndex((n) => n.key === edge.to_key)
          <= state.nodes.findIndex((n) => n.key === edge.from_key)
          ? el("span", { class: "tag warn" }, "loop")
          : null,
        el("div", { class: "card-actions" },
          el("button", {
            class: "btn danger", type: "button",
            onclick: () => removeSelected({ kind: "edge", index }),
          }, "Delete edge"))),
      el("div", { class: "row" },
        field("Label", labelInput, "How the router refers to this branch."),
        field("Goes to", targets)),
      field("Condition", condition,
        parallel
          ? `Leave blank to keep ${edge.from_key} fanning out: every arm runs in parallel. `
            + "Adding a condition here turns it into a branch."
          : siblings.length > 1
            ? "Required: this node branches, so every edge needs a condition — or must be "
              + "the default, which is taken when no condition matches."
            : "Optional while this is the only way out of " + edge.from_key + "."),
      el("details", { class: "editor", open: !!edge.expression },
        el("summary", {}, testSummary),
        el("p", { class: "sub" },
          "A deterministic test over the source step's JSON result — e.g. "
          + "\u201cissues contains architecture\u201d, \u201capproved is false\u201d, "
          + "\u201cscore > 5\u201d. When any expression on this node matches, the model is "
          + "not consulted at all."),
        field("Expression", expression),
        expressionState),
      isDefault.label,
      field("Reset visit budgets", resetsSelect,
        "Nodes whose visit counts start again when this edge is taken — for sending work "
        + "back upstream with a fresh loop budget."));
  }

  // --- adding nodes -----------------------------------------------------

  const newKey = el("input", { placeholder: "e.g. build" });
  const newRole = el("select", {}, roles.map((r) => el("option", { value: r.id }, r.name)));
  const addNodeButton = el("button", {
    class: "btn ghost", type: "button",
    onclick: () => {
      const key = newKey.value.trim().toLowerCase();
      if (!key) { toast("A node needs a key", true); return; }
      if (state.nodes.some((n) => n.key === key)) {
        toast(`There is already a node called ${key}`, true);
        return;
      }
      // Place it to the right of everything so it lands somewhere visible.
      const maxX = state.nodes.reduce((acc, n) => Math.max(acc, n.pos_x ?? 0), 0);
      state.nodes.push({
        key, role_id: Number(newRole.value), instructions: "",
        is_start: state.nodes.length === 0, max_visits: 3,
        pos_x: state.nodes.length ? maxX + 230 : 30,
        pos_y: 30,
      });
      newKey.value = "";
      state.selection = { kind: "node", key };
      redraw();
    },
  }, "Add node");

  // --- examples ---------------------------------------------------------

  const exampleButtons = examples.map((example) =>
    el("button", {
      class: "btn ghost", type: "button",
      onclick: () => {
        const missing = [];
        const keyed = {};
        for (const node of example.nodes) {
          const role = roles.find((r) => r.name === node.role);
          if (!role) missing.push(node.role);
          else keyed[node.key] = role.id;
        }
        if (missing.length) {
          toast(`Create these roles first: ${[...new Set(missing)].join(", ")}`, true);
          return;
        }
        nameInput.value = nameInput.value || example.name;
        descInput.value = descInput.value || example.description;
        if (example.max_steps) stepsInput.value = example.max_steps;
        state.nodes = example.nodes.map((n, index) => ({
          key: n.key, role_id: keyed[n.key], instructions: n.instructions || "",
          is_start: !!n.is_start, max_visits: n.max_visits || 3,
          output_key: n.output_key || n.key,
          output_schema: n.output_schema || null,
          inputs: n.inputs || [],
          pos_x: 30 + index * 230, pos_y: 30,
        }));
        state.edges = example.edges.map((e) => ({
          ...e, condition: e.condition || "", expression: e.expression || "",
          resets: e.resets || [],
        }));
        state.selection = null;
        redraw();
        // The escalation picker is rebuilt from the nodes, so set it afterwards.
        if (example.escalation_key) {
          escalationSelect.value = example.escalation_key;
        }
      },
    }, example.name));

  const form = el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        const body = payload();
        const saved = state.id
          ? await api(`/api/workflows/${state.id}`, { method: "PUT", body })
          : await api("/api/workflows", { method: "POST", body });
        toast("Saved");
        state.id = saved.id;
        location.hash = "#/workflows";
        render(panel);
      } catch (error) {
        toast(error.message, true);
      }
    },
  },
    el("div", { class: "row" },
      field("Name", nameInput),
      field("Step budget", stepsInput,
        "Hard ceiling on steps for one run, whatever the loops say.")),
    el("div", { class: "row" },
      field("Description", descInput),
      field("On exhausted budget", escalationSelect,
        "Where to hand over when a visit or step budget runs out, instead of failing.")),
    examples.length
      ? el("p", { class: "sub" }, "Start from: ",
          ...exampleButtons.flatMap((b, i) => (i ? [" ", b] : [b])))
      : null,

    el("h3", {}, "Graph"),
    problems,
    canvasHost,
    inspector,

    el("h3", {}, "Add a node"),
    el("div", { class: "row" },
      field("Key", newKey, "Short identifier used by edges."),
      field("Role", newRole),
      el("div", {}, addNodeButton)),

    el("div", {}, el("button", { class: "btn", type: "submit" },
      editing ? "Save workflow" : "Create workflow"))
  );

  redraw();
  return form;
}
