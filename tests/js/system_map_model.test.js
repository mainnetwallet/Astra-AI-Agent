"use strict";
const test = require("node:test");
const assert = require("node:assert");
const M = require("../../static/js/system_map_model.js");

test("providers are built only from the registry payload, never hardcoded", () => {
  assert.deepStrictEqual(M.providerCards({ providers: {} }), []);
  assert.deepStrictEqual(M.providerCards(null), []);
  const cards = M.providerCards({ providers: {
    zeta: { state: "healthy", healthy: true, models: ["a", "b"], latency_avg_ms: 120, calls: 10, errors: 1, credentials: 2, keys: ["sk-SECRET"], base_url: "https://x" },
    alpha: { state: "cooldown", models: [], calls: 0, errors: 0 },
  } });
  assert.deepStrictEqual(cards.map((c) => c.name), ["alpha", "zeta"]);
  assert.strictEqual(cards[0].status, "rate_limited");
  assert.strictEqual(cards[1].status, "online");
  assert.strictEqual(cards[1].successRate, 90);
  assert.strictEqual(cards[0].successRate, null);          // no calls => Unavailable, not 0/100
  assert.strictEqual(cards[1].keyCount, 2);
  assert.ok(!JSON.stringify(cards).includes("sk-SECRET"), "credentials must never leak into view data");
  assert.ok(!JSON.stringify(cards).includes("https://x"));
});

test("provider status mapping", () => {
  assert.strictEqual(M.providerStatus("healthy"), "online");
  assert.strictEqual(M.providerStatus("down"), "offline");
  assert.strictEqual(M.providerStatus("degraded"), "degraded");
  assert.strictEqual(M.providerStatus(undefined, false), "offline");
  assert.strictEqual(M.providerStatus(undefined), "unknown");
});

test("tools are grouped from the real registry payload", () => {
  const out = M.toolGroups({ tools: [
    { name: "terminal_exec", category: "terminal", description: "run", input_schema: { type: "object" }, risk_level: "exec", requires_confirmation: true },
    { name: "browser_open", category: "browser" },
    { name: "mine", category: "misc", plugin: "user" },
  ], stats: { terminal_exec: { calls: 3, errors: 1 } } });
  assert.strictEqual(out.total, 3);
  const names = out.groups.map((g) => g.name);
  assert.ok(names.includes("Terminal") && names.includes("Browser") && names.includes("Custom"));
  const t = out.groups.find((g) => g.name === "Terminal").tools[0];
  assert.strictEqual(t.calls, 3); assert.strictEqual(t.requiresConfirmation, true);
  assert.deepStrictEqual(M.toolGroups(null).groups, []);
});

test("events map to sources and error flags", () => {
  assert.strictEqual(M.sourceOfKind("astra_gateway.success"), "Gateway");
  assert.strictEqual(M.sourceOfKind("router.decision"), "Router");
  assert.strictEqual(M.sourceOfKind("tool.completed"), "Tool");
  assert.strictEqual(M.sourceOfKind("memory.saved"), "Memory");
  const row = M.eventRow({ id: 5, kind: "tool.failed", data: { tool: "x" }, created_at: "2026-09-28 09:25:12" });
  assert.strictEqual(row.time, "09:25:12"); assert.strictEqual(row.error, true); assert.strictEqual(row.source, "Tool");
  assert.strictEqual(M.filterEvents([row, { ...row, error: false, source: "Agent" }], "errors").length, 1);
});

test("operations pair start and terminal events by op id", () => {
  const ev = (id, kind, op, extra) => ({ id, kind, agent: "", data: { op, ...extra }, created_at: "2026-09-28 09:00:0" + id });
  const ops = M.operationsFromEvents([
    ev(1, "tool.started", "a", { tool: "file_read" }), ev(2, "tool.completed", "a"),
    ev(3, "ai.started", "b", { provider: "groq", model: "m" }),
    ev(4, "tool.started", "c"), ev(5, "tool.failed", "c"),
    { id: 6, kind: "memory.saved", data: {}, created_at: "2026-09-28 09:00:06" },   // no op => not an operation
  ], Date.parse("2026-09-28T09:00:09"));
  const by = Object.fromEntries(ops.map((o) => [o.id, o]));
  assert.strictEqual(ops.length, 3);
  assert.strictEqual(by.a.status, "completed"); assert.strictEqual(by.a.tool, "file_read");
  assert.strictEqual(by.b.status, "running"); assert.strictEqual(by.b.provider, "groq");
  assert.strictEqual(by.c.status, "failed");
  assert.strictEqual(by.b.durationMs, 6000);
});

