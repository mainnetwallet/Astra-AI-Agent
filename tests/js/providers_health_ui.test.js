/* Integration test for the unified "Providers Health Test" page.
 *
 * Drives the REAL static/js/astra.js (the provider + Astra AI Gateway render
 * layer) and static/js/providers_health.js (the page chrome: overall health,
 * summary cards, filters/search and the Live Test Activity rail) over the
 * shared DOM shim. No browser is needed: the page reaches the DOM only through
 * getElementById / innerHTML / classList / addEventListener and the global
 * $ / $$ document helpers, which this test supplies over the shim tree.
 *
 * The whole point of the page is that it shows ONLY real data, so this asserts
 * that the summary is derived from the real /api/providers payload, that every
 * reference-image number is absent, and that an API key value never reaches
 * the DOM.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { Doc, HTML } = require("./dom_shim.js");

const ROOT = path.join(__dirname, "..", "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const PH_SRC = read("static/js/providers_health.js");
const ASTRA_SRC = read("static/js/astra.js");

/* ---------------------- a tiny selector engine ----------------------------
 * The shim stores innerHTML as text and builds no child tree, so the filter
 * tests build real El rows (appendChild) and need the same descendant
 * selectors the page uses. Supports #id, .class, tag, [attr], [attr="v"]
 * compounds and the descendant combinator. */
