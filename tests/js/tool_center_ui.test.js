/* Integration test for the 🔧 Tool Center tab (static/js/tool_center.js).
 *
 * No browser is needed: the module only touches the DOM through
 * getElementById / innerHTML / classList / addEventListener, so the shared
 * shim (tests/js/dom_shim.js) drives the REAL normalise + filter + detail
 * render code paths. The payload shape mirrors the REAL GET /api/tools
 * response (astra/web.py -> ToolRegistry.list() + stats()).
 *
 * The whole point of the page is that it shows ONLY real registry data — so
 * these tests feed real-shaped payloads and assert that nothing is invented
 * (no fake status, no fabrication, no "run tool" button the backend cannot
 * support) and that secrets never leak through.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { Doc, HTML } = require("./dom_shim.js");

const ROOT = path.join(__dirname, "..", "..");

/* Shape of one tool as Tool.describe() returns it (astra/tools/schemas.py). */
function tool(name, over) {
  return Object.assign({
    name, description: name + " description", category: "browser",
    input_schema: {}, output_schema: {}, risk_level: "read",
    requires_confirmation: false, confirmation_delegate: "", timeout_s: 0.0,
    retries: 0, retry_backoff_s: 1.0, idempotent: false, supports_async: false,
    rate_limit_per_min: 0, strict: false, plugin: "core", agent_forbidden: false,
  }, over || {});
}

const PAYLOAD = {
  tools: [
    tool("browser_open", { description: "Open a URL in a real browser session.", category: "browser",
      risk_level: "low_risk_write", input_schema: { type: "object", properties: { url: { type: "string" } } } }),
    tool("read_file", { description: "Read content from a file.", category: "files" }),
    tool("terminal_exec", { description: "Execute a shell command on the host.", category: "terminal",
      risk_level: "system_action", agent_forbidden: true, input_schema: { type: "object", properties: { cmd: { type: "string" } } } }),
    tool("tx_prepare", { description: "PREPARE a transaction. Never signs.", category: "web3",
      risk_level: "financial_action", requires_confirmation: true, confirmation_delegate: "web3_tx" }),
    tool("my_plugin_tool", { description: "A third-party tool.", category: "misc", plugin: "user" }),
  ],
  stats: { browser_open: { calls: 4, errors: 1, average_duration: 250.5, last_called: "2026-09-29 18:00:00" } },
};