test("tool loop highlights the step of the latest real event", () => {
  assert.strictEqual(M.activeLoopStep([]), -1);
  assert.strictEqual(M.activeLoopStep([{ kind: "tool.started" }]), 3);
  assert.strictEqual(M.activeLoopStep([{ kind: "tool.started" }, { kind: "tool.completed" }]), 4);
});

test("nodes show Unavailable/Planned instead of invented data", () => {
  const nodes = M.buildNodes({ providerCards: [], tools: null, gateway: null, agents: [], security: null });
  const by = Object.fromEntries(nodes.map((n) => [n.id, n]));
  assert.ok(by.interfaces.lines.some((l) => l.k === "Mobile" && l.v === "Planned" && l.s === "planned"));
  assert.ok(/Unavailable/.test(by.agents.lines[0].v));
  assert.ok(/Unavailable/.test(by.security.lines[0].v));
  assert.ok(/NOT a provider/.test(by.gateway.lines[0].v) && /NOT a provider/.test(by.router.lines[0].v));
  assert.strictEqual(by.providers.lines[0].k, "No providers configured");
});

test("provider and agent nodes are generated from live data", () => {
  const cards = M.providerCards({ providers: { groq: { state: "healthy", models: ["m1"] } } });
  const nodes = M.buildNodes({ providerCards: cards, agents: [{ name: "research", description: "d" }], tools: M.toolGroups({ tools: [{ name: "t", category: "file" }] }) });
  const by = Object.fromEntries(nodes.map((n) => [n.id, n]));
  assert.strictEqual(by.providers.lines[0].k, "groq");
  assert.strictEqual(by.agents.lines[0].k, "research");
  assert.strictEqual(by.tools.lines[0].k, "File");
});

test("search, neighbours and KPIs", () => {
  const nodes = M.buildNodes({ providerCards: [], agents: [] });
  assert.strictEqual(M.searchNodes(nodes, ""), null);
  assert.ok(M.searchNodes(nodes, "toolregistry").has("tools"));
  assert.ok(M.neighbours("gateway").has("router"));
  const k = M.kpis({ providerCards: [], health: null, eventsOk: false, agents: [] }, []);
  assert.strictEqual(k.length, 8);
  assert.strictEqual(k.find((x) => x.id === "ops").value, "Unavailable");
  assert.strictEqual(k.find((x) => x.id === "system").value, "Unavailable");
});

test("subsystem health reports Unavailable when nothing is known", () => {
  const rows = M.subsystemHealth({ providerCards: [] });
  assert.deepStrictEqual(rows.map((r) => r.name), ["API", "Gateway", "Router", "Providers", "ToolRegistry", "Memory", "Workflow Engine", "EventBus", "Web3", "Terminal"]);
  assert.ok(rows.every((r) => r.status === "unavailable"));
});

test("replayed events never double-count an operation", () => {
  const ev = (id, kind, op, extra) => ({ id, kind, agent: "", data: { op, ...extra }, created_at: "2026-09-28 09:00:0" + id });
  const once = [ev(1, "tool.started", "a", { tool: "file_read" }), ev(2, "tool.completed", "a")];
  const now = Date.parse("2026-09-28T09:00:09");
  const single = M.operationsFromEvents(once, now);
  // The SSE reference point is re-established on every reconnect and the
  // history endpoint is re-read on every visit, so the same event can arrive
  // many times; the derived operation must stay identical.
  const replay = M.operationsFromEvents([...once, ...once, ...once], now);
  assert.strictEqual(replay.length, 1);
  assert.deepStrictEqual(replay[0], single[0]);
  assert.strictEqual(replay[0].status, "completed");
  assert.strictEqual(replay[0].durationMs, single[0].durationMs);
});

test("a terminal event without a start is not given an invented duration", () => {
  const [op] = M.operationsFromEvents([
    { id: 1, kind: "workflow.completed", agent: "wf", data: { op: "wf-1" }, created_at: "2026-09-28 09:00:03" },
  ], Date.parse("2026-09-28T09:00:09"));
  assert.strictEqual(op.status, "completed");
  assert.strictEqual(op.startedAt, null);      // the start was never observed
  assert.strictEqual(op.durationMs, null);     // so the duration is Unavailable, not guessed
});
