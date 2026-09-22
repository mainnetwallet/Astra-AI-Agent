/* Unit tests for the Agent Workflow presentation model
 * (static/js/workflow_model.js). Pure logic, no DOM: run with
 * `node --test tests/js/` or via tests/test_workflow_model_js.py.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const W = require("../../static/js/workflow_model.js");

function ev(id, kind, data, agent, created) {
  return { id: id, kind: kind, agent: agent || "",
           data: data || {}, created_at: created || "2026-09-23 10:00:0" + (id % 10) };
}

/* ------------------------------------------------------------ definitions */

test("normalizeSteps mirrors engine.run()'s auto ids (step1, step2, …)", () => {
  const s = W.normalizeSteps([{ tool: "get_health" }, { id: "custom", tool: "remember" }]);
  assert.strictEqual(s[0].id, "step1");
  assert.strictEqual(s[1].id, "custom");
  assert.deepStrictEqual(s[0].depends_on, []);
  assert.deepStrictEqual(s[0].params, {});
});

test("normalizeSteps drops non-string depends_on / non-object params", () => {
  const s = W.normalizeSteps([{ id: "a", tool: "t", depends_on: [1, "b", null],
                                params: "nope" }]);
  assert.deepStrictEqual(s[0].depends_on, ["b"]);
  assert.deepStrictEqual(s[0].params, {});
});

test("layerSteps is a longest-path layering over depends_on (engine._topo order)",
  () => {
    const steps = [
      { id: "a", tool: "t" },
      { id: "b", tool: "t", depends_on: ["a"] },
      { id: "c", tool: "t", depends_on: ["b"] },
      { id: "d", tool: "t", depends_on: ["a"] },
    ];
    const lay = W.layerSteps(steps);
    assert.deepStrictEqual(lay.layers, [["a"], ["b", "d"], ["c"]]);
    assert.strictEqual(lay.depth.c, 2);
  });

test("layerSteps survives a dependency cycle instead of hanging", () => {
  const lay = W.layerSteps([{ id: "a", tool: "t", depends_on: ["b"] },
                            { id: "b", tool: "t", depends_on: ["a"] }]);
  assert.strictEqual(lay.order.length, 2);
  // both steps are still placed (a cycle guard, not a crash); the engine's own
  // _topo() likewise just leaves un-orderable steps out of the run order.
  const placed = lay.layers.reduce((n, l) => n + l.length, 0);
  assert.strictEqual(placed, 2);
});

test("extractRefs finds {{step.param}} like engine._resolve", () => {
  assert.deepStrictEqual(W.extractRefs({ path: "{{w.output.path}}", x: 1 }),
                         ["w.output.path"]);
  assert.deepStrictEqual(W.extractRefs({ a: "{{ b.k }} + {{b.j}}" }), ["b.k", "b.j"]);
  assert.deepStrictEqual(W.extractRefs({ a: "plain" }), []);
});

test("graphEdges separates depends_on edges, data edges and run params", () => {
  const g = W.graphEdges([
    { id: "w", tool: "write_file", params: { path: "n.txt" } },
    { id: "r", tool: "read_file", depends_on: ["w"],
      params: { path: "{{w.output.path}}", n: "{{limit}}" } },
  ]);
  const kinds = g.edges.map((e) => e.kind).sort();
  assert.deepStrictEqual(kinds, ["data", "dep"]);
  assert.deepStrictEqual(g.run_params, ["limit"]);
});

test("buildGraph reports dependents, condition and depth per node", () => {
  const g = W.buildGraph([
    { id: "a", tool: "get_health" },
    { id: "b", tool: "remember", depends_on: ["a"], if: { step: "a", op: "ok" } },
  ]);
  const a = g.nodes.find((n) => n.id === "a");
  const b = g.nodes.find((n) => n.id === "b");
  assert.deepStrictEqual(a.dependents, ["b"]);
  assert.deepStrictEqual(b.condition, { step: "a", op: "ok" });
  assert.strictEqual(b.depth, 1);
});

/* ------------------------------------------------------- run step results */

test("stepStateFromResult maps the real engine result shapes", () => {
  assert.strictEqual(W.stepStateFromResult({ ok: true, output: {} }), "completed");
  assert.strictEqual(W.stepStateFromResult({ ok: false, error: "boom" }), "failed");
  assert.strictEqual(W.stepStateFromResult({ skipped: true, reason: "condition false" }), "skipped");
  assert.strictEqual(W.stepStateFromResult({ blocked: true, reason: "needs confirmation" }), "blocked");
  assert.strictEqual(W.stepStateFromResult(undefined), "pending");
});

