/* Security Center UI (static/js/security_center.js) — real-data-only checks.
 * Driven through the shared DOM shim; the payload mirrors the REAL
 * GET /api/security/status shape (astra/security_center.py). */
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { Doc, HTML } = require("./dom_shim.js");
const ROOT = path.join(__dirname, "..", "..");
const SRC = fs.readFileSync(path.join(ROOT, "static/js/security_center.js"), "utf8");

const lvl = (id, name, status, level, detail) => ({ id, name, status, level, detail });
const STATUS = {
  generated_at: "2026-09-30 14:27:00",
  posture: { state: "protected", label: "Protected", summary: "No critical issues", score: null, findings: [], counts: { critical: 0, high: 0, medium: 1, passed: 6 } },
  api: { authentication: lvl("auth", "API Authentication", "Open", "warn", "x"), rate_limiting: lvl("rate_limit", "Rate Limiting", "Active", "ok", "300/60s"), security_headers: lvl("headers", "Security Headers", "Protected", "ok", "h") },
  credentials: { keys: 3, healthy: 2, failed: 1, providers: 2 },
  controls: [lvl("auth", "API Authentication", "Open", "warn", "ASTRA_TOKEN is unset"), lvl("ssrf", "SSRF Protection", "Protected", "ok", "blocked")],
  providers: [{ name: "groq", state: "healthy", level: "ok", keys: 2, healthy_keys: 2, models: 5, kind: "provider" }, { name: "Gemini", state: "degraded", level: "warn", keys: null, healthy_keys: null, models: null, kind: "gateway" }],
  providers_available: true,
  tools: { available: true, total: 59, high: 19, medium: 3, low: 37, confirmation_required: 1, agent_forbidden: 10, calls: 7, errors: 1, counted: "since server start", granted: ["read", "low_risk_write"] },
  web3: { available: true, mode: "CONFIRM", stopped: false, chains: 7, limits_configured: false },
  web3_rows: [{ k: "Confirmation Mode", v: "CONFIRM", level: "ok" }, { k: "Emergency Stop", v: "Not engaged", level: "ok" }],
  runtime_rows: [{ k: "Agent Runtime", v: null, level: "na" }, { k: "Network Access", v: "Private/loopback blocked (SSRF guard)", level: "ok" }],
  emergency: { active: false, engaged_at: null, released_at: null, last_result: null },
};

function boot(respond) {
  const doc = new Doc(HTML), calls = [];
  globalThis.window = { addEventListener() {}, Astra: { loaders: {} } };
  globalThis.document = doc;
  globalThis.esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  globalThis.api = async (url, opts) => { calls.push({ url, opts }); return respond ? respond(url, opts) : { ok: true, data: STATUS }; };
  const entry = require.resolve("../../static/js/security_center.js");
  delete require.cache[entry]; require(entry);
  return { doc, calls, win: globalThis.window };
}
const html = (env, id) => env.doc.getElementById(id).innerHTML;
async function open(respond) { const env = boot(respond); await env.win.Astra.loaders["security-center"](); env.win.SecurityCenter.state.timer && clearInterval(env.win.SecurityCenter.state.timer); return env; }
const page = (env) => ["sc-head", "sc-summary", "sc-grid", "sc-row2", "sc-emergency", "sc-backup"].map((i) => html(env, i)).join("\n");

test("index.html wires the tab, stylesheet and script after the shell", () => {
  assert.match(HTML, /id="tab-security-center"/);
  assert.match(HTML, /\/static\/css\/security_center\.css/);
  const at = (f) => HTML.indexOf('src="/static/js/' + f + '"');
  assert.ok(at("security_center.js") > at("astra_os.js"));
});

test("sidebar item opens the dedicated page and other nav items are untouched", () => {
  const os = fs.readFileSync(path.join(ROOT, "static/js/astra_os.js"), "utf8");
  assert.match(os, /id: "sec", label: "Security Center", ic: "🛡️", tab: "security-center"/);
  for (const id of ["health", "web3", "tools", "wf", "activity"]) assert.ok(os.includes(`id: "${id}"`), id);
});

