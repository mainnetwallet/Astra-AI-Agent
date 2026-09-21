/* Unit tests for the Activity Log presentation model + live-scroll state
 * machine (static/js/log_model.js). Pure logic, no DOM: run with
 * `node --test tests/js/` or via tests/test_log_model_js.py.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const Log = require("../../static/js/log_model.js");

function ev(kind, data, agent) {
  return { id: 1, kind, agent: agent || "", data: data || {},
           created_at: "2026-09-21 21:42:18" };
}

test("normalize: tool completion is a readable timeline row", () => {
  const m = Log.normalize(ev("tool.completed",
    { tool: "web_search", category: "builtin", duration_ms: 1240,
      input: '{"query":"astra"}', output: "8 results" }));
  assert.strictEqual(m.category, "tools");
  assert.strictEqual(m.status, "ok");
  assert.strictEqual(m.icon, "⚡");
  assert.strictEqual(m.title, "Tool execution");
  assert.strictEqual(m.subject, "web_search");
  assert.strictEqual(m.detail, "1.24s");
  assert.strictEqual(m.time, "21:42:18");
  assert.match(m.input, /astra/);
  assert.strictEqual(m.output, "8 results");
});

test("normalize: gateway call carries provider and model", () => {
  const m = Log.normalize(ev("astra_gateway.success",
    { provider: "groq", model: "llama-3.3-70b", latency_ms: 2830 }));
  assert.strictEqual(m.category, "ai");
  assert.strictEqual(m.status, "ok");
  assert.strictEqual(m.subject, "groq · llama-3.3-70b");
  assert.strictEqual(m.detail, "2.83s");
});

test("normalize: failures are errors with the reason", () => {
  const m = Log.normalize(ev("tool.failed",
    { tool: "fetch_url", error: "blocked private host" }));
  assert.strictEqual(m.status, "err");
  assert.strictEqual(m.detail, "blocked private host");
});

test("normalize: correction requested is a warning", () => {
  const m = Log.normalize(ev("gateway.supervision.correction_requested",
    { provider: "groq", attempt: 2 }));
  assert.strictEqual(m.status, "warn");
  assert.strictEqual(m.title, "Result supervision");
});

test("normalize: web3 lifecycle maps to web3 category", () => {
  ["prepared", "submitted", "broadcast", "rejected", "confirmed", "failed"]
    .forEach((stage) => {
      const m = Log.normalize(ev("web3.transaction." + stage,
        { tx: "0xabc", reason: "" }));
      assert.strictEqual(m.category, "web3");
      assert.match(m.title, /Transaction/);
    });
});

test("categoryOf routes every family to a filter chip", () => {
  assert.strictEqual(Log.categoryOf("tool.completed"), "tools");
  assert.strictEqual(Log.categoryOf("ai.completed"), "ai");
  assert.strictEqual(Log.categoryOf("astra_gateway.success"), "ai");
  assert.strictEqual(Log.categoryOf("router.decision"), "ai");
  assert.strictEqual(Log.categoryOf("provider.health_changed"), "ai");
  assert.strictEqual(Log.categoryOf("chat.pipeline.started"), "agents");
  assert.strictEqual(Log.categoryOf("workflow.completed"), "agents");
  assert.strictEqual(Log.categoryOf("memory.saved"), "agents");
  assert.strictEqual(Log.categoryOf("browser.opened"), "browser");
  assert.strictEqual(Log.categoryOf("web3.transaction.confirmed"), "web3");
});

test("isMeaningful drops heartbeat/duplicate noise, keeps activity", () => {
  assert.strictEqual(Log.isMeaningful(ev("ai.token", {}, "provider")), false);
  assert.strictEqual(Log.isMeaningful(ev("scheduler.tick", {}, "scheduler")), false);
  // gateway's own streaming ai.* duplicates the astra_gateway.* wrapper
  assert.strictEqual(Log.isMeaningful(ev("ai.completed", {}, "gateway")), false);
  // provider-router ai.* is real activity
  assert.strictEqual(Log.isMeaningful(ev("ai.completed", {}, "router")), true);
  assert.strictEqual(Log.isMeaningful(ev("tool.completed", {})), true);
  assert.strictEqual(Log.isMeaningful(ev("web3.transaction.prepared", {})), true);
});

test("orderHistory renders oldest -> newest regardless of input order", () => {
  const rows = [{ id: 3 }, { id: 1 }, { id: 2 }];
  assert.deepStrictEqual(Log.orderHistory(rows).map((r) => r.id), [1, 2, 3]);
  // does not mutate the input
  assert.deepStrictEqual(rows.map((r) => r.id), [3, 1, 2]);
});

test("scrub masks credential-shaped values", () => {
  assert.match(Log.scrub("key sk-abcdefghij0123456789 done"), /\*\*\*redacted\*\*\*/);
  assert.match(Log.scrub("Authorization: Bearer abc.def.ghi"), /\*\*\*redacted\*\*\*/);
  assert.match(Log.scrub("0x" + "a".repeat(64)), /\*\*\*redacted\*\*\*/);
});

