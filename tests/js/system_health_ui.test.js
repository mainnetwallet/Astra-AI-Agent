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
  // exactly the five tabs (Logs is no longer part of System Health)
  const tabs = (doc.getElementById("sh-tabs").innerHTML.match(/data-sh-tab="/g) || []).length;
  assert.strictEqual(tabs, 5);
  assert.doesNotMatch(doc.getElementById("sh-tabs").innerHTML, /Logs|data-sh-tab="logs"/);
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
/* ---------------------------- system resources (real host telemetry) ---------------------------- */

function resPayload(at, cpu, up, down) {
  return {
    available: true, sampled_at: at,
    cpu: { percent: cpu },
    memory: { percent: 41.7, used_bytes: 4e9, total_bytes: 9.6e9, available_bytes: 5.6e9 },
    disk: { percent: 36.2, used_bytes: 1e11, total_bytes: 2.7e11, free_bytes: 1.7e11 },
    network: { bytes_sent: 1000, bytes_recv: 2000, upload_bps: up, download_bps: down },
  };
}

test("System Resources renders the backend's real CPU / memory / disk / network", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  const data = releasePayload();
  data.metrics.resources = resPayload(1000.1, 27.4, 0.6 * 1048576, 1.8 * 1048576);
  SH.render(data, []);
  const html = doc.getElementById("sh-resources-body").innerHTML;
  assert.match(html, />27%</);
  assert.match(html, />41\.7%</);
  assert.match(html, />36%</);
  assert.match(html, /\u2193 1\.8 MB\/s/);
  assert.match(html, /\u2191 614\.4 KB\/s/);
  assert.doesNotMatch(html, /Not reported by the API|Host metrics unavailable/);
  assert.match(html, /class="ln live"/);
});

test("missing or unavailable resources render an honest empty state, not numbers", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  SH.render(releasePayload(), []);           // older backend: no `resources`
  let html = doc.getElementById("sh-resources-body").innerHTML;
  assert.match(html, /Host metrics unavailable/);
  assert.doesNotMatch(html, /\d+%/);
  const data = releasePayload();
  data.metrics.resources = { available: false, cpu: null, memory: null, disk: null, network: null };
  SH.render(data, []);
  html = doc.getElementById("sh-resources-body").innerHTML;
  assert.match(html, /Host metrics unavailable/);
  assert.doesNotMatch(html, /class="ln live"/);
});

test("resource history is real, deduplicated by sample, and never exceeds 120 samples", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  const data = releasePayload();
  data.metrics.resources = resPayload(1, 10, 100, 200);
  SH.render(data, []);
  SH.render(data, []);                       // same sample re-rendered (SSE batch) -> no duplicate point
  assert.strictEqual(SH.state.resHist.cpu.length, 1);
  for (let i = 2; i <= 250; i++) {
    data.metrics.resources = resPayload(i, i % 100, i, i * 2);
    SH.render(data, []);
  }
  for (const k of ["cpu", "ram", "disk", "up", "down"]) {
    assert.ok(SH.state.resHist[k].length <= 120, k + " history exceeds 120");
  }
  assert.strictEqual(SH.state.resHist.cpu.length, 120);
  assert.strictEqual(SH.state.resHist.cpu[119], 250 % 100);
});

test("the first sample is a minimal line (no fabricated history); nothing random is used", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  const data = releasePayload();
  data.metrics.resources = resPayload(5, 20, null, null);   // first network sample has no rate yet
  SH.render(data, []);
  const html = doc.getElementById("sh-resources-body").innerHTML;
  assert.strictEqual(SH.state.resHist.cpu.length, 1);
  assert.strictEqual(SH.state.resHist.down, undefined);
  assert.match(html, /d="M68,\d+\.\d L76,\d+\.\d"/);
  assert.doesNotMatch(JS.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, ""), /Math\.random/);
});

/* ------------------------- live resource polling (one managed loop) ------------------------- */