test("runStates paints history and marks a running run's current step", () => {
  const steps = [{ id: "s1", tool: "t" }, { id: "s2", tool: "t" }];
  const hist = { status: "completed", current_step: "",
                 results: { s1: { ok: true, output: 1 }, s2: { ok: false, error: "x" } } };
  assert.deepStrictEqual(W.runStates(hist, steps), { s1: "completed", s2: "failed" });
  const running = { status: "running", current_step: "s2", results: { s1: { ok: true } } };
  assert.deepStrictEqual(W.runStates(running, steps), { s1: "completed", s2: "running" });
  assert.deepStrictEqual(W.runStates({ status: "paused", current_step: "s1", results: {} }, steps),
                         { s1: "pending", s2: "pending" });
});

/* ------------------------------------------------ live workflow reduction */

test("reduceRun folds workflow.started + task.* into a live run snapshot", () => {
  let st = W.emptyRun();
  st = W.reduceRun(st, ev(1, "workflow.started", { op: "wf:7", workflow: "check", run_id: 7 }));
  assert.strictEqual(st.run_id, "7");
  assert.strictEqual(st.status, "running");
  st = W.reduceRun(st, ev(2, "task.started", { op: "wf:7:s1", run_id: 7, step: "s1" }));
  assert.strictEqual(st.steps.s1.state, "running");
  assert.strictEqual(st.current_step, "s1");
  st = W.reduceRun(st, ev(3, "task.completed", { op: "wf:7:s1", run_id: 7, step: "s1",
                                                 terminal: true, duration_ms: 42 }));
  assert.strictEqual(st.steps.s1.state, "completed");
  assert.strictEqual(st.steps.s1.duration_ms, 42);
  st = W.reduceRun(st, ev(4, "workflow.completed", { op: "wf:7", run_id: 7, terminal: true }));
  assert.strictEqual(st.status, "completed");
});

test("reduceRun records a failed step's error and fails the run", () => {
  let st = W.reduceRun(W.emptyRun(),
    ev(1, "task.started", { op: "wf:3:bad", run_id: 3, step: "bad" }));
  st = W.reduceRun(st, ev(2, "task.failed",
    { op: "wf:3:bad", run_id: 3, step: "bad", terminal: true, error: "KeyError: unknown tool" }));
  assert.strictEqual(st.steps.bad.state, "failed");
  assert.match(st.steps.bad.error, /unknown tool/);
});

test("reduceRun ignores task events that are not part of a workflow run", () => {
  const st = W.reduceRun(W.emptyRun(), ev(1, "task.created", { task_id: 9, goal: "x" }));
  assert.strictEqual(st.run_id, "");
  assert.deepStrictEqual(st.steps, {});
});

test("runIdOf/stepIdOf decode the engine's op correlation ids", () => {
  assert.strictEqual(W.runIdOf({ op: "wf:12" }), "12");
  assert.strictEqual(W.runIdOf({ op: "wf:12:s1" }), "12");
  assert.strictEqual(W.runIdOf({ run_id: 5 }), "5");
  assert.strictEqual(W.stepIdOf({ op: "wf:12:s1" }), "s1");
  assert.strictEqual(W.stepIdOf({ step: "sx" }), "sx");
});

/* --------------------------------------------------------- runtime pipeline */

test("pipelineStageOf maps the real emitted kinds onto real stages", () => {
  assert.strictEqual(W.pipelineStageOf("chat.pipeline.started", {}, "understand"), "request");
  assert.strictEqual(W.pipelineStageOf("astra_gateway.request", {}, "understand"), "understand");
  assert.strictEqual(W.pipelineStageOf("astra_gateway.request", {}, "verify"), "verify");
  assert.strictEqual(W.pipelineStageOf("router.decision", {}, "understand"), "route");
  assert.strictEqual(W.pipelineStageOf("router.gateway_task_completion", {}, "verify"), "verify");
  assert.strictEqual(W.pipelineStageOf("agent.tool_loop.step", {}, ""), "execute");
  assert.strictEqual(W.pipelineStageOf("ai.completed", {}, ""), "execute");
  assert.strictEqual(W.pipelineStageOf("tool.completed", {}, ""), "tools");
  assert.strictEqual(W.pipelineStageOf("terminal.started", {}, ""), "tools");
  assert.strictEqual(W.pipelineStageOf("memory.saved", {}, ""), "memory");
  assert.strictEqual(W.pipelineStageOf("chat.pipeline.finished", {}, ""), "reply");
  assert.strictEqual(W.pipelineStageOf("something.else", {}, ""), "");
});