test("loads ONLY from /api/security/status and renders real values", async () => {
  const env = await open();
  assert.deepStrictEqual(env.calls.map((c) => c.url), ["/api/security/status"]);
  const p = page(env);
  for (const s of ["ASTRA Security Center", "Scan Now", "Auto refresh", "2 / 3", "59 tools", "API Authentication", "groq", "Gemini", "Confirmation Mode", "Emergency Shutdown", "Total Data Backup", "Shut Down All Agents", "Create Backup", "Choose File and Import"]) {
    assert.ok(p.includes(s), "missing: " + s);
  }
});

test("Security Events is gone and nothing is invented", async () => {
  const env = await open();
  assert.doesNotMatch(page(env) + SRC + HTML.match(/id="tab-security-center"[^>]*>/)[0], /Security Events/i);
  assert.doesNotMatch(SRC, /Math\.random|setTimeout\([^)]*shutdown/i);
  assert.doesNotMatch(page(env), /Security Score/, "no score when the backend supplies none");
});

test("a numeric score renders only when the backend actually sends one", async () => {
  const env = await open(() => ({ ok: true, data: { ...STATUS, posture: { ...STATUS.posture, score: 88 } } }));
  assert.match(html(env, "sc-summary"), /88/);
});

test("unreported values say 'Not reported by API', never a guess", async () => {
  const env = await open();
  const g = html(env, "sc-row2");
  assert.match(g, /Agent Runtime[\s\S]*Not reported by API/);
  const bad = await open(() => ({ ok: true, data: { ...STATUS, tools: { available: false }, web3: { available: false }, web3_rows: [], credentials: { keys: null, healthy: null, failed: null, providers: null } } }));
  assert.match(html(bad, "sc-summary"), /Not reported by API/);
});

test("API failure shows a real error state instead of stale/fake data", async () => {
  const env = await open(() => ({ ok: false, error: "boom" }));
  assert.match(html(env, "sc-summary"), /boom/);
  assert.match(html(env, "sc-head"), /Unavailable/);
});

test("emergency badge follows the backend latch", async () => {
  const on = await open(() => ({ ok: true, data: { ...STATUS, posture: { ...STATUS.posture, state: "shutdown", label: "Shutdown active" }, emergency: { active: true, last_result: { subsystems: [{ subsystem: "web3", status: "stopped", detail: "d" }, { subsystem: "terminal", status: "failed", detail: "RuntimeError" }] } } } }));
  const e = html(on, "sc-emergency");
  assert.match(e, /Shutdown Active/); assert.match(e, /Release Shutdown/);
  assert.match(e, /terminal<\/b> — failed/);
  const idle = html(await open(), "sc-emergency");
  assert.match(idle, /Active/); assert.doesNotMatch(idle, /Shutdown Active/); assert.match(idle, /Shut Down All Agents/);
});

test("shutdown needs the confirmation dialog, then really POSTs {confirm:true}", async () => {
  const env = await open();
  const sc = env.win.SecurityCenter;
  sc.ask("shutdown");
  assert.match(html(env, "sc-dialog"), /This will stop all running agents, tasks, workflows and runtime operations/);
  assert.strictEqual(env.calls.filter((c) => /emergency/.test(c.url)).length, 0, "nothing sent before confirming");
  await sc.confirmDialog();
  const post = env.calls.find((c) => c.url === "/api/security/emergency-shutdown");
  assert.ok(post, "backend was called");
  assert.strictEqual(post.opts.method, "POST");
  assert.deepStrictEqual(post.opts.body, { confirm: true });
  assert.strictEqual(html(env, "sc-dialog"), "");
});

test("cancelling the dialog sends nothing", async () => {
  const env = await open(); env.win.SecurityCenter.ask("shutdown");
  env.win.SecurityCenter.state.dialog = null; env.win.SecurityCenter.paint();
  assert.strictEqual(env.calls.filter((c) => /emergency/.test(c.url)).length, 0);
});

test("a failed shutdown request surfaces the backend error", async () => {
  const env = await open((u) => (/emergency/.test(u) ? { ok: false, error: "shutdown rejected" } : { ok: true, data: STATUS }));
  env.win.SecurityCenter.ask("shutdown"); await env.win.SecurityCenter.confirmDialog();
  assert.match(html(env, "sc-emergency"), /shutdown rejected/);
});