/** Controllable clock + timers + api(): drives the REAL lifecycle code with no real waiting. */
function liveEnv(opts) {
  opts = opts || {};
  const env = { now: 1000000, timers: [], calls: [], aborted: 0, pending: [], hidden: false, listeners: [] };
  const doc = new Doc(HTML);
  doc.hidden = false;
  doc.addEventListener = (t, fn) => { if (t === "visibilitychange") env.listeners.push(fn); };
  globalThis.document = doc;
  env.doc = doc;
  env.realNow = Date.now;
  Date.now = () => env.now;
  env.setTimeout = globalThis.setTimeout; env.clearTimeout = globalThis.clearTimeout;
  let id = 0;
  globalThis.setTimeout = (fn, ms) => { const t = { id: ++id, fn, ms, live: true }; env.timers.push(t); return t.id; };
  globalThis.clearTimeout = (h) => { env.timers.forEach((t) => { if (t.id === h) t.live = false; }); };
  env.sample = 0;
  globalThis.api = (url, o) => {
    env.calls.push({ url, o });
    return new Promise((resolve) => {
      const respond = () => {
        if (env.fail) return resolve({ ok: false, error: "network: down" });
        env.sample++;
        resolve({ ok: true, data: resPayload(env.now / 1000 + env.sample, 10 + env.sample, 100 * env.sample, 200 * env.sample) });
      };
      if (o && o.signal) o.signal.addEventListener("abort", () => { env.aborted++; resolve({ ok: false, error: "network: aborted" }); });
      if (opts.manual) env.pending.push(respond); else respond();
    });
  };
  env.restore = () => {
    globalThis.setTimeout = env.setTimeout; globalThis.clearTimeout = env.clearTimeout;
    Date.now = env.realNow; delete globalThis.api;
  };
  env.live = () => env.timers.filter((t) => t.live && t.ms !== 3000);      // the poll timers (not per-request guards)
  env.fire = async () => { const t = env.live()[0]; t.live = false; env.now += t.ms; t.fn(); await flush(); };
  return env;
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

test("live: start polls once immediately and schedules exactly one ~500ms timer", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    assert.strictEqual(SH.live.start(), true);
    await flush();
    assert.strictEqual(env.calls.length, 1);
    assert.strictEqual(env.calls[0].url, "/api/system-resources");
    assert.strictEqual(env.live().length, 1);
    assert.ok(env.live()[0].ms <= 500 && env.live()[0].ms >= 0);
    assert.strictEqual(SH.state.live.last.cpu.percent, 11);
  } finally { env.restore(); }
});

test("live: calling start repeatedly never creates duplicate loops or timers", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); SH.live.start(); SH.live.start();
    await flush();
    assert.strictEqual(env.calls.length, 1);
    assert.strictEqual(env.live().length, 1);
    SH.live.start();                                   // while a timer is pending
    await flush();
    assert.strictEqual(env.calls.length, 1);
    assert.strictEqual(env.live().length, 1);
  } finally { env.restore(); }
});

test("live: each timer tick fetches once, updates values from the server sample and reschedules", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    for (let i = 0; i < 5; i++) await env.fire();
    assert.strictEqual(env.calls.length, 6);
    assert.strictEqual(env.live().length, 1);
    assert.strictEqual(SH.state.live.last.cpu.percent, 16);           // the 6th REAL server sample
    assert.deepStrictEqual(SH.state.resHist.cpu, [11, 12, 13, 14, 15, 16]);
    assert.match(env.doc.getElementById("sh-resources-body").innerHTML, />16%</);
  } finally { env.restore(); }
});

test("live: overlapping requests are prevented while one is still pending", async () => {
  const env = liveEnv({ manual: true });
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    assert.strictEqual(env.calls.length, 1);
    assert.strictEqual(await SH.live.refreshOnce(), false);           // blocked by the in-flight guard
    SH.live.start(); await flush();
    assert.strictEqual(env.calls.length, 1);
    env.pending.shift()(); await flush();
    assert.strictEqual(env.live().length, 1);                          // resumes only after it settles
  } finally { env.restore(); }
});

test("live: the request is aborted if it hangs (AbortController guard)", async () => {
  const env = liveEnv({ manual: true });
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    const guard = env.timers.find((t) => t.live && t.ms === 3000);
    assert.ok(guard, "a request timeout guard must exist");
    guard.fn(); await flush();
    assert.strictEqual(env.aborted, 1);
    assert.strictEqual(SH.state.live.fails, 1);
    assert.strictEqual(SH.state.live.inflight, null);
  } finally { env.restore(); }
});

test("live: stale detection LIVE -> DELAYED -> STALE -> OFFLINE keeps the last values and recovers", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    assert.strictEqual(SH.live.status().label, "LIVE");
    env.fail = true;
    await env.fire();                                                  // one failure: degraded, last values kept
    assert.strictEqual(SH.live.status().label, "DELAYED");
    assert.match(env.doc.getElementById("sh-resources-body").innerHTML, />11%</);
    env.now += 3000; assert.strictEqual(SH.live.status().label, "DELAYED");
    env.now += 3000; assert.strictEqual(SH.live.status().label, "STALE");
    env.now += 20000; assert.strictEqual(SH.live.status().label, "OFFLINE");
    assert.match(env.doc.getElementById("sh-resources-body").innerHTML, />11%</);   // still the last REAL value
    env.fail = false;
    await env.fire();                                                  // endpoint recovers -> LIVE at once
    assert.strictEqual(SH.live.status().label, "LIVE");
    assert.strictEqual(SH.state.live.fails, 0);
    assert.match(env.doc.getElementById("sh-res-live").innerHTML, /LIVE/);
    assert.match(env.doc.getElementById("sh-res-live").className, /healthy/);
  } finally { env.restore(); }
});

