// Frontend graph logic, tested without a browser.
//
//   node tests/graph.test.mjs
//
// Covers the pure parts of the workflow canvas: where nodes land when the user has not
// placed them, how a condition is wrapped for drawing, and which edges the canvas must
// flag as undecidable. The pointer interactions need a real browser and are not here.

import { conditionLines, fansOut, needsCondition, parseSchemaText, positionsOf,
  siblingsOf, BOX_W } from "../app/static/views/graph.js";

let failures = 0;
const results = [];

function check(label, condition, detail = "") {
  results.push({ label, ok: !!condition, detail });
  if (!condition) failures++;
}

function group(name) {
  results.push({ group: name });
}

// ------------------------------------------------------------------- layout

group("positions");

let pos = positionsOf(
  [{ key: "a", is_start: true, pos_x: 111, pos_y: 222 }, { key: "b" }],
  [{ from_key: "a", to_key: "b", label: "x" }],
);
check("a stored position is used verbatim",
  pos.get("a").x === 111 && pos.get("a").y === 222);
check("an unplaced node still gets a position", typeof pos.get("b").x === "number");

pos = positionsOf(
  [{ key: "a", is_start: true }, { key: "b" }, { key: "c" }],
  [{ from_key: "a", to_key: "b", label: "x" }, { from_key: "b", to_key: "c", label: "y" }],
);
check("a chain is laid out left to right",
  pos.get("a").x < pos.get("b").x && pos.get("b").x < pos.get("c").x,
  `${pos.get("a").x} < ${pos.get("b").x} < ${pos.get("c").x}`);
check("chained nodes do not overlap", pos.get("b").x - pos.get("a").x >= BOX_W);

pos = positionsOf(
  [{ key: "a", is_start: true }, { key: "b" }, { key: "c" }],
  [{ from_key: "a", to_key: "b", label: "x" }, { from_key: "a", to_key: "c", label: "y" }],
);
check("branch siblings share a column", pos.get("b").x === pos.get("c").x);
check("branch siblings sit on different rows", pos.get("b").y !== pos.get("c").y);

pos = positionsOf([{ key: "a", is_start: true }, { key: "orphan" }], []);
check("an unreachable node is still placed somewhere of its own",
  pos.get("orphan").x !== pos.get("a").x || pos.get("orphan").y !== pos.get("a").y);

pos = positionsOf(
  [{ key: "build", is_start: true }, { key: "test" }],
  [{ from_key: "build", to_key: "test", label: "t" },
   { from_key: "test", to_key: "build", label: "rework" }],
);
check("a loop does not relayout its target", pos.get("build").x < pos.get("test").x);

pos = positionsOf([{ key: "a", is_start: true }],
  [{ from_key: "a", to_key: null, label: "done" }]);
check("an END edge does not break layout", pos.size === 1);

// -------------------------------------------------------- condition wrapping

group("condition text");

check("no condition yields no lines", conditionLines("").length === 0);
check("whitespace-only yields no lines", conditionLines("   \n ").length === 0);
check("a short condition stays on one line",
  conditionLines("the tests fail").join("|") === "the tests fail");
check("newlines collapse to spaces", conditionLines("tests\n  fail")[0] === "tests fail");

const wrapped = conditionLines(
  "any test fails, or the implementation is incomplete and needs more work");
check("a long condition wraps to at most two lines", wrapped.length === 2,
  JSON.stringify(wrapped));
check("wrapped lines stay within the drawn width", wrapped.every((l) => l.length <= 35));
check("truncation is signalled with an ellipsis", wrapped.at(-1).endsWith("…"),
  wrapped.at(-1));
check("a single over-long word is cut rather than overflowing",
  conditionLines("supercalifragilisticexpialidociousandthensome")[0].endsWith("…"));

// ------------------------------------------------------- undecidable branches

group("needs a condition");

const single = [{ from_key: "a", label: "next", condition: "" }];
check("a lone outgoing edge needs no condition", needsCondition(single, single[0]) === false);

// Several bare edges out of one node are a fan-out, not a broken branch: every arm
// runs in parallel, which is what manager_dispatch does in the LangGraph example.
const fan = [
  { from_key: "a", label: "x", condition: "" },
  { from_key: "a", label: "y", condition: "" },
];
check("a fan-out is recognised", fansOut(fan, "a") === true);
check("fan-out arms are not flagged as missing a condition",
  needsCondition(fan, fan[0]) === false && needsCondition(fan, fan[1]) === false);