test("normalize never emits secret-named fields", () => {
  const m = Log.normalize(ev("tool.completed",
    { tool: "x", api_key: "sk-secret-1234567890", token: "ghp_" + "a".repeat(24) }));
  const flat = JSON.stringify(m);
  assert.ok(!flat.includes("sk-secret-1234567890"));
  assert.ok(!flat.includes("ghp_aaaa"));
});

test("long subjects/details are clipped so the layout survives", () => {
  const m = Log.normalize(ev("tool.completed",
    { tool: "t".repeat(500), duration_ms: 10 }));
  assert.ok(m.subject.length <= 81);
  const f = Log.normalize(ev("tool.failed", { tool: "x", error: "e".repeat(600) }));
  assert.ok(f.detail.length <= 121);
});

/* ---------------------------------------------------------- scroll machine */

const BOTTOM = { scrollTop: 900, scrollHeight: 1200, clientHeight: 300 };

test("onAppend follows to the bottom when the user is at the bottom", () => {
  const state = { follow: true, unread: 0 };
  const r = Log.onAppend(state, BOTTOM);
  assert.strictEqual(r.scrollToBottom, true);
  assert.strictEqual(r.showIndicator, false);
  assert.strictEqual(state.unread, 0);
});

test("scroll up stops auto-follow and appends do not yank the view", () => {
  const state = { follow: true, unread: 0 };
  Log.onScroll(state, { scrollTop: 100, scrollHeight: 1200, clientHeight: 300 });
  assert.strictEqual(state.follow, false);
  const r = Log.onAppend(state, { scrollTop: 100, scrollHeight: 1200, clientHeight: 300 });
  assert.strictEqual(r.scrollToBottom, false);
  assert.strictEqual(r.showIndicator, true);
  assert.strictEqual(state.unread, 1);
  Log.onAppend(state, { scrollTop: 100, scrollHeight: 1200, clientHeight: 300 });
  assert.strictEqual(state.unread, 2);
});

test("returning to the bottom resumes follow and clears the indicator", () => {
  const state = { follow: false, unread: 5 };
  Log.onScroll(state, BOTTOM);
  assert.strictEqual(state.follow, true);
  assert.strictEqual(state.unread, 0);
});

test("jump to latest re-enables follow and clears unread", () => {
  const state = { follow: false, unread: 9 };
  Log.onJumpToLatest(state);
  assert.strictEqual(state.follow, true);
  assert.strictEqual(state.unread, 0);
});

test("near-bottom uses a small threshold, not an exact match", () => {
  assert.strictEqual(Log.isNearBottom(
    { scrollTop: 880, scrollHeight: 1200, clientHeight: 300 }), true);
  assert.strictEqual(Log.isNearBottom(
    { scrollTop: 500, scrollHeight: 1200, clientHeight: 300 }), false);
});

test("trimming the DOM buffer keeps the reader's position stable", () => {
  assert.strictEqual(Log.compensateTrim(400, 60), 340);
  assert.strictEqual(Log.compensateTrim(10, 60), 0);
});

/* ------------------------------------------------ controller state machine */

function seq(n, start) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(ev("tool.completed", { tool: "t" + i }));
  out.forEach((e, i) => { e.id = (start || 0) + i + 1; });
  return out;
}

test("createState starts following, empty and unfiltered", () => {
  const s = Log.createState();
  assert.strictEqual(s.filter, "all");
  assert.strictEqual(s.paused, false);
  assert.strictEqual(s.follow, true);
  assert.strictEqual(s.counts.total, 0);
  assert.strictEqual(s.rendered.size, 0);
  assert.deepStrictEqual(s.pending, []);
});

test("admit renders fresh activity, skips noise, skips duplicates", () => {
  const s = Log.createState();
  const e = ev("tool.completed", { tool: "x" });
  assert.strictEqual(Log.admit(s, e), "render");
  Log.markRendered(s, e);
  assert.strictEqual(Log.admit(s, e), "skip");           // same id again
  assert.strictEqual(Log.admit(s, ev("scheduler.tick", {})), "skip");
  assert.strictEqual(Log.admit(s, null), "skip");
});

