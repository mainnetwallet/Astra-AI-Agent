/* Integration test for the ASTRA System Health page (static/js/system_health.js).
 *
 * No browser is needed: the module only touches the DOM through
 * getElementById / innerHTML / classList / addEventListener, so the shared
 * shim (tests/js/dom_shim.js) drives the REAL normalise + paint code paths.
 *
 * The payload shapes mirror the REAL endpoints the page reads:
 *   GET /api/health      -> {ok, checks:{...}}
 *   GET /api/providers   -> {providers:{NAME:{state,healthy,models,latency_avg_ms,
 *                            calls,errors,credentials,keys:[{key_id,label,healthy}],
 *                            key_results:{MODEL:{KEYID:{key_label,ok,latency_ms,tested_at}}}}}}
 *   GET /api/models      -> {models:[{provider,model,capabilities:[],disabled,...}]}
 *   GET /api/metrics     -> {uptime_s, requests:{count,errors}}
 *
 * The whole point of the page is that it shows ONLY real data, so these tests
 * assert that: nothing is invented, every unreported metric becomes an em dash,
 * and a real API key never reaches the rendered page.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { Doc, HTML } = require("./dom_shim.js");

const ROOT = path.join(__dirname, "..", "..");
const JS = fs.readFileSync(path.join(ROOT, "static/js/system_health.js"), "utf8");

/* A real-shaped /api/providers payload: two providers, one with per-model key
 * results and one whose credential has been rejected. */
function providersPayload() {
  return {
    providers: {
      "Google Gemini": {
        state: "healthy", healthy: true, base_url: "https://example.invalid",
        models: ["gemini-2.5-flash", "gemini-2.5-pro"], latency_avg_ms: 420,
        calls: 1000, errors: 1, cost_usd: 0, credentials: 2,
        keys: [{ key_id: "k1", label: "key 1", healthy: true, in_cooldown: false,
                 calls: 999, errors: 1, last_error: null }],
        key_results: {
          "gemini-2.5-flash": { k1: { key_label: "key 1", ok: true, latency_ms: 420,
                                      tested_at: "2026-09-29 21:17:12" } },
          "gemini-1.5-flash": { k1: { key_label: "key 1", ok: false, latency_ms: null,
                                      tested_at: "2026-09-29 21:15:33" } },
        },
      },
      "OpenRouter": {
        state: "rate_limited", healthy: false, models: ["gpt-4o-mini"],
        latency_avg_ms: null, calls: 0, errors: 0, credentials: 1,
        keys: [{ key_id: "k2", label: "key 1", healthy: false, in_cooldown: true,
                 calls: 0, errors: 0, last_error: "429" }],
        key_results: {},
      },
    },
  };
}

function modelsPayload() {
  return {
    models: [
      { provider: "Google Gemini", model: "gemini-2.5-flash", capabilities: ["chat", "vision", "tools"] },
      { provider: "Google Gemini", model: "gemini-2.5-pro", capabilities: ["chat"], supports_json: true },
      { provider: "Google Gemini", model: "gemini-1.5-flash", capabilities: ["chat", "vision"], disabled: true },
      { provider: "OpenRouter", model: "gpt-4o-mini", capabilities: ["chat"] },
    ],
  };
}

function releasePayload() {
  return {
    health: { ok: true, checks: { database: "ok" } },
    providerCards: [
      { name: "Google Gemini", status: "online", state: "healthy",
        modelCount: 2, successRate: 99.9, latencyMs: 420, keyCount: 2 },
      { name: "OpenRouter", status: "rate_limited", state: "rate_limited",
        modelCount: 1, successRate: null, latencyMs: null, keyCount: 1 },
    ],
    providersRaw: providersPayload(),
    metrics: { uptime_s: 90061, requests: { count: 500, errors: 2 } },
    tools: { total: 59 },
    agents: [{ id: "a1" }, { id: "a2" }],
    tasks: [{ status: "running" }, { status: "pending" }],
    eventsOk: true,
    gateway: { state: "healthy" },
    router: {},
    web3: { mode: "prepare", stopped: false },
    runtime: { available: true, state: "stopped" },
  };
}

function load() {
  globalThis.window = globalThis.window || {};
  globalThis.window.SystemMapModel = require("../../static/js/system_map_model.js");
  globalThis.window.AstraOS = { data: {}, events: [], poll: { enabled: true, intervalMs: 30000 }, hooks: {} };
  const entry = require.resolve("../../static/js/system_health.js");
  delete require.cache[entry];
  require(entry);
  return globalThis.window.SystemHealth;
}

/* --------------------------------------------------------------- tests -- */

test("index.html wires the stylesheet and the script after its dependencies", () => {
  assert.match(HTML, /\/static\/css\/system_health\.css/);
  const at = (f) => HTML.indexOf('src="/static/js/' + f + '"');
  assert.ok(at("system_health.js") > -1, "the System Health script must be included");
  assert.ok(at("system_map_model.js") < at("system_health.js"));
  assert.ok(at("astra_os.js") < at("system_health.js"));
});