function parseSimple(sel) {
  const out = { tag: "", id: "", classes: [], attrs: [] };
  const re = /([#.]?[A-Za-z0-9_-]+)|(\[[^\]]+\])/g;
  let m;
  while ((m = re.exec(sel))) {
    if (m[2]) {
      const body = m[2].slice(1, -1);
      const eq = body.indexOf("=");
      if (eq < 0) out.attrs.push([body.trim(), null]);
      else out.attrs.push([body.slice(0, eq).trim(),
                           body.slice(eq + 1).trim().replace(/^["']|["']$/g, "")]);
    } else {
      const tok = m[1];
      if (tok[0] === "#") out.id = tok.slice(1);
      else if (tok[0] === ".") out.classes.push(tok.slice(1));
      else out.tag = tok.toUpperCase();
    }
  }
  return out;
}
function camel(name) { return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
function matchSimple(el, s) {
  if (s.tag && String(el.tagName || "").toUpperCase() !== s.tag) return false;
  if (s.id && el.id !== s.id) return false;
  if (!s.classes.every((c) => el.classList.contains(c))) return false;
  return s.attrs.every(([k, v]) => {
    const got = k.indexOf("data-") === 0 ? el.dataset[camel(k.slice(5))] : el.getAttribute(k);
    return v === null ? got !== undefined && got !== null : String(got) === v;
  });
}
function matchesChain(el, parts) {
  if (!matchSimple(el, parts[parts.length - 1])) return false;
  let i = parts.length - 2, node = el.parentElement;
  while (i >= 0 && node) {
    if (matchSimple(node, parts[i])) i -= 1;
    node = node.parentElement;
  }
  return i < 0;
}
function collect(node, out) {
  for (const c of node.children || []) { out.push(c); collect(c, out); }
  return out;
}
function rootNode(root) { return root && root.root ? root.root : root; }
function queryAll(root, selector) {
  const nodes = collect(rootNode(root), []);
  const groups = String(selector).split(",").map((g) => g.trim()).filter(Boolean)
    .map((g) => g.split(/\s+/).map(parseSimple));
  return nodes.filter((n) => groups.some((parts) => matchesChain(n, parts)));
}

/* --------------------------- real payload -------------------------------- */
const FLASH = "gemini-2.5-flash";
function providersPayload() {
  return {
    ok: true,
    data: {
      providers: {
        "Google Gemini": {
          state: "healthy", healthy: true,
          models: ["gemini-2.5-flash", "gemini-2.5-pro"],
          latency_avg_ms: 420, calls: 12, errors: 0,
          keys: [{ key_id: "k1", label: "key 1", healthy: true },
                 { key_id: "k2", label: "key 2", healthy: true }],
          key_results: {
            "gemini-2.5-flash": { k1: { key_label: "key 1", ok: true,
                latency_ms: 410, tested_at: "2026-09-30 12:01:00" } },
          },
        },
        "OpenRouter": {
          state: "rate_limited", healthy: false, models: ["gpt-4o-mini"],
          latency_avg_ms: null, calls: 0, errors: 3,
          keys: [{ key_id: "k3", label: "key 1", healthy: false, in_cooldown: true }],
          key_results: {},
        },
      },
      astra_ai_gateway: {
        state: "healthy",
        connections: {
          "astra-gw-groq": {
            state: "healthy", models: ["llama-3.1-70b"],
            keys: [{ key_id: "g1", label: "key 1", healthy: true }],
            model_health: {
              "llama-3.1-70b": { success_count: 1, failure_count: 0,
                last_success: "2026-09-30 12:02:00", last_failure: null },
            },
          },
        },
      },
    },
  };
}

function makeEnvironment() {
  const doc = new Doc(HTML);
  doc.readyState = "complete";
  const payload = providersPayload();
  const calls = [];

  const respond = (method, url) => {
    calls.push({ method, url });
    const p = String(url).split("?")[0];
    if (p === "/api/providers") return payload;
    if (p === "/api/manifest") return { ok: true, data: { name: "Astra", tabs: [{ tab: "providers", label: "Providers" }] } };
    if (p === "/api/dashboard") return { ok: true, data: [] };
    if (p === "/api/events") return { ok: true, data: [] };
    return { ok: true, data: {} };
  };

  // attach every scanned id to the document tree so descendant selectors work
  for (const el of doc._els.values()) doc.root.appendChild(el);
  // the shim does not model <select> children; renderKeyFilter reads .options
  const keySel = doc.getElementById("ph-key-filter");
  if (keySel) keySel.options = [];
  doc.root.appendChild(doc.getElementById("providers-list"));
  doc.root.appendChild(doc.getElementById("gateway-card"));

  globalThis.$ = (s, r = doc) => {
    s = String(s);
    if (s[0] === "#" && !/[\s.\[]/.test(s)) return doc.getElementById(s.slice(1));
    return queryAll(r || doc, s)[0] || null;
  };
  globalThis.$$ = (s, r = doc) => {
    s = String(s);
    if (s[0] === "#" && !/[\s.\[]/.test(s)) {
      const e = doc.getElementById(s.slice(1));
      return e ? [e] : [];
    }
    return queryAll(r || doc, s);
  };

  globalThis.window = {
    addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({ matches: false }),
    confirm: () => true,
    Astra: { loaders: {} },
  };
  globalThis.document = doc;
  globalThis.CSS = { escape: (s) => String(s) };
  globalThis.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } };
  globalThis.localStorage = {
    _m: {},
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; },
    setItem(k, v) { this._m[k] = String(v); },
    removeItem(k) { delete this._m[k]; },
  };
  globalThis.esc = (s) => String(s === undefined || s === null ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                                   '"': "&quot;", "'": "&#39;" }[c]));
  globalThis.fetch = async (url, opts) =>
    ({ json: async () => respond((opts && opts.method) || "GET", url) });
  globalThis.api = async (url, opts = {}) => await (await globalThis.fetch(url, opts)).json();
  globalThis.post = (url, body) => globalThis.api(url, { method: "POST", body });
  globalThis.del = (url) => globalThis.api(url, { method: "DELETE" });

  class EventSourceShim {
    constructor(url) { this.url = url; this.onmessage = null; this.onerror = null; }
    close() {}
  }
  globalThis.EventSource = EventSourceShim;
  globalThis.window.EventSource = EventSourceShim;

  globalThis.AstraLog = require("../../static/js/log_model.js");
  globalThis.AstraChatStatus = require("../../static/js/chat_status.js");

  globalThis.setInterval = () => 0;
  delete require.cache[require.resolve("../../static/js/astra.js")];
  require("../../static/js/astra.js");
  globalThis.Astra = globalThis.window.Astra;
  globalThis.loaders = globalThis.window.Astra.loaders;

  delete require.cache[require.resolve("../../static/js/providers_health.js")];
  require("../../static/js/providers_health.js");
  // setInterval stays stubbed: astra.js installs a 60s dashboard refresh in
  // boot(), which would otherwise hold the node event loop open forever.

  return { doc, calls, PH: globalThis.window.ProvidersHealth, payload };
}

/* ------------------------------- tests ----------------------------------- */

test("index.html links the page assets after astra.js and keeps the tab id", () => {
  assert.match(HTML, /\/static\/css\/providers_health\.css/);
  const at = (f) => HTML.indexOf('src="/static/js/' + f + '"');
  assert.ok(at("providers_health.js") > -1, "the page script must be linked");
  assert.ok(at("astra.js") < at("providers_health.js"));
  assert.ok(at("astra_os.js") < at("providers_health.js"));
  assert.match(HTML, /id="tab-providers" class="tabview"/);
  assert.match(HTML, /id="providers-list"/);
  assert.match(HTML, /id="gateway-card"/);
});

test("the page owns ONE data feed and ONE test engine (no second SSE / timer)", () => {
  assert.strictEqual(PH_SRC.indexOf("new EventSource"), -1);
  assert.strictEqual(/(^|[^.\w])setInterval\s*\(/.test(PH_SRC), false);
  assert.strictEqual(PH_SRC.indexOf("new WebSocket"), -1);
  // Test All is forwarded to the existing operation, never re-implemented
  assert.match(PH_SRC, /api\(\)\.testAll/);
  // both entry points reach the same button astra.js owns
  const at = (f) => HTML.indexOf('src="/static/js/' + f + '"');
  assert.match(HTML, /id="ph-test-all"/);
  assert.match(HTML, /id="btn-providers-test-all"/);
  assert.match(ASTRA_SRC, /testAllBtn\.onclick/);
  // refresh/recovery stays in astra.js (the page must not remove it)
  assert.match(ASTRA_SRC, /_maybeResumeRuns/);
  assert.match(ASTRA_SRC, /_loadRunning/);
  assert.match(ASTRA_SRC, /RESTORED_PROVIDER_PENDING/);
  void at;
});

test("the summary is computed from the real payload, never invented", async () => {
  const env = makeEnvironment();
  await globalThis.loaders.providers();
  const s = env.PH.summary();
  assert.strictEqual(s.providers, 2, "two direct providers in the payload");
  assert.strictEqual(s.connections, 1, "one Gateway connection");
  assert.strictEqual(s.models, 4, "four distinct models across both systems");
  assert.strictEqual(s.keys, 4, "two + one provider keys plus one Gateway key");
  assert.strictEqual(s.healthy, 2, "gemini-2.5-flash + llama-3.1-70b probed healthy");
  assert.strictEqual(s.failed, 0);
  assert.ok(s.lastTest instanceof Date);
});

test("the summary cards paint real values and none of the reference numbers", async () => {
  const env = makeEnvironment();
  await globalThis.loaders.providers();
  const html = String(env.doc.getElementById("ph-metrics").innerHTML);
  for (const label of ["AI Providers", "Gateway Connections", "Total Models",
                       "Total Keys", "Healthy Models", "Failed Models", "Last Test"]) {
    assert.ok(html.indexOf(label) >= 0, "missing summary card: " + label);
  }
  assert.strictEqual(env.doc.getElementById("ph-overall-note").textContent, "2 / 4 models healthy");
  assert.strictEqual(env.doc.getElementById("ph-pct").textContent, "50%");
  // the screenshot's illustrative figures must never be hardcoded/painted
  assert.ok(!/98%/.test(html), "reference-image 98% leaked");
  assert.ok(!/\b74\b/.test(html), "reference-image 74 leaked");
  assert.ok(!/\b19\b/.test(html), "reference-image 19 leaked");
  assert.ok(!/72 \/ 74/.test(html));
});

test("providers and Gateway render on ONE page with real rows and Test buttons", async () => {
  const env = makeEnvironment();
  await globalThis.loaders.providers();
  const ph = String(env.doc.getElementById("providers-list").innerHTML);
  const gw = String(env.doc.getElementById("gateway-card").innerHTML);
  assert.match(ph, /data-ph-kind="provider"/);
  assert.match(ph, /Google Gemini/);
  assert.match(ph, /OpenRouter/);
  assert.match(ph, /data-role="provider-test"/);
  assert.match(ph, /class="ph-caret"/);
  assert.match(gw, /data-ph-kind="gateway"/);
  assert.match(gw, /data-gw-conn="astra-gw-groq"/);
  assert.match(gw, /data-role="gw-test"/);
});

test("the activity rail follows the real test lifecycle events", () => {
  const env = makeEnvironment();
  const feed = env.doc.getElementById("ph-activity-feed");
  env.PH.onTestEvent({ detail: { phase: "start", kind: "provider", owner: "Groq", model: "llama-3.1-70b" } });
  assert.match(String(feed.innerHTML), /Testing\.\.\./);
  env.PH.onTestEvent({ detail: { phase: "done", kind: "provider", owner: "Groq",
                                 model: "llama-3.1-70b", ok: true, latency_ms: 1200 } });
  assert.match(String(feed.innerHTML), /Test successful/);
  assert.match(String(feed.innerHTML), /1\.2s/);
  env.PH.onTestEvent({ detail: { phase: "done", kind: "gateway", owner: "astra-gw-groq",
                                 model: "mistral-large", ok: false, error: "429 rate limit" } });
  assert.match(String(feed.innerHTML), /Test failed . 429 rate limit/);
});

function makeRow(doc, kind, name, status, opts) {
  opts = opts || {};
  const el = doc.createElement("div");
  el.tagName = "DIV";
  el.className = "provider-card";
  el.dataset.phKind = kind;
  el.dataset.phName = name;
  el.dataset.phStatus = status;
  el.dataset.phRunning = opts.running ? "1" : "0";
  el.dataset.phModels = (opts.models || []).join(" ");
  el.dataset.phKeyLabels = (opts.keyLabels || []).join("|");
  el.dataset.phCount = String((opts.models || []).length);
  el.dataset.phKeys = String((opts.keyLabels || []).length);
  return el;
}

test("view / status / search / key filters operate on the rendered rows", () => {
  const env = makeEnvironment();
  const doc = env.doc, PH = env.PH;
  const provList = doc.getElementById("providers-list");
  const gwList = doc.getElementById("gateway-card");
  provList.innerHTML = ""; gwList.innerHTML = "";
  const gemini = makeRow(doc, "provider", "Google Gemini", "healthy",
    { models: ["gemini-2.5-flash", "gemini-2.5-pro"], keyLabels: ["key 1", "key 2"] });
  const openrouter = makeRow(doc, "provider", "OpenRouter", "failed",
    { models: ["gpt-4o-mini"], keyLabels: ["key 1"], running: true });
  const groove = makeRow(doc, "gateway", "Groq", "healthy",
    { models: ["llama-3.1-70b"], keyLabels: ["key 1"] });
  [gemini, openrouter].forEach((r) => provList.appendChild(r));
  gwList.appendChild(groove);

  PH.state.view = "providers"; PH.state.status = "all"; PH.state.search = ""; PH.state.key = "";
  PH.applyFilters();
  assert.strictEqual(gemini.classList.contains("ph-filtered"), false);
  assert.strictEqual(openrouter.classList.contains("ph-filtered"), false);
  assert.strictEqual(groove.classList.contains("ph-filtered"), true, "gateway hidden in providers view");

  PH.state.view = "gateway"; PH.applyFilters();
  assert.strictEqual(gemini.classList.contains("ph-filtered"), true);
  assert.strictEqual(groove.classList.contains("ph-filtered"), false);

  PH.state.view = "all"; PH.state.status = "healthy"; PH.applyFilters();
  assert.strictEqual(gemini.classList.contains("ph-filtered"), false);
  assert.strictEqual(openrouter.classList.contains("ph-filtered"), true, "failed hidden by healthy filter");

  PH.state.status = "running"; PH.applyFilters();
  assert.strictEqual(openrouter.classList.contains("ph-filtered"), false, "only the running row shows");

  PH.state.status = "all"; PH.state.search = "gemini"; PH.applyFilters();
  assert.strictEqual(gemini.classList.contains("ph-filtered"), false);
  assert.strictEqual(openrouter.classList.contains("ph-filtered"), true, "search excludes non-matches");

  PH.state.search = ""; PH.state.key = "key 2"; PH.applyFilters();
  assert.strictEqual(gemini.classList.contains("ph-filtered"), false, "gemini holds key 2");
  assert.strictEqual(groove.classList.contains("ph-filtered"), true, "groq only has key 1");
});

test("secrets never reach the published state or the rendered DOM", async () => {
  const env = makeEnvironment();
  await globalThis.loaders.providers();
  const dump = JSON.stringify(globalThis.window.AstraProviders.providers)
    + JSON.stringify(globalThis.window.AstraProviders.gateway);
  assert.ok(!/"(api_?key|secret|token|password|authorization)"/i.test(dump),
            "a credential value field leaked into AstraProviders");
  for (const el of ["providers-list", "gateway-card", "ph-metrics"]) {
    const html = String(env.doc.getElementById(el).innerHTML);
    assert.ok(!/Bearer |sk-|AIza/.test(html), "a raw secret shape leaked into " + el);
  }
});

test("a refresh keeps working and never fabricates a completed state", async () => {
  const env = makeEnvironment();
  await globalThis.loaders.providers();
  await globalThis.loaders.providers();
  assert.strictEqual(env.PH.summary().healthy, 2);
  assert.match(String(env.doc.getElementById("providers-list").innerHTML), /Google Gemini/);
  assert.ok(env.calls.filter((c) => c.url === "/api/providers").length >= 2);
});
