/* Unit tests for the Agent Workflow editor model (static/js/workflow_model.js).
 * Pure logic, no DOM: run with `node --test tests/js/` or via
 * tests/test_workflow_js.py.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const M = require("../../static/js/workflow_model.js");

const TOOLS = [
  { name: "get_health", category: "system", input_schema: {} },
  { name: "ai_generate", category: "ai", input_schema: {
      prompt: { type: "string", required: true },
      provider: { type: "string" } } },
  { name: "write_file", category: "files",
    input_schema: { path: { type: "string", required: true } } },
];
const TOOLMAP = M.toolMapFrom(TOOLS);

function definition(id, name, steps) {
  return { id, name, description: "", steps: steps || [], layout: {} };
}

/* --------------------------------------------------------------- drafting */

test("blankDraft is empty, new and id-less", () => {
  const d = M.blankDraft();
  assert.strictEqual(d.mode, "new");
  assert.strictEqual(d.id, null);
  assert.deepStrictEqual(d.steps, []);
  assert.deepStrictEqual(d.layout, {});
});

test("draftFromDefinition deep-copies: editing a draft cannot touch the source", () => {
  const def = definition(7, "A", [{ id: "s1", tool: "get_health", params: { x: 1 } }]);
  const d = M.draftFromDefinition(def);
  assert.strictEqual(d.mode, "edit");
  assert.strictEqual(d.id, 7);
  d.steps[0].params.x = 999;
  d.steps[0].tool = "write_file";
  d.steps.push({ id: "s2", tool: "get_health" });
  assert.strictEqual(def.steps[0].params.x, 1, "source definition must not change");
  assert.strictEqual(def.steps[0].tool, "get_health");
  assert.strictEqual(def.steps.length, 1);
});

test("two drafts from the same definition share nothing", () => {
  const def = definition(7, "A", [{ id: "s1", tool: "get_health", depends_on: [] }]);
  const a = M.draftFromDefinition(def);
  const b = M.draftFromDefinition(def);
  assert.notStrictEqual(a.uid, b.uid);
  a.steps[0].depends_on.push("ghost");
  assert.deepStrictEqual(b.steps[0].depends_on, []);
});

test("a new draft never inherits another workflow's steps or layout", () => {
  const first = M.draftFromDefinition(definition(1, "First",
    [{ id: "s1", tool: "get_health" }]));
  first.layout.s1 = { x: 5, y: 5 };
  const second = M.blankDraft();          // "New Workflow"
  assert.strictEqual(second.id, null);
  assert.deepStrictEqual(second.steps, []);
  assert.deepStrictEqual(second.layout, {});
  assert.notStrictEqual(first.steps, second.steps);
});

test("definitionFromDraft emits exactly what the engine accepts", () => {
  const d = M.blankDraft();
  d.name = "  My flow  ";
  M.addStep(d, "get_health");
  const body = M.definitionFromDraft(d);
  assert.strictEqual(body.name, "My flow");
  assert.deepStrictEqual(Object.keys(body).sort(),
                         ["description", "layout", "name", "steps"]);
  assert.deepStrictEqual(body.steps[0].depends_on, []);
  assert.strictEqual(body.steps[0].if, null);
});

test("definitionFromDraft defaults a blank name", () => {
  const d = M.blankDraft();
  assert.strictEqual(M.definitionFromDraft(d).name, "Untitled Workflow");
});

test("definitionFromDraft payload is a copy, not live draft state", () => {
  const d = M.blankDraft();
  d.name = "x";
  M.addStep(d, "get_health");
  const body = M.definitionFromDraft(d);
  d.steps[0].tool = "write_file";
  assert.strictEqual(body.steps[0].tool, "get_health");
});

/* ------------------------------------------------------------------ nodes */

test("step ids are unique and stable", () => {
  const d = M.blankDraft();
  const a = M.addStep(d, "get_health");
  const b = M.addStep(d, "get_health");
  assert.notStrictEqual(a.id, b.id);
  const fresh = M.blankDraft();
  assert.strictEqual(M.addStep(fresh, "get_health").id, "s1");
});

test("addStep can chain onto the previous node", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  const b = M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  assert.deepStrictEqual(b.depends_on, ["s1"]);
});

test("removing a node clears every reference to it", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  const b = M.addStep(d, "get_health", { dependsOn: ["s1"] });
  M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s2"] });
  assert.deepStrictEqual(b.depends_on, ["s1"]);
  assert.ok(M.findStep(d, "s2").if);
  d.layout[b.id] = { x: 1, y: 2 };
  d.layout["s1"] = { x: 0, y: 0 };
  M.removeStep(d, "s1");
  assert.strictEqual(M.findStep(d, "s1"), null);
  assert.deepStrictEqual(M.findStep(d, "s2").depends_on, []);
  assert.strictEqual(M.findStep(d, "s2").if, null);
  // the removed node's position goes; the surviving node keeps its own
  assert.deepStrictEqual(d.layout, { s2: { x: 1, y: 2 } });
});

