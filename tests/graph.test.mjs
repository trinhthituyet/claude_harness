// Frontend graph logic, tested without a browser.
//
//   node tests/graph.test.mjs
//
// Covers the pure parts of the workflow canvas: where nodes land when the user has not
// placed them, how a condition is wrapped for drawing, and which edges the canvas must
// flag as undecidable. The pointer interactions need a real browser and are not here.

import { conditionLines, fansOut, needsCondition, positionsOf, siblingsOf, BOX_W }
  from "../app/static/views/graph.js";

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
