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