test("cycles are refused (the engine refuses them too)", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "get_health", { dependsOn: ["s1"] });
  assert.strictEqual(M.wouldCycle(d.steps, ["s2"], "s1"), true);
  assert.strictEqual(M.wouldCycle(d.steps, ["s1"], "s2"), false,
                     "the existing edge is not a cycle");
  assert.strictEqual(M.setDependsOn(d, "s1", ["s2"]), false);
  assert.deepStrictEqual(M.findStep(d, "s1").depends_on, []);
  assert.strictEqual(M.hasCycle(d.steps), false);
});

test("topoOrder keeps dependencies first", () => {
  const steps = [
    { id: "b", tool: "x", depends_on: ["a"] },
    { id: "a", tool: "x", depends_on: [] },
  ];
  assert.deepStrictEqual(M.topoOrder(steps).map((s) => s.id), ["a", "b"]);
});

/* ------------------------------------------------------------- conditions */

test("a condition compiles to the engine's `if` on the gated step", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  const id = M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s2"] });
  assert.strictEqual(id, "cond:s1:ok");
  assert.deepStrictEqual(M.findStep(d, "s2").if, { step: "s1", op: "ok" });
  assert.deepStrictEqual(M.conditionNodes(d).map((c) => [c.id, c.gates]), [["cond:s1:ok", ["s2"]]]);
});

test("changing a condition's op rewrites exactly the gated steps", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  const id = M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s2"] });
  const newId = M.upsertCondition(d, id, { step: "s1", op: "not_ok", gates: ["s2"] });
  assert.strictEqual(newId, "cond:s1:not_ok");
  assert.deepStrictEqual(M.findStep(d, "s2").if, { step: "s1", op: "not_ok" });
  assert.deepStrictEqual(M.conditionNodes(d).map((c) => c.id), ["cond:s1:not_ok"]);
});

test("a condition cannot gate its own source step", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  assert.strictEqual(M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s1"] }), null);
});

test("clearing a condition drops the `if` from its gates", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  const id = M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s2"] });
  assert.strictEqual(M.clearCondition(d, id), true);
  assert.strictEqual(M.findStep(d, "s2").if, null);
  assert.deepStrictEqual(M.conditionNodes(d), []);
});

/* ---------------------------------------------------------------- canvas */

test("canvas nodes cover the trigger, every step and every condition", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s2"] });
  const nodes = M.canvasNodes(d, TOOLMAP);
  assert.deepStrictEqual(nodes.map((n) => n.id),
    ["__trigger__", "s1", "s2", "cond:s1:ok"]);
  assert.strictEqual(nodes.filter((n) => n.id === "s2")[0].kind, "ai");
  assert.strictEqual(nodes.filter((n) => n.id === "s1")[0].kind, "tool");
});

test("edges follow depends_on, the trigger and condition gates", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  M.addStep(d, "ai_generate", { dependsOn: ["s1"] });
  M.upsertCondition(d, null, { step: "s1", op: "ok", gates: ["s2"] });
  const kinds = M.canvasEdges(d, TOOLMAP).map((e) => `${e.from}->${e.to}:${e.kind}`);
  assert.ok(kinds.includes("__trigger__->s1:start"));
  assert.ok(kinds.includes("s1->s2:dep"));
  assert.ok(kinds.includes("s1->cond:s1:ok:cond"));
  assert.ok(kinds.includes("cond:s1:ok->s2:gate"));
});

test("nodes without a stored position get one; stored ones win", () => {
  const d = M.blankDraft();
  M.addStep(d, "get_health");
  const nodes = M.withPositions(M.canvasNodes(d, TOOLMAP), { s1: { x: 400, y: 90 } });
  const s1 = nodes.filter((n) => n.id === "s1")[0];
  assert.strictEqual(s1.x, 400);
  assert.strictEqual(s1.y, 90);
  assert.ok(nodes.filter((n) => n.id === "__trigger__")[0].x >= 0);
});

/* ------------------------------------------------------------ validation */

test("validateDraft mirrors the engine's rules", () => {
  const d = M.blankDraft();
  assert.strictEqual(M.validateDraft(d, TOOLMAP).ok, false, "needs a name");
  d.name = "ok";
  assert.strictEqual(M.validateDraft(d, TOOLMAP).ok, true, "empty steps are allowed");
  M.addStep(d, "not_a_real_tool");
  const bad = M.validateDraft(d, TOOLMAP);
  assert.strictEqual(bad.ok, false);
  assert.match(bad.errors.join(" "), /unknown tool/);
});