test("pause buffers arrivals and resumed flush is still in order", () => {
  const s = Log.createState();
  const events = seq(5);
  Log.admit(s, events[0]); Log.markRendered(s, events[0]);
  s.paused = true;
  events.slice(1).forEach((e) => {
    assert.strictEqual(Log.admit(s, e), "buffer");
    Log.buffer(s, e);
  });
  assert.strictEqual(s.pending.length, 4);
  // resume: pending replays in arrival order, then admit() renders again
  s.paused = false;
  const order = s.pending.map((e) => e.id);
  assert.deepStrictEqual(order, [2, 3, 4, 5]);
});

test("clear resets counts, dedupe set, pending and follow", () => {
  const s = Log.createState();
  Log.count(s, Log.normalize(seq(1)[0]));
  Log.markRendered(s, seq(1)[0]);
  s.pending.push(seq(1)[0]);
  Log.onScroll(s, { scrollTop: 0, scrollHeight: 1200, clientHeight: 300 });
  Log.reset(s);
  assert.strictEqual(s.counts.total, 0);
  assert.strictEqual(s.rendered.size, 0);
  assert.strictEqual(s.pending.length, 0);
  assert.strictEqual(s.follow, true);
  assert.strictEqual(s.unread, 0);
});

test("count tracks the category and the error total", () => {
  const s = Log.createState();
  Log.count(s, Log.normalize(ev("tool.completed", { tool: "a" })));
  Log.count(s, Log.normalize(ev("browser.error", { error: "no" })));
  Log.count(s, Log.normalize(ev("astra_gateway.success", { provider: "groq" })));
  assert.strictEqual(s.counts.total, 3);
  assert.strictEqual(s.counts.tools, 1);
  assert.strictEqual(s.counts.browser, 1);
  assert.strictEqual(s.counts.ai, 1);
  assert.strictEqual(s.counts.errors, 1);
});

test("matchesRow drives the filter chips and search", () => {
  const m = Log.normalize(ev("tool.completed", { tool: "web_search" }));
  assert.ok(Log.matchesRow("tools", m.search, "all", ""));
  assert.ok(Log.matchesRow("tools", m.search, "tools", ""));
  assert.ok(!Log.matchesRow("tools", m.search, "browser", ""));
  assert.ok(Log.matchesRow("tools,errors", m.search, "errors", ""));
  assert.ok(Log.matchesRow("tools", m.search, "all", "WEB_SEARCH"));
  assert.ok(!Log.matchesRow("tools", m.search, "all", "nope"));
});

test("high volume keeps the dedupe set bounded (no memory leak)", () => {
  const s = Log.createState();
  seq(5000).forEach((e) => { Log.markRendered(s, e); });
  assert.ok(s.rendered.size <= Log.RENDERED_MAX);
  // the newest ids are still remembered (recent re-delivery is deduped)
  assert.ok(s.rendered.has("5000"));
});

test("the paused buffer is bounded too", () => {
  const s = Log.createState();
  s.paused = true;
  seq(2000).forEach((e) => { if (Log.admit(s, e) === "buffer") Log.buffer(s, e); });
  assert.strictEqual(s.pending.length, Log.PENDING_MAX);
  // the most recent events survive the cap
  assert.strictEqual(s.pending[s.pending.length - 1].id, 2000);
});

/* --------------------------------------------------- lifecycle reconciliation
 * Mirrors the bookkeeping astra.js performs around the DOM: a lifecycle
 * continuation must update the SAME row, never append a second one. */

function lifeEvent(kind, data, id) {
  const e = ev(kind, data);
  e.id = id;
  return e;
}

// Simulate the render loop: returns the rows the UI would end up with.
function simulate(events) {
  const state = Log.createState();
  const rows = [];
  events.forEach((e) => {
    const m = Log.normalize(e);
    const plan = Log.planRender(state, e, m);
    const info = plan.key ? state.active.get(plan.key) : null;
    let row;
    if (info && info.el && plan.action === "update") {
      row = info.el;
      Log.recount(state, row.model, m);
      row.model = m;
    } else {
      row = { key: plan.key, model: m };
      rows.push(row);
      Log.count(state, m);
    }
    plan.closeKeys.forEach((k) => {
      const ci = state.active.get(k);
      if (ci && ci.el && ci.el.model.status === "running") {
        // mirrors astra.js resolveRow(): a child still running when its
        // request/run ended is marked interrupted with a reason.
        const reason = "interrupted when the request ended";
        const prev = ci.el.model;
        ci.el.model = Object.assign({}, prev, {
          status: "warn", detail: reason, icon: prev.icon,
          search: (prev.search + " " + reason).toLowerCase(),
        });
      }
    });
    Log.commit(state, plan, m);
    const tracked = plan.key ? state.active.get(plan.key) : null;
    if (tracked) tracked.el = row;
  });
  return { state, rows };
}