function makeEnvironment(respond) {
  const doc = new Doc(HTML);
  const calls = [];
  const answer = respond || (() => ({ ok: true, data: PAYLOAD }));

  globalThis.window = {
    addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({ matches: false }),
    Astra: { loaders: {} },
  };
  globalThis.document = doc;
  globalThis.esc = (s) => String(s == null ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  globalThis.api = async (url) => { calls.push(url); return answer(url); };

  const model = require("../../static/js/system_map_model.js");
  window.SystemMapModel = model;

  const entry = require.resolve("../../static/js/tool_center.js");
  delete require.cache[entry];
  require(entry);
  return { doc, calls, win: globalThis.window };
}

async function booted(respond) {
  const env = makeEnvironment(respond);
  assert.strictEqual(typeof env.win.Astra.loaders["tool-center"], "function",
    "Tool Center must register a tab loader into Astra.loaders");
  await env.win.Astra.loaders["tool-center"]();
  return env;
}

function click(root, sel, el) {
  return root.fire("click", { target: { closest: (s) => (s === sel ? (el || { dataset: {} }) : null) } });
}

/* ---------------------------------------------------------------- tests -- */

test("index.html wires the tab, the stylesheet and the script", () => {
  assert.match(HTML, /id="tab-tool-center"/);
  assert.match(HTML, /\/static\/css\/tool_center\.css/);
  // the script tag order matters: system_map_model.js (grouping) and
  // astra_os.js (loader registry) must load before tool_center.js
  const at = (f) => HTML.indexOf('src="/static/js/' + f + '"');
  assert.ok(at("tool_center.js") > -1, "the Tool Center script must be included");
  assert.ok(at("system_map_model.js") < at("tool_center.js"));
  assert.ok(at("astra_os.js") < at("tool_center.js"));
});

test("tools come only from the real /api/tools payload, grouped by the shared model", async () => {
  const env = await booted();
  assert.deepStrictEqual(env.calls, ["/api/tools"], "the ONE data source is /api/tools");
  const rows = env.win.ToolCenter.state.rows;
  assert.strictEqual(rows.length, 5);
  const byName = Object.fromEntries(rows.map((r) => [r.name, r]));
  // categories reuse system_map_model.toolGroupOf — same labels as the System Map
  assert.strictEqual(byName.browser_open.group, "Browser");
  assert.strictEqual(byName.read_file.group, "File");
  assert.strictEqual(byName.terminal_exec.group, "Terminal");
  assert.strictEqual(byName.tx_prepare.group, "Web3");
  assert.strictEqual(byName.my_plugin_tool.group, "Custom");
  // real safety metadata is carried through, not invented
  assert.strictEqual(byName.terminal_exec.agentForbidden, true);
  assert.strictEqual(byName.tx_prepare.requiresConfirmation, true);
  assert.strictEqual(byName.browser_open.calls, 4);
});

test("category chips and counts are derived from the data, never hardcoded", async () => {
  const env = await booted();
  const cats = env.doc.getElementById("tc-cats").innerHTML;
  assert.match(cats, /data-cat="all"/);
  assert.match(cats, />All<span class="n">5<\/span>/);
  assert.match(cats, /data-cat="Browser"/);
  assert.match(cats, /data-cat="Web3"/);
  assert.ok(!/data-cat="Wallet"/.test(cats), "a category with no tools must not appear");
});

test("search matches name, description and category and updates immediately", async () => {
  const env = await booted();
  const q = env.doc.getElementById("tc-q");
  const list = env.doc.getElementById("tc-list");
  q.value = "transaction";
  await q.fire("input", {});
  assert.match(list.innerHTML, /tx_prepare/);
  assert.ok(!/browser_open/.test(list.innerHTML));
  q.value = "browser";        // matches category too
  await q.fire("input", {});
  assert.match(list.innerHTML, /browser_open/);
  assert.ok(!/read_file/.test(list.innerHTML));
  q.value = "read content";   // matches description only
  await q.fire("input", {});
  assert.match(list.innerHTML, /read_file/);
});

test("category filter and search combine", async () => {
  const env = await booted();
  const root = env.doc.getElementById("tab-tool-center");
  await click(root, "[data-cat]", { dataset: { cat: "Terminal" } });
  const list = env.doc.getElementById("tc-list");
  assert.match(list.innerHTML, /terminal_exec/);
  assert.ok(!/browser_open/.test(list.innerHTML));
  const q = env.doc.getElementById("tc-q");
  q.value = "browser";        // no Terminal tool matches -> empty state
  await q.fire("input", {});
  assert.match(list.innerHTML, /No tools found/);
  q.value = "";
  await q.fire("input", {});
  assert.match(list.innerHTML, /terminal_exec/);
});

test("selecting a tool renders its real schema and safety metadata", async () => {
  const env = await booted();
  const root = env.doc.getElementById("tab-tool-center");
  await click(root, "[data-tool]", { dataset: { tool: "browser_open" } });
  const d = env.doc.getElementById("tc-detail").innerHTML;
  assert.match(d, /browser_open/);
  assert.match(d, /Browser/);
  assert.match(d, /Low-risk write/);
  assert.match(d, /Input schema/);
  assert.match(d, /&quot;url&quot;/);       // schema JSON, HTML-escaped
  assert.match(d, /Usage/);
  assert.match(d, /2026-09-29 18:00:00/);
  assert.ok(!/Run \/ Test Tool|Run tool|Execute tool/i.test(d),
    "no fabricated execution action — the API has no run endpoint");
});

test("host-only tools show the Agent block; confirmation is surfaced", async () => {
  const env = await booted();
  const root = env.doc.getElementById("tab-tool-center");
  await click(root, "[data-tool]", { dataset: { tool: "terminal_exec" } });
  assert.match(env.doc.getElementById("tc-detail").innerHTML, /Blocked \(host-only\)/);
  const list = env.doc.getElementById("tc-list").innerHTML;
  assert.match(list, /Host-only/);
  await click(root, "[data-tool]", { dataset: { tool: "tx_prepare" } });
  assert.match(env.doc.getElementById("tc-detail").innerHTML, /Required · web3_tx/);
});

test("refresh re-reads /api/tools; a failure keeps the previous rows on screen", async () => {
  let fail = false;
  const env = await booted(() => (fail
    ? { ok: false, error: "network: down" }
    : { ok: true, data: PAYLOAD }));
  assert.strictEqual(env.calls.length, 1);
  await click(env.doc.getElementById("tab-tool-center"), "#tc-refresh");
  assert.strictEqual(env.calls.length, 2, "Refresh must hit /api/tools again");
  assert.match(env.doc.getElementById("tc-list").innerHTML, /browser_open/);
  fail = true;
  await click(env.doc.getElementById("tab-tool-center"), "#tc-refresh");
  assert.strictEqual(env.win.ToolCenter.state.error, "network: down");
  assert.match(env.doc.getElementById("tc-list").innerHTML, /browser_open/,
    "previous data must survive a failed refresh");
});

test("a total failure with no cached data shows an error state with Retry", async () => {
  let fail = true;
  const env = await booted(() => (fail ? { ok: false, error: "network: down" } : { ok: true, data: PAYLOAD }));
  const list = env.doc.getElementById("tc-list");
  assert.match(list.innerHTML, /Unable to load tools/);
  assert.match(list.innerHTML, /tc-retry/);
  fail = false;
  await click(env.doc.getElementById("tab-tool-center"), "#tc-retry");
  assert.match(env.doc.getElementById("tc-list").innerHTML, /browser_open/);
});

test("narrow screens open the detail panel as a drawer", async () => {
  const env = makeEnvironment();
  env.win.matchMedia = () => ({ matches: true });
  await env.win.ToolCenter.open();
  const host = env.doc.getElementById("tab-tool-center");
  const drawer = env.doc.getElementById("tc-root");
  await click(host, "[data-tool]", { dataset: { tool: "browser_open" } });
  assert.ok(drawer.classList.contains("open"), "the drawer container must open");
  await click(host, "[data-close]", {});
  assert.ok(!drawer.classList.contains("open"), "close must dismiss the drawer");
});