test("import preview shows metadata, conflicts, strategy and never auto-restores", async () => {
  const env = await open();
  const sc = env.win.SecurityCenter;
  globalThis.FormData = class { constructor() { this.f = []; } append(k, v) { this.f.push([k, v]); } };
  globalThis.fetch = async () => ({ json: async () => ({ ok: true, data: { valid: true, backup: { format_version: 1, created_at: "2026-09-30 10:00:00", astra_version: "1.0.0", data_size_bytes: 2048 }, compatibility: { notes: ["Created on Windows"] }, categories: [{ id: "memory", label: "Memory", restorable: true, items: 4, conflicts: 2 }, { id: "providers", label: "Provider metadata (reference)", restorable: false, items: null, conflicts: 0 }] } }) });
  await sc.pickFile({ name: "b.zip" });
  const b = html(env, "sc-backup");
  for (const s of ["Backup found", "2.0 KB", "2 already exist", "reference only", "Keep existing", "Merge", "Replace matches", "Import &amp; Restore"]) assert.ok(b.includes(s), s);
  assert.strictEqual(env.calls.filter((c) => /restore/.test(c.url)).length, 0);
});

test("restore is sent only after confirmation, with strategy and chosen categories", async () => {
  const env = await open(); const sc = env.win.SecurityCenter; const sent = [];
  globalThis.FormData = class { constructor() { this.f = []; sent.push(this.f); } append(k, v) { this.f.push([k, v]); } };
  globalThis.fetch = async (u) => ({ json: async () => (/inspect/.test(u) ? { ok: true, data: { valid: true, backup: { format_version: 1 }, compatibility: { notes: [] }, categories: [{ id: "memory", label: "Memory", restorable: true, items: 1, conflicts: 0 }, { id: "workflows", label: "Workflows", restorable: true, items: 1, conflicts: 0 }] } } : { ok: true, data: { results: { memory: { added: 1, updated: 0, skipped: 0, renamed: 0, note: "" } }, verified: { memory: true }, safety_snapshot: "pre-import_x.zip", notes: [] } }) });
  await sc.pickFile({ name: "b.zip" });
  sc.state.imp.strategy = "merge"; sc.state.imp.skip.workflows = true;
  sc.ask("import"); await sc.confirmDialog();
  const f = Object.fromEntries(sent[sent.length - 1]);
  assert.strictEqual(f.confirm, "true"); assert.strictEqual(f.strategy, "merge"); assert.strictEqual(f.categories, "memory");
  assert.match(html(env, "sc-backup"), /Restore complete — verified/);
  assert.match(html(env, "sc-backup"), /pre-import_x\.zip/);
});

test("an invalid backup is rejected with the backend's message", async () => {
  const env = await open();
  globalThis.FormData = class { append() {} };
  globalThis.fetch = async () => ({ json: async () => ({ ok: false, error: "not an Astra backup (not a ZIP archive)" }) });
  await env.win.SecurityCenter.pickFile({ name: "x.zip" });
  assert.match(html(env, "sc-backup"), /not an Astra backup/);
  assert.doesNotMatch(html(env, "sc-backup"), /Import &amp; Restore/);
});

test("all rendered text is HTML-escaped", async () => {
  const evil = "<img src=x onerror=alert(1)>";
  const env = await open(() => ({ ok: true, data: { ...STATUS, providers: [{ name: evil, state: evil, level: "ok", keys: 1, healthy_keys: 1, kind: "provider" }] } }));
  assert.ok(!html(env, "sc-grid").includes(evil)); assert.ok(html(env, "sc-grid").includes("&lt;img"));
});

test("source embeds no credential-shaped literal and never sends an auth header of its own", () => {
  assert.doesNotMatch(SRC, /Bearer\s|sk-[A-Za-z0-9_-]{10}|ghp_[A-Za-z0-9]{10}|0x[a-fA-F0-9]{40}/);
  assert.doesNotMatch(SRC, /X-Astra-Token|Authorization/);
});