test("started -> completed produces ONE resolved row", () => {
  const { state, rows } = simulate([
    lifeEvent("tool.started", { op: "T1", tool: "web_search" }, 1),
    lifeEvent("tool.completed", { op: "T1", tool: "web_search",
                                  duration_ms: 1240, terminal: true }, 2),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
  assert.strictEqual(rows[0].model.detail, "1.24s");
  assert.strictEqual(state.active.size, 0);
  assert.strictEqual(state.counts.total, 1);
});

test("started -> failed produces ONE failed row", () => {
  const { state, rows } = simulate([
    lifeEvent("tool.started", { op: "T2", tool: "fetch_url" }, 1),
    lifeEvent("tool.failed", { op: "T2", tool: "fetch_url", terminal: true,
                               error: "boom" }, 2),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "err");
  assert.strictEqual(rows[0].model.detail, "boom");
  assert.strictEqual(state.active.size, 0);
  assert.strictEqual(state.counts.errors, 1);
});

test("started -> timeout resolves correctly", () => {
  assert.strictEqual(Log.phaseOf("ai.timeout", {}), "terminal");
  const { state, rows } = simulate([
    lifeEvent("tool.started", { op: "T3", tool: "slow" }, 1),
    lifeEvent("tool.failed", { op: "T3", tool: "slow", terminal: true,
                               error: "tool 'slow' timed out after 5s" }, 2),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "err");
  assert.match(rows[0].model.detail, /timed out/);
  assert.strictEqual(state.active.size, 0);
});

test("two concurrent identical operations stay separate", () => {
  const { state, rows } = simulate([
    lifeEvent("ai.started", { op: "A", provider: "groq", model: "m" }, 1),
    lifeEvent("ai.started", { op: "B", provider: "groq", model: "m" }, 2),
    lifeEvent("ai.completed", { op: "B", provider: "groq", model: "m",
                                terminal: true, latency_ms: 100 }, 3),
    lifeEvent("ai.completed", { op: "A", provider: "groq", model: "m",
                                terminal: true, latency_ms: 200 }, 4),
  ]);
  assert.strictEqual(rows.length, 2);
  assert.deepStrictEqual(rows.map((r) => r.model.status), ["ok", "ok"]);
  // B completed first but A's row must not have been overwritten
  assert.strictEqual(rows.find((r) => r.key === "op:A").model.detail, "200ms");
  assert.strictEqual(rows.find((r) => r.key === "op:B").model.detail, "100ms");
  assert.strictEqual(state.active.size, 0);
});

test("retry updates the running row and the terminal failure resolves it", () => {
  const { state, rows } = simulate([
    lifeEvent("ai.started", { op: "R", provider: "x", model: "m" }, 1),
    lifeEvent("router.retry", { op: "R", provider: "x", model: "m", attempt: 2 }, 2),
    lifeEvent("ai.failed", { op: "R", provider: "x", model: "m", attempt: 1,
                             retrying: true, terminal: false, error: "rate" }, 3),
    lifeEvent("ai.failed", { op: "R", provider: "x", model: "m", attempt: 2,
                             terminal: true, error: "still failing" }, 4),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "err");
  assert.strictEqual(state.active.size, 0);
});

test("retry then success resolves the SAME row to ok", () => {
  const { rows } = simulate([
    lifeEvent("ai.started", { op: "S", provider: "x", model: "m" }, 1),
    lifeEvent("ai.failed", { op: "S", provider: "x", model: "m",
                             retrying: true, terminal: false, error: "rate" }, 2),
    lifeEvent("ai.completed", { op: "S", provider: "x", model: "m",
                                terminal: true, latency_ms: 90 }, 3),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
});

test("a terminal event with no prior start is one resolved row", () => {
  const { rows } = simulate([
    lifeEvent("tool.completed", { op: "Z", tool: "t", terminal: true }, 1),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
});

test("final Response generated leaves no stale running operations", () => {
  const R = "req-1";
  const { state, rows } = simulate([
    lifeEvent("chat.pipeline.started", { op: "chat:" + R, request: R, trace: R }, 1),
    lifeEvent("astra_gateway.request", { op: "G1", trace: R, category: "general" }, 2),
    lifeEvent("astra_gateway.success", { op: "G1", trace: R, terminal: true,
                                         provider: "gemini", model: "g" }, 3),
    lifeEvent("router.request", { op: "RT", trace: R, task: "coding" }, 4),
    // this gateway call never reports a terminal event
    lifeEvent("astra_gateway.request", { op: "G2", trace: R, category: "x" }, 5),
    lifeEvent("chat.pipeline.finished", { op: "chat:" + R, request: R, trace: R,
                                          terminal: true, status: "COMPLETE" }, 6),
  ]);
  assert.strictEqual(rows.length, 4);
  assert.strictEqual(state.active.size, 0);
  const stale = rows.filter((r) => r.model.status === "running");
  assert.strictEqual(stale.length, 0, "no operation left running");
  const routerRow = rows.find((r) => r.key === "op:RT");
  assert.strictEqual(routerRow.model.status, "warn");
  assert.match(routerRow.model.detail, /interrupted/);
  const finalRow = rows.find((r) => r.key === "op:chat:" + R);
  assert.strictEqual(finalRow.model.status, "ok");
});

test("a failed request resolves its children as well", () => {
  const R = "req-err";
  const { state, rows } = simulate([
    lifeEvent("chat.pipeline.started", { op: "chat:" + R, request: R, trace: R }, 1),
    lifeEvent("astra_gateway.request", { op: "G9", trace: R }, 2),
    lifeEvent("chat.pipeline.failed", { op: "chat:" + R, request: R, trace: R,
                                        terminal: true, error: "boom" }, 3),
  ]);
  assert.strictEqual(state.active.size, 0);
  assert.strictEqual(rows.find((r) => r.key === "op:chat:" + R).model.status, "err");
  assert.strictEqual(rows.find((r) => r.key === "op:G9").model.status, "warn");
});

test("web3 stages update one row per tx and only confirmed ends it", () => {
  const { state, rows } = simulate([
    lifeEvent("web3.transaction.prepared", { tx: "0xabc" }, 1),
    lifeEvent("web3.transaction.submitted", { tx: "0xabc" }, 2),
    lifeEvent("web3.transaction.broadcast", { tx: "0xabc" }, 3),
    lifeEvent("web3.transaction.confirmed", { tx: "0xabc" }, 4),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
  assert.strictEqual(state.active.size, 0);
});

test("concurrent requests do not close each other's operations", () => {
  const { state, rows } = simulate([
    lifeEvent("chat.pipeline.started", { op: "chat:A", request: "A", trace: "A" }, 1),
    lifeEvent("chat.pipeline.started", { op: "chat:B", request: "B", trace: "B" }, 2),
    lifeEvent("astra_gateway.request", { op: "GA", trace: "A" }, 3),
    lifeEvent("astra_gateway.request", { op: "GB", trace: "B" }, 4),
    lifeEvent("chat.pipeline.finished", { op: "chat:A", request: "A", trace: "A",
                                          terminal: true, status: "COMPLETE" }, 5),
  ]);
  // A's gateway op resolved as interrupted, B's is still genuinely active
  assert.strictEqual(rows.find((r) => r.key === "op:GA").model.status, "warn");
  assert.strictEqual(rows.find((r) => r.key === "op:GB").model.status, "running");
  assert.deepStrictEqual([...state.active.keys()].sort(),
                         ["op:GB", "op:chat:B"]);
});

test("generic task lifecycle resolves one row via task_id", () => {
  const { state, rows } = simulate([
    lifeEvent("task.running", { task_id: 7, goal: "research" }, 1),
    lifeEvent("task.done", { task_id: 7, goal: "research", terminal: true }, 2),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
  assert.strictEqual(state.active.size, 0);
});

test("workflow step start and terminal share one op-scoped row", () => {
  const op = "wf:1:step1";
  const { state, rows } = simulate([
    lifeEvent("task.started", { op, run_id: 1, step: "step1" }, 1),
    lifeEvent("task.completed", { op, run_id: 1, step: "step1",
                                  terminal: true }, 2),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
  assert.strictEqual(state.active.size, 0);
});

test("an intermediate update keeps the operation active until terminal", () => {
  const { state, rows } = simulate([
    lifeEvent("astra_gateway.request", { op: "GX", trace: "T" }, 1),
    // a non-terminal retry of the same op must NOT drop it from active
    lifeEvent("astra_gateway.error", { op: "GX", trace: "T",
                                       terminal: false }, 2),
    lifeEvent("astra_gateway.success", { op: "GX", trace: "T",
                                         terminal: true }, 3),
  ]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].model.status, "ok");
  assert.strictEqual(state.active.size, 0);
});