test("live: a hidden tab pauses polling and becoming visible resumes immediately, without duplicates", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    assert.strictEqual(env.live().length, 1);
    env.doc.hidden = true; SH.live.onVisibility();
    assert.strictEqual(env.live().length, 0);                          // timer cleared
    const before = env.calls.length;
    env.doc.hidden = false; SH.live.onVisibility(); await flush();
    assert.strictEqual(env.calls.length, before + 1);                  // immediate fetch on resume
    assert.strictEqual(env.live().length, 1);
    SH.live.onVisibility(); SH.live.onVisibility(); await flush();     // repeated visibility events
    assert.strictEqual(env.calls.length, before + 1);
    assert.strictEqual(env.live().length, 1);
  } finally { env.restore(); }
});

test("live: leaving System Health stops polling; opening it again restarts once", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    SH.onTab("system-map");
    assert.strictEqual(env.live().length, 0);
    const n = env.calls.length;
    await flush();
    assert.strictEqual(env.calls.length, n);
    SH.onTab("command-center");                                        // navigating TO it does not itself start a 2nd loop
    assert.strictEqual(env.live().length, 0);
    SH.live.start(); SH.live.start(); await flush();
    assert.strictEqual(env.calls.length, n + 1);
    assert.strictEqual(env.live().length, 1);
  } finally { env.restore(); }
});

test("live: a stopped loop discards a late response and never repaints stale data", async () => {
  const env = liveEnv({ manual: true });
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    SH.live.stop();
    env.pending.shift()(); await flush();
    assert.strictEqual(SH.state.live.last, null);
    assert.strictEqual(env.live().length, 0);
  } finally { env.restore(); }
});

test("live: the only thing the loop requests is /api/system-resources (no aggregate, models or providers)", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    for (let i = 0; i < 4; i++) await env.fire();
    assert.ok(env.calls.length >= 5);
    for (const c of env.calls) assert.strictEqual(c.url, "/api/system-resources");
    assert.strictEqual(SH.live.pollMs, 500);
  } finally { env.restore(); }
});

test("live: 30s aggregate renders no longer add samples once the live feed is delivering", async () => {
  const env = liveEnv();
  try {
    const SH = load(); SH.mount();
    SH.live.start(); await flush();
    const data = releasePayload();
    data.metrics.resources = resPayload(424242, 99, 1, 1);
    SH.render(data, []);
    assert.deepStrictEqual(SH.state.resHist.cpu, [11]);                // aggregate cpu=99 was NOT mixed in
  } finally { env.restore(); }
});

test("live: formatting is compact and real (bytes, 1-decimal KB/MB, memory decimals only when present)", () => {
  const doc = new Doc(HTML);
  globalThis.document = doc;
  const SH = load();
  const f = SH.norm.fmtRate, b = SH.norm.fmtBytes;
  assert.strictEqual(f(0), "0 B/s");
  assert.strictEqual(f(512), "512 B/s");
  assert.strictEqual(f(8.2 * 1024), "8.2 KB/s");
  assert.strictEqual(f(1.4 * 1048576), "1.4 MB/s");
  assert.strictEqual(f(12.7 * 1048576), "12.7 MB/s");
  assert.strictEqual(f(null), "\u2014");
  assert.strictEqual(b(1023), "1023 B");
  const data = releasePayload();
  data.metrics.resources = resPayload(1, 0.4, 0, 0);
  data.metrics.resources.memory.percent = 31.0;
  SH.render(data, []);
  let html = doc.getElementById("sh-resources-body").innerHTML;
  assert.match(html, />0%</);
  assert.match(html, />31%</);
  assert.match(html, /\u2193 0 B\/s/);                                  // a measured zero is shown as a zero
  data.metrics.resources = resPayload(2, 87.2, 0, 0);
  data.metrics.resources.memory.percent = 30.4;
  SH.render(data, []);
  html = doc.getElementById("sh-resources-body").innerHTML;
  assert.match(html, />87%</);
  assert.match(html, />30\.4%</);
});