test("the page keeps ownership of the command-center tabview (the /command-center route)", () => {
  assert.match(HTML, /id="tab-command-center" class="tabview"/);
  const os = fs.readFileSync(path.join(ROOT, "static/js/astra_os.js"), "utf8");
  assert.match(os, /"\/command-center": "command-center"/);
  assert.match(os, /Astra\.loaders\["command-center"\]/);
});

test("no second data feed and no second timer: it reuses the shared poll", () => {
  assert.strictEqual(JS.indexOf("new EventSource"), -1);
  // no timer of its own: it drives astra_os.js's ONE poll through the hooks
  assert.strictEqual(/(^|[^.\w])setInterval\s*\(/.test(JS), false);
  assert.match(JS, /OS\.hooks\.setAuto/);
  assert.match(JS, /OS\.hooks\.refresh/);
  assert.match(JS, /OS\.hooks\.setInterval/);
});

test("services normalise subsystem health and never invent uptime/latency", () => {
  const SH = load();
  const rows = SH.norm.services(releasePayload());
  const api = rows.find((r) => r.name === "API");
  assert.ok(api, "the API subsystem row must be present");
  assert.strictEqual(api.status, "online");        // "healthy" -> "online"
  assert.strictEqual(api.uptime, null);            // /api/health reports no per-service uptime
  assert.strictEqual(api.response, null);
  const providers = rows.find((r) => r.name === "Providers");
  assert.strictEqual(providers.detail, "1/2 online");
});

test("providers mirror the real provider cards; unknown metrics stay null", () => {
  const SH = load();
  const rows = SH.norm.providers(releasePayload());
  const gem = rows.find((r) => r.name === "Google Gemini");
  assert.strictEqual(gem.status, "online");
  assert.strictEqual(gem.successRate, 99.9);
  const or = rows.find((r) => r.name === "OpenRouter");
  assert.strictEqual(or.status, "rate_limited");
  assert.strictEqual(or.successRate, null);        // backend reported no calls -> no rate
  assert.strictEqual(or.latencyMs, null);
});

test("the model table joins /api/models with per-model key health from /api/providers", () => {
  const SH = load();
  SH.state.models.payload = modelsPayload();
  const rows = SH.norm.models(releasePayload());
  assert.strictEqual(rows.length, 4, "every registry model becomes exactly one row");
  const flash = rows.find((r) => r.model === "gemini-2.5-flash");
  assert.strictEqual(flash.provider, "Google Gemini");
  assert.strictEqual(flash.keyTag, "key 1");       // the backend's own non-secret label
  assert.strictEqual(flash.status, "healthy");
  assert.strictEqual(flash.latencyMs, 420);
  assert.deepStrictEqual(flash.capabilities, ["Text", "Vision", "Tools"]);

  const dead = rows.find((r) => r.model === "gemini-1.5-flash");
  assert.strictEqual(dead.status, "unavailable");  // registry says disabled
  assert.strictEqual(dead.keyOk, false);           // the key result says it failed

  const pro = rows.find((r) => r.model === "gemini-2.5-pro");
  assert.deepStrictEqual(pro.capabilities, ["Text", "JSON"]); // supports_json flag -> badge
});

test("a model with no key health reports an em dash, never a fabricated 0%", () => {
  const SH = load();
  SH.state.models.payload = modelsPayload();
  const rows = SH.norm.models(releasePayload());
  const or = rows.find((r) => r.model === "gpt-4o-mini");
  assert.strictEqual(or.keyTag, "key 1");
  assert.strictEqual(or.latencyMs, null);
  assert.strictEqual(or.status, "rate_limited");   // falls back to the provider card status
});

test("with no registry payload the model list is empty and nothing is invented", () => {
  const SH = load();
  SH.state.models.payload = null;
  assert.deepStrictEqual(SH.norm.models(releasePayload()), []);
});

test("key counters read only the secret-free keys[] metadata", () => {
  const SH = load();
  const ks = SH.norm.keyStats({ providersRaw: providersPayload() });
  assert.strictEqual(ks.total, 2);
  assert.strictEqual(ks.healthy, 1);
  assert.strictEqual(ks.providersWithKeys, 2);
});

test("the six KPI cards carry real values, or an em dash when unreported", () => {
  const SH = load();
  const kpis = SH.norm.sixKpis(releasePayload());
  assert.strictEqual(kpis.length, 6);
  const by = Object.fromEntries(kpis.map((k) => [k.id, k]));
  assert.strictEqual(by.system.value, "Healthy");
  assert.strictEqual(by.uptime.value, "1d 1h");          // 90061s
  assert.strictEqual(by.latency.value, "420 ms");
  assert.strictEqual(by.agents.value, "0 / 2");         // 0 running of 2 registered (empty feed)
  assert.strictEqual(by.tasks.value, "1");
  assert.strictEqual(by.errors.value, "0.4%");          // 2/500
});

test("unreported headline metrics render an em dash, not a fake number", () => {
  const SH = load();
  const kpis = SH.norm.sixKpis({});                     // nothing available at all
  const by = Object.fromEntries(kpis.map((k) => [k.id, k]));
  assert.strictEqual(by.system.value, "Not reported");
  assert.strictEqual(by.uptime.value, "\u2014");
  assert.strictEqual(by.latency.value, "\u2014");
  assert.strictEqual(by.errors.value, "\u2014");
});

test("the health summary row counts only what the backend reports", () => {
  const SH = load();
  const sum = SH.norm.sixSummary(releasePayload());
  const by = Object.fromEntries(sum.map((c) => [c.id, c]));
  assert.strictEqual(by.providers.value, "1 / 2");
  assert.strictEqual(by.degraded.value, "1");           // the rate-limited provider
  assert.strictEqual(by.failed.value, "0");
  // API keys come from the raw keys[] counts, never a fabricated total
  assert.strictEqual(by.keys.value, "1 / 2");
});

test("search + filters run over the loaded rows only", () => {
  const SH = load();
  SH.state.models.payload = modelsPayload();
  const rows = SH.norm.models(releasePayload());
  const find = (f) => SH.norm.visibleModels(rows, f).map((r) => r.model);
  assert.deepStrictEqual(find({ fProvider: "Google Gemini" }).sort(),
    ["gemini-1.5-flash", "gemini-2.5-flash", "gemini-2.5-pro"]);
  assert.deepStrictEqual(find({ q: "pro" }), ["gemini-2.5-pro"]);
  assert.deepStrictEqual(find({ fProvider: "Google Gemini", q: "flash" }).sort(),
    ["gemini-1.5-flash", "gemini-2.5-flash"]);
  assert.deepStrictEqual(find({ q: "nothing-here" }), []);
});

test("status labels use the documented vocabulary", () => {
  const SH = load();
  assert.strictEqual(SH.norm.statusLabel("healthy"), "Healthy");
  assert.strictEqual(SH.norm.statusLabel("degraded"), "Degraded");
  assert.strictEqual(SH.norm.statusLabel("offline"), "Failed");
  assert.strictEqual(SH.norm.statusLabel("rate_limited"), "Rate Limited");
  assert.strictEqual(SH.norm.statusLabel(""), "Not reported");
});

test("mount paints the real page shell into the command-center tabview", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  SH.mount();
  SH.paint();
  const host = doc.getElementById("tab-command-center");
  assert.match(host.innerHTML, /id="sh-root"/);
  assert.match(host.innerHTML, /ASTRA System Health/);
  // exactly the six tabs the design calls for
  const tabs = (doc.getElementById("sh-tabs").innerHTML.match(/data-sh-tab="/g) || []).length;
  assert.strictEqual(tabs, 6);
});

test("mounted paint renders six KPI cards and six summary cards from real data", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  SH.state.models.payload = modelsPayload();
  SH.render(releasePayload(), []);
  const kpis = doc.getElementById("sh-kpis").innerHTML;
  const sum = doc.getElementById("sh-summary").innerHTML;
  assert.strictEqual((kpis.match(/class="sh-panel sh-kpi"/g) || []).length, 6);
  assert.strictEqual((sum.match(/class="sh-panel sh-sum"/g) || []).length, 6);
  // the model rows are painted with the four registry models
  const body = doc.getElementById("sh-models-body").innerHTML;
  assert.strictEqual((body.match(/data-sh-row=/g) || []).length, 4);
});

test("the rendered page never contains a real credential value", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  SH.state.models.payload = modelsPayload();
  SH.render(releasePayload(), []);
  const html = ["sh-kpis", "sh-summary", "sh-services-body", "sh-providers-body",
    "sh-models-body"].map((id) => doc.getElementById(id).innerHTML).join("");
  // the payload's only key material is the backend's non-secret label
  assert.match(html, /key 1/);
  for (const leak of ["api_key", "apiKey", "sk-", "AIza", "Bearer "]) {
    assert.strictEqual(html.indexOf(leak), -1, "secret-shaped value leaked: " + leak);
  }
});

test("no provider name or model id is hardcoded in the module", () => {
  const code = JS.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  for (const name of ["gemini", "openai", "anthropic", "cloudflare", "openrouter",
    "huggingface", "mistral", "groq"]) {
    assert.strictEqual(code.toLowerCase().indexOf(name), -1,
      "hardcoded provider name: " + name);
  }
});

test("the icons use Astra glyph classes, not emoji", () => {
  assert.match(JS, /"ic-service"/);
  assert.match(JS, /sh-ic /);
  const code = JS.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  const emoji = /[\u{1F300}-\u{1FAFF}\u{FE0F}]/u;
  assert.strictEqual(emoji.test(code), false, "System Health must not use emoji icons");
});