test("validateDraft catches dangling dependencies", () => {
  const d = M.blankDraft();
  d.name = "x";
  M.addStep(d, "get_health");
  M.findStep(d, "s1").depends_on = ["ghost"];
  assert.match(M.validateDraft(d, TOOLMAP).errors.join(" "), /unknown step 'ghost'/);
});

/* --------------------------------------------------------------- run view */

test("nodeStatus maps real run results onto canvas states", () => {
  const run = { id: 3, status: "failed", current_step: "s2",
                results: { s1: { ok: true }, s2: { ok: false, error: "boom" },
                           s3: { skipped: true, reason: "condition false" } } };
  assert.strictEqual(M.nodeStatus(run, "s1"), "success");
  assert.strictEqual(M.nodeStatus(run, "s2"), "failed");
  assert.strictEqual(M.nodeStatus(run, "s3"), "skipped");
  assert.strictEqual(M.nodeStatus(run, "s9"), "idle");
  assert.strictEqual(M.nodeStatus(null, "s1"), "idle");
});

test("a running step is marked running, not done", () => {
  const run = { id: 4, status: "running", current_step: "s2",
                results: { s1: { ok: true } } };
  assert.strictEqual(M.nodeStatus(run, "s2"), "running", "no result yet -> running");
  assert.strictEqual(M.nodeStatus(run, "s1"), "success", "a recorded result is final");
  // counts cover whatever the run recorded (an API run carries no step list)
  assert.deepStrictEqual(M.runCounts(run), { success: 1, failed: 0, skipped: 0,
                                             blocked: 0, pending: 0 });
});

test("runCounts also counts steps a run has not reached yet", () => {
  // a live-shaped run (with the workflow's step list) still has steps to go
  const run = { id: 5, status: "running", current_step: "s1",
                steps: [{ id: "s1" }, { id: "s2" }],
                results: { s1: { ok: true } } };
  assert.deepStrictEqual(M.runCounts(run), { success: 1, failed: 0, skipped: 0,
                                             blocked: 0, pending: 1 });
});

test("runSummary reports duration and failures from real fields", () => {
  const run = { id: 9, status: "failed", started_at: "2026-09-23 10:00:00",
                completed_at: "2026-09-23 10:00:05", error: "s2: boom",
                results: { s1: { ok: true }, s2: { ok: false } } };
  const s = M.runSummary(run);
  assert.strictEqual(s.duration, "5.0s");
  assert.strictEqual(s.failed, 1);
  assert.strictEqual(M.runHasErrors(run), true);
});

/* ------------------------------------------------------------------ misc */

test("only events for the displayed run are shown", () => {
  const ev = { id: 1, kind: "task.started", data: { run_id: 5, step: "s1" } };
  assert.strictEqual(M.isRunEvent(ev, 5), true);
  assert.strictEqual(M.isRunEvent(ev, 6), false);
  assert.strictEqual(M.isRunEvent({ id: 2, kind: "x", data: {} }, 5), false);
  const row = M.eventRow(ev);
  assert.strictEqual(row.step, "s1");
  assert.strictEqual(row.text, "step started");
});

test("tool schemas normalise from both registry shapes", () => {
  assert.deepStrictEqual(M.toolFields(TOOLS[1]).map((f) => [f.name, f.required]),
    [["prompt", true], ["provider", false]]);
  const jsonSchema = { input_schema: { type: "object",
    properties: { action: { type: "string" } }, required: ["action"] } };
  assert.deepStrictEqual(M.toolFields(jsonSchema).map((f) => [f.name, f.type, f.required]),
    [["action", "string", true]]);
  assert.deepStrictEqual(M.toolFields({ input_schema: {} }), []);
});

test("uniqueName mirrors the engine's deterministic naming", () => {
  assert.strictEqual(M.uniqueName([], "Untitled Workflow"), "Untitled Workflow");
  assert.strictEqual(M.uniqueName(["Untitled Workflow"], "Untitled Workflow"),
                     "Untitled Workflow 2");
  assert.strictEqual(
    M.uniqueName(["Untitled Workflow", "Untitled Workflow 2", "Untitled Workflow 3"], ""),
    "Untitled Workflow 4");
});

test("category labels fall back for an unknown category", () => {
  assert.strictEqual(M.categoryLabel("ai"), "AI / Agent");
  assert.strictEqual(M.categoryLabel("research"), "Web / API");
  assert.strictEqual(M.categoryLabel("quantum"), "Quantum");
});