// Mixing described and undescribed edges is the ambiguous case.
const mixed = [
  { from_key: "a", label: "x", condition: "" },
  { from_key: "a", label: "y", condition: "something is wrong" },
];
check("a mixed node is not a fan-out", fansOut(mixed, "a") === false);
check("the undescribed edge of a mixed node is flagged",
  needsCondition(mixed, mixed[0]) === true);
check("its described sibling is not", needsCondition(mixed, mixed[1]) === false);

check("a lone bare edge is never a fan-out", fansOut(single, "a") === false);

const defaulted = [
  { from_key: "a", label: "x", condition: "it broke" },
  { from_key: "a", label: "y", condition: "", is_default: true },
];
check("the default edge needs no condition — it is the fallback",
  needsCondition(defaulted, defaulted[1]) === false);

const unrelated = [
  { from_key: "a", label: "x", condition: "" },
  { from_key: "b", label: "y", condition: "" },
];
check("edges from different nodes are not siblings",
  needsCondition(unrelated, unrelated[0]) === false
  && siblingsOf(unrelated, unrelated[0]).length === 1);

const blank = [
  { from_key: "a", label: "x", condition: "   " },
  { from_key: "a", label: "y", condition: "real" },
];
check("whitespace does not count as a condition", needsCondition(blank, blank[0]) === true);
check("whitespace does not make a fan-out either", fansOut(blank, "a") === false);

// --------------------------------------------------------- output shape editing

group("output shape");

let shape = parseSchemaText("");
check("empty text means prose", shape.ok && shape.schema === null, shape.message);
check("empty text says so", shape.message.includes("prose"));

shape = parseSchemaText("   \n  ");
check("whitespace also means prose", shape.schema === null);

// Half-typed JSON must not wipe the last good schema: `schema` is left undefined so
// the caller keeps what it had, while the text the user sees is untouched.
shape = parseSchemaText('{"type": "object", "properti');
check("half-typed JSON is not ok", shape.ok === false);
check("half-typed JSON leaves the schema alone", shape.schema === undefined);
check("half-typed JSON explains itself", shape.message.startsWith("✗"), shape.message);

shape = parseSchemaText('{"type":"object","properties":{"approved":{"type":"boolean"}}}');
check("valid JSON is parsed", shape.ok && shape.schema.type === "object");
check("its fields are listed", shape.fields.join(",") === "approved", shape.message);
check("the message names the fields", shape.message.includes("approved"));

shape = parseSchemaText(
  '{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"number"}}}');
check("several fields are counted", shape.message.includes("2 fields"), shape.message);

shape = parseSchemaText('{"type":"object"}');
check("valid JSON with no properties is still ok to keep typing", shape.ok === true);
check("but it says there are no properties yet",
  shape.message.includes("no properties"), shape.message);

shape = parseSchemaText('{"type":"object","properties":{}}');
check("an empty properties object reports no fields", shape.fields.length === 0);

// ------------------------------------------------- expression edges on canvas

group("expression edges");

const exprBranch = [
  { from_key: "review", label: "arch", condition: "", expression: "issues contains architecture" },
  { from_key: "review", label: "ok", condition: "", expression: "approved is true" },
];
check("an expression describes the edge, so nothing is flagged",
  needsCondition(exprBranch, exprBranch[0]) === false);
check("two described edges are not a fan-out", fansOut(exprBranch, "review") === false);

const exprPlusWorded = [
  { from_key: "a", label: "x", condition: "", expression: "score > 5" },
  { from_key: "a", label: "y", condition: "it looks wrong" },
];
check("an expression and a worded condition can coexist on a node",
  needsCondition(exprPlusWorded, exprPlusWorded[0]) === false
  && needsCondition(exprPlusWorded, exprPlusWorded[1]) === false);

const exprPlusBare = [
  { from_key: "a", label: "x", condition: "", expression: "score > 5" },
  { from_key: "a", label: "y", condition: "" },
];
check("a bare edge beside an expression is still flagged",
  needsCondition(exprPlusBare, exprPlusBare[1]) === true);
check("and that node is not a fan-out", fansOut(exprPlusBare, "a") === false);

// ----------------------------------------------------------------- reporting

for (const row of results) {
  if (row.group) {
    console.log(`\n${row.group}`);
    continue;
  }
  console.log(`  ${row.ok ? "ok  " : "FAIL"} ${row.label}${row.detail ? ` — ${row.detail}` : ""}`);
}
const total = results.filter((r) => !r.group).length;
console.log(`\n${total - failures}/${total} passed`);
process.exit(failures ? 1 : 0);