test("turnKeyOf groups a turn by request/trace/chat op like the backend", () => {
  assert.strictEqual(W.turnKeyOf(ev(1, "chat.pipeline.started", { op: "chat:abc" })), "abc");
  assert.strictEqual(W.turnKeyOf(ev(2, "router.decision", { trace: "abc" })), "abc");
  assert.strictEqual(W.turnKeyOf(ev(3, "router.decision", { request: "abc" })), "abc");
  assert.strictEqual(W.turnKeyOf(ev(4, "scheduler.tick", {})), "");
});

test("reduceTurn tracks provider/model, tools, phase switch and terminal status",
  () => {
    const events = [
      ev(1, "chat.pipeline.started", { op: "chat:r1", gateway: "astra" }),
      ev(2, "astra_gateway.success", { trace: "r1", provider: "groq" }),
      ev(3, "router.decision", { trace: "r1", provider: "groq", model: "llama-3.3" }),
      ev(4, "chat.pipeline.assigned", { op: "chat:r1", provider: "groq", capability: "terminal" }),
      ev(5, "agent.tool_loop.started", { trace: "r1" }),
      ev(6, "tool.completed", { trace: "r1", tool: "terminal_exec" }),
      ev(7, "chat.pipeline.verified", { op: "chat:r1", verdict: "complete" }),
      ev(8, "chat.pipeline.finished", { op: "chat:r1", status: "ok" }),
    ];
    let t = W.newTurn("r1", events[0], 1);
    events.forEach((e) => { t = W.reduceTurn(t, e); });
    assert.strictEqual(t.provider, "groq");
    assert.strictEqual(t.model, "llama-3.3");
    assert.deepStrictEqual(t.tools, ["terminal_exec"]);
    assert.strictEqual(t.verdict, "complete");
    assert.strictEqual(t.phase, "verify");
    assert.strictEqual(t.stages.route.status, "ok");
    assert.strictEqual(t.stages.tools.status, "ok");
    assert.strictEqual(t.status, "ok");
  });

test("reduceTurn marks a router fallback as a warning (a real retry/fallback)",
  () => {
    const t = W.reduceTurn(W.newTurn("r", ev(1, "router.request", { trace: "r" }), 1),
      ev(2, "router.fallback", { trace: "r", provider: "b", fallback: true,
                                 reason: "rate limited" }));
    assert.strictEqual(t.stages.route.status, "warn");
  });

test("reduceTurn surfaces a failed turn from chat.pipeline.failed", () => {
  let t = W.newTurn("r", ev(1, "chat.pipeline.started", { op: "chat:r" }), 1);
  t = W.reduceTurn(t, ev(2, "chat.pipeline.failed",
    { op: "chat:r", error: "provider down", terminal: true }));
  assert.strictEqual(t.status, "err");
  assert.strictEqual(t.stages.reply.status, "err");
});

test("a finished turn closes the stages it left running", () => {
  let t = W.newTurn("r", ev(1, "chat.pipeline.started", { op: "chat:r" }), 1);
  t = W.reduceTurn(t, ev(1, "chat.pipeline.started", { op: "chat:r" }));
  t = W.reduceTurn(t, ev(2, "router.request", { trace: "r", task: "simple_chat" }));
  t = W.reduceTurn(t, ev(3, "ai.started", { trace: "r", provider: "groq", model: "m" }));
  t = W.reduceTurn(t, ev(4, "chat.pipeline.finished", { op: "chat:r", status: "COMPLETE" }));
  assert.strictEqual(t.status, "ok");
  assert.strictEqual(t.stages.request.status, "ok");
  assert.strictEqual(t.stages.route.status, "ok");
  assert.strictEqual(t.stages.execute.status, "ok");
});

test("verification status drives the turn status (real outcome values)", () => {
  // ChatPipeline emits status = TaskVerificationOutcome.status, or "skipped"
  // when the Gateway is unavailable — so an unverified reply is a warning.
  const finish = (status) => W.reduceTurn(
    W.reduceTurn(W.newTurn("r", ev(1, "chat.pipeline.started", { op: "chat:r" }), 1),
                 ev(1, "chat.pipeline.started", { op: "chat:r" })),
    ev(2, "chat.pipeline.finished", { op: "chat:r", status: status })).status;
  assert.strictEqual(finish("COMPLETE"), "ok");
  assert.strictEqual(finish("INCOMPLETE"), "warn");
  assert.strictEqual(finish("UNCERTAIN"), "warn");
  assert.strictEqual(finish("skipped"), "warn");
  assert.strictEqual(finish("FAILED"), "err");
});

test("pipelineTurns groups unrelated turns separately and keeps order", () => {
  const turns = W.pipelineTurns([
    ev(1, "chat.pipeline.started", { op: "chat:A" }),
    ev(2, "chat.pipeline.started", { op: "chat:B" }),
    ev(3, "chat.pipeline.finished", { op: "chat:A", status: "ok" }),
    ev(4, "chat.pipeline.finished", { op: "chat:B", status: "ok" }),
    ev(5, "scheduler.tick", {}),
  ]);
  assert.deepStrictEqual(turns.map((t) => t.key), ["A", "B"]);
  assert.strictEqual(turns[0].status, "ok");
});

/* --------------------------------------------------------------- builder */

test("validateDefinition reports what the backend/engine really rejects", () => {
  const known = { get_health: {}, remember: {} };
  let v = W.validateDefinition("", [{ id: "a", tool: "get_health" }], known);
  assert.strictEqual(v.ok, false);
  assert.match(v.errors.join(" "), /name is required/);
  v = W.validateDefinition("wf", [{ id: "a", tool: "nope" }], known);
  assert.match(v.errors.join(" "), /unknown tool 'nope'/);
  v = W.validateDefinition("wf", [{ id: "a", tool: "get_health", depends_on: ["zz"] }], known);
  assert.match(v.errors.join(" "), /depends_on 'zz'/);
  v = W.validateDefinition("wf", [{ id: "a", tool: "get_health" },
                                  { id: "a", tool: "remember" }], known);
  assert.match(v.errors.join(" "), /duplicate step id 'a'/);
  v = W.validateDefinition("wf", [{ id: "a", tool: "get_health" },
                                  { id: "b", tool: "remember", if: { step: "a", op: "error" } }], known);
  assert.strictEqual(v.ok, true);
  assert.match(v.warnings.join(" "), /engine\._condition only acts on 'ok'/);
});

test("newStepTemplate chains from the previous step and prefills required args", () => {
  const tool = { name: "write_file",
                 input_schema: { properties: {
                   path: { type: "string", required: true },
                   content: { type: "string" },
                   overwrite: { type: "bool" } } } };
  const step = W.newStepTemplate(tool, [{ id: "step1", tool: "get_health" }]);
  assert.strictEqual(step.id, "step2");
  assert.strictEqual(step.tool, "write_file");
  assert.deepStrictEqual(step.depends_on, ["step1"]);
  assert.deepStrictEqual(step.params, { path: "" });
});

test("parseParamsJson accepts objects and rejects non-objects/invalid JSON", () => {
  assert.deepStrictEqual(W.parseParamsJson(""), { ok: true, value: {} });
  assert.deepStrictEqual(W.parseParamsJson('{"a":1}').value, { a: 1 });
  assert.strictEqual(W.parseParamsJson("[1,2]").ok, false);
  assert.strictEqual(W.parseParamsJson("{bad}").ok, false);
});

test("resolveParams substitutes {{step.param}} / run params like engine._lookup", () => {
  const resolved = W.resolveParams(
    { path: "{{w.output.path}}", n: "{{limit}}", plain: 5 },
    { w: { output: { path: "/tmp/n.txt" } } },
    { limit: 3 });
  assert.strictEqual(resolved.path, "/tmp/n.txt");
  assert.strictEqual(resolved.n, "3");
  assert.strictEqual(resolved.plain, 5);
  // a missing ref resolves to "" exactly like engine._lookup
  assert.strictEqual(W.resolveParams({ x: "{{nope.k}}" }, {}, {}).x, "");
});

test("every state the graph can render has a real status + label mapping", () => {
  W.STEP_STATES.forEach((s) => {
    assert.ok(W.STEP_STATE_STATUS[s], "status for " + s);
    assert.ok(W.STEP_STATE_LABEL[s], "label for " + s);
  });
  W.WORKFLOW_STATUSES.forEach((s) => {
    assert.ok(W.RUN_STATUS_STATUS[s], "run status for " + s);
  });
  assert.deepStrictEqual(W.WORKFLOW_STATUSES,
    ["created", "running", "paused", "completed", "failed", "cancelled"]);
  // "retrying" is NOT a workflow step state — the engine never emits it
  assert.strictEqual(W.STEP_STATES.indexOf("retrying"), -1);
});
