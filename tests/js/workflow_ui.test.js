/* Integration test for the Agent Workflow tab (static/js/workflow.js) with a
 * minimal DOM.
 *
 * No browser is needed: the module only touches the DOM through
 * getElementById / innerHTML / classList / addEventListener, so a tiny shim
 * drives the REAL render + save + run code paths. Element ids are taken from
 * static/index.html, which is what makes this a wiring test as well — an id
 * that exists in neither index.html nor a rendered template shows up here.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const HTML = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");
const SOURCE = fs.readFileSync(path.join(ROOT, "static/js/workflow.js"), "utf8");

const ID_RE = /id="([^"]+)"/g;

class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  toggle(c, on) {
    const want = on === undefined ? !this.set.has(c) : !!on;
    if (want) this.set.add(c); else this.set.delete(c);
    return want;
  }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(doc, id) {
    this.ownerDocument = doc;
    this.id = id || "";
    this.tagName = "DIV";
    this.type = "text";
    this.dataset = {};
    this.style = {};
    this.children = [];
    this._listeners = {};
    this._html = "";
    this.classList = new ClassList();
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.hidden = false;
    this.className = "";
    this.checked = false;
    this.parentElement = null;
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v);
    this.ownerDocument._scanIds(this._html);
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener() {}
  fire(type, ev) {
    return Promise.resolve().then(() => {
      let out;
      (this._listeners[type] || []).forEach((fn) => { out = fn(ev || {}); });
      return out;
    });
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  appendChild(child) { this.children.push(child); return child; }
  remove() {}
  setAttribute(k, v) { this[k] = v; }
  getAttribute() { return ""; }
  focus() {}
  select() {}
  blur() {}
  click() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
}

class Doc {
  constructor(html) {
    this._els = new Map();
    this._scanIds(html);
  }
  _scanIds(html) {
    let m;
    ID_RE.lastIndex = 0;
    while ((m = ID_RE.exec(html))) this._ensure(m[1]);
  }
  _ensure(id) {
    if (!this._els.has(id)) this._els.set(id, new El(this, id));
    return this._els.get(id);
  }
  has(id) { return this._els.has(id); }
  getElementById(id) { return this._els.get(id) || null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  createElement() { return new El(this, ""); }
  createElementNS() { return new El(this, ""); }
  addEventListener() {}
}

/* ------------------------------------------------------------- fixtures -- */
const TOOLS = [
  { name: "get_health", category: "system", description: "runtime health",
    risk_level: "read", input_schema: {} },
  { name: "ai_generate", category: "ai", description: "ask a model",
    risk_level: "read",
    input_schema: { prompt: { type: "string", required: true },
                    provider: { type: "string" },
                    model: { type: "string" } } },
];
const RUN_1 = {
  id: 3, workflow_id: 1, name: "Alpha", status: "completed", current_step: "s1",
  params: {}, results: { s1: { ok: true, output: { version: "1.0.0" } } },
  error: "", started_at: "2026-09-23 10:00:00", completed_at: "2026-09-23 10:00:01",
};
const DEF_1 = {
  id: 1, name: "Alpha", description: "", enabled: 1,
  steps: [{ id: "s1", name: "", tool: "get_health", params: {}, depends_on: [], if: null }],
  layout: { s1: { x: 340, y: 40 } },
};

function makeEnvironment() {
  const doc = new Doc(HTML);
  const calls = [];
  let created = 0;

  const respond = (method, url, body) => {
    calls.push({ method, url, body });
    const p = url.split("?")[0];
    if (p === "/api/workflows/options") {
      return { ok: true, data: { tools: TOOLS,
        categories: { system: ["get_health"], ai: ["ai_generate"] },
        providers: [{ name: "gemini", state: "healthy", healthy: true,
                      models: [{ id: "gemini-flash", capabilities: ["chat"] }] }],
        models: [], scheduler: true,
        statuses: ["created", "running", "completed", "failed"],
        schedule_kinds: ["daily", "interval", "oneshot", "weekly", "deadline"] } };
    }
    if (p === "/api/workflows" && method === "GET") {
      return { ok: true, data: [{ id: 1, name: "Alpha", run_count: 1,
        last_run: { id: 3, status: "completed", started_at: RUN_1.started_at,
                    completed_at: RUN_1.completed_at, error: "" } }] };
    }
    if (p === "/api/workflows" && method === "POST") {
      created += 1;
      return { ok: true, data: { id: 20 + created, name: body.name + (created > 1 ? " 2" : ""),
        description: body.description, steps: body.steps, layout: body.layout,
        enabled: 1, created_at: "now", updated_at: "now" } };
    }
    if (/^\/api\/workflows\/\d+$/.test(p) && method === "PATCH") {
      return { ok: true, data: Object.assign({}, DEF_1, { name: body.name || DEF_1.name }) };
    }
    if (/^\/api\/workflows\/\d+$/.test(p) && method === "GET") {
      const id = Number(p.split("/").pop());
      if (id === 1) return { ok: true, data: DEF_1 };
      return { ok: true, data: Object.assign({}, DEF_1, { id, name: "Created " + id }) };
    }
    if (/^\/api\/workflows\/\d+\/runs$/.test(p)) return { ok: true, data: [RUN_1] };
    if (p === "/api/workflows/runs/3") return { ok: true, data: RUN_1 };
    if (/^\/api\/workflows\/\d+\/run$/.test(p)) {
      return { ok: true, data: Object.assign({}, RUN_1, { id: 9, status: "completed" }) };
    }
    if (p === "/api/schedules") return { ok: true, data: [] };
    if (p === "/api/events") {
      return { ok: true, data: [
        { id: 11, kind: "workflow.started",
          data: { run_id: 3, workflow: "Alpha" }, created_at: "2026-09-23 10:00:00" },
        { id: 12, kind: "task.started",
          data: { run_id: 3, step: "s1" }, created_at: "2026-09-23 10:00:00" },
        { id: 13, kind: "task.completed",
          data: { run_id: 3, step: "s1", tool: "get_health", duration_ms: 12,
                  terminal: true }, created_at: "2026-09-23 10:00:01" },
        { id: 14, kind: "workflow.completed",
          data: { run_id: 3, workflow: "Alpha", terminal: true },
          created_at: "2026-09-23 10:00:01" },
        { id: 21, kind: "workflow.started",
          data: { run_id: 9, workflow: "Alpha" }, created_at: "2026-09-23 11:00:00" },
        { id: 22, kind: "task.started",
          data: { run_id: 9, step: "s1" }, created_at: "2026-09-23 11:00:00" },
        { id: 23, kind: "task.completed",
          data: { run_id: 9, step: "s1", tool: "get_health", duration_ms: 7,
                  terminal: true }, created_at: "2026-09-23 11:00:01" },
        { id: 24, kind: "workflow.completed",
          data: { run_id: 9, workflow: "Alpha", terminal: true },
          created_at: "2026-09-23 11:00:01" },
      ] };
    }
    if (p === "/api/schedules" || /^\/api\/schedules\/\d+$/.test(p)) {
      return { ok: true, data: [] };
    }
    return { ok: true, data: {} };
  };

  globalThis.window = {
    addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({ matches: false }),
    confirm: () => true,
    Astra: { loaders: {} },
  };
  globalThis.document = doc;
  globalThis.localStorage = {
    _m: {},
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; },
    setItem(k, v) { this._m[k] = String(v); },
    removeItem(k) { delete this._m[k]; },
  };
  globalThis.esc = (s) => String(s === undefined || s === null ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                                   '"': "&quot;", "'": "&#39;" }[c]));
  globalThis.api = async (url, opts) => respond((opts && opts.method) || "GET",
                                                url, opts && opts.body);
  globalThis.post = (url, body) => globalThis.api(url, { method: "POST", body });
  globalThis.patch = (url, body) => globalThis.api(url, { method: "PATCH", body });
  globalThis.del = (url) => globalThis.api(url, { method: "DELETE" });

  window.AstraWorkflowModel = require("../../static/js/workflow_model.js");
  // fresh module instance per test: the tab keeps state in module scope, so
  // a cached copy would leak one test's draft into the next.
  const entry = require.resolve("../../static/js/workflow.js");
  delete require.cache[entry];
  require(entry);
  return { doc, calls, win: globalThis.window };
}

function delegated(selector, target) {
  return { target: { closest: (sel) => (sel === selector ? target : null) },
           preventDefault() {} };
}

async function booted() {
  const env = makeEnvironment();
  assert.strictEqual(typeof env.win.Astra.loaders.workflow, "function",
                     "the tab loader must register into Astra.loaders");
  await env.win.Astra.loaders.workflow();
  return env;
}

/* ---------------------------------------------------------------- tests -- */

test("boot loads the workflow list and opens the remembered workflow", async () => {
  const env = await booted();
  const list = env.doc.getElementById("wf-list").innerHTML;
  assert.match(list, /Alpha/);
  assert.match(list, /completed/);
  const wf = env.win.AstraWorkflow.state;
  assert.strictEqual(wf.draft.id, 1);
  assert.strictEqual(wf.draft.mode, "edit");
  assert.strictEqual(wf.draft.steps.length, 1);
  assert.strictEqual(env.doc.getElementById("wf-name").value, "Alpha");
  assert.match(env.doc.getElementById("wf-badge").textContent, /#1/);
});

test("the canvas renders a trigger, the steps and real edges", async () => {
  const env = await booted();
  const nodes = env.doc.getElementById("wf-nodes").innerHTML;
  assert.match(nodes, /data-node-id="__trigger__"/);
  assert.match(nodes, /data-node-id="s1"/);
  assert.match(nodes, /get_health/);
  assert.match(nodes, /wf-port out/);
  assert.match(env.doc.getElementById("wf-status").innerHTML, /run #3 completed/);
});

test("the execution log is built from the shared event stream", async () => {
  const env = await booted();
  const panel = env.doc.getElementById("wf-panel").innerHTML;
  assert.match(panel, /task\.completed/);
  assert.match(panel, /get_health/);
  assert.match(panel, /run #3/);
});

test("run history lists real runs for the selected workflow", async () => {
  const env = await booted();
  await env.doc.getElementById("wf-tabs").fire("click",
    delegated("[data-wf-tab]", { dataset: { wfTab: "runs" } }));
  const panel = env.doc.getElementById("wf-panel").innerHTML;
  assert.match(panel, /#3/);
  assert.match(panel, /completed/);
});

test("node library is built from the live tool registry", async () => {
  const env = await booted();
  await env.doc.getElementById("wf-add").fire("click", {});
  const pal = env.doc.getElementById("wf-palette-list").innerHTML;
  assert.match(pal, /get_health/);
  assert.match(pal, /ai_generate/);
  assert.match(pal, /AI \/ Agent/);
  assert.match(pal, /data-add-cond/);
});

test("New Workflow clears the selection: no id, no steps, no run", async () => {
  const env = await booted();
  const beforeDraft = env.win.AstraWorkflow.state.draft;
  assert.strictEqual(beforeDraft.id, 1);
  await env.doc.getElementById("wf-new").fire("click", {});
  const state = env.win.AstraWorkflow.state;
  assert.strictEqual(state.draft.mode, "new");
  assert.strictEqual(state.draft.id, null);
  assert.deepStrictEqual(state.draft.steps, []);
  assert.strictEqual(state.run, null);
  assert.deepStrictEqual(state.events, []);
  assert.notStrictEqual(state.draft, beforeDraft);
  assert.match(env.doc.getElementById("wf-badge").textContent, /new/);
});

test("a new workflow POSTs (never PATCHes) and adopts the returned id", async () => {
  const env = await booted();
  await env.doc.getElementById("wf-new").fire("click", {});
  await env.doc.getElementById("wf-palette-list").fire("click",
    delegated("[data-add-tool]", { dataset: { addTool: "get_health" } }));
  const state = env.win.AstraWorkflow.state;
  assert.strictEqual(state.draft.steps.length, 1);

  await env.doc.getElementById("wf-save").fire("click", {});
  const creates = env.calls.filter((c) => c.method === "POST" && c.url === "/api/workflows");
  const patches = env.calls.filter((c) => c.method === "PATCH");
  assert.strictEqual(creates.length, 1, "creating must POST exactly once");
  assert.strictEqual(patches.length, 0, "creating must never PATCH an existing workflow");
  assert.deepStrictEqual(creates[0].body.steps.map((s) => s.tool), ["get_health"]);
  assert.strictEqual(state.draft.mode, "edit");
  assert.strictEqual(state.draft.id, 21, "the draft adopts the id the server returned");
  assert.match(env.doc.getElementById("wf-badge").textContent, /#21/);
});

test("renaming an existing workflow PATCHes only that workflow", async () => {
  const env = await booted();
  const nameBox = env.doc.getElementById("wf-name");
  nameBox.value = "Renamed";
  nameBox.fire("change", {});
  await new Promise((r) => setTimeout(r, 0));   // the rename is a live PATCH
  const patches = env.calls.filter((c) => c.method === "PATCH");
  assert.strictEqual(patches.length, 1);
  assert.strictEqual(patches[0].url, "/api/workflows/1");
  assert.strictEqual(patches[0].body.name, "Renamed");
  assert.strictEqual(env.win.AstraWorkflow.state.draft.name, "Renamed");
});

test("switching workflow replaces the draft object entirely", async () => {
  const env = await booted();
  const first = env.win.AstraWorkflow.state.draft;
  await env.doc.getElementById("wf-list").fire("click",
    delegated("[data-wf]", { dataset: { wf: "1" } }));
  const second = env.win.AstraWorkflow.state.draft;
  assert.notStrictEqual(first, second);
  assert.notStrictEqual(first.steps, second.steps);
});

test("Run executes on the backend and then shows the real run", async () => {
  const env = await booted();
  await env.doc.getElementById("wf-run").fire("click", {});
  const runs = env.calls.filter((c) => c.method === "POST" && /\/run$/.test(c.url));
  assert.strictEqual(runs.length, 1);
  assert.strictEqual(runs[0].url, "/api/workflows/1/run");
  const state = env.win.AstraWorkflow.state;
  assert.strictEqual(state.run.id, 9);
  assert.strictEqual(state.busy, false);
  assert.match(env.doc.getElementById("wf-status").innerHTML, /run #9 completed/);
  // the log was re-read from the persisted events for THAT run
  assert.ok(state.events.length >= 1, "run #9 events must be loaded");
  assert.match(env.doc.getElementById("wf-panel").innerHTML,
               /run #9 · completed/);
});

test("a NAMED node selection opens its inspector with schema-driven fields", async () => {
  const env = await booted();
  await env.doc.getElementById("wf-run").fire("click", {});
  // select the AI node through the model the canvas reads
  env.win.AstraWorkflow.state.selected = "s1";
  env.win.AstraWorkflow.refresh();
  const body = env.doc.getElementById("wf-inspector-body").innerHTML;
  assert.match(body, /wf-node-tool/);
  assert.match(body, /Depends on/);
  assert.match(body, /Raw params/);
  assert.match(body, /Last result/);
});

test("live events paint the running node and extend the log", async () => {
  const env = await booted();
  const api = env.win.AstraWorkflow;
  api.state.run = { id: 3, status: "running", current_step: "s1", results: {} };
  api.onEvent({ id: 30, kind: "task.started", created_at: "2026-09-23 10:00:02",
                data: { run_id: 3, step: "s1" } });
  assert.strictEqual(api.state.live.s1, "running");
  assert.ok(api.state.events.some((e) => e.id === 30),
            "the live event is appended to the execution log");
  api.onEvent({ id: 31, kind: "task.completed", created_at: "2026-09-23 10:00:03",
                data: { run_id: 3, step: "s1", tool: "get_health", terminal: true } });
  assert.strictEqual(api.state.live.s1, undefined);
});

test("events for another run are ignored", async () => {
  const env = await booted();
  const api = env.win.AstraWorkflow;
  const before = api.state.events.length;
  api.onEvent({ id: 99, kind: "task.started", created_at: "2026-09-23 10:00:04",
                data: { run_id: 4242, step: "sX" } });
  assert.strictEqual(api.state.events.length, before);
});

test("every element id workflow.js asks for is in index.html or rendered", async () => {
  const env = await booted();
  const api = env.win.AstraWorkflow;
  await env.doc.getElementById("wf-add").fire("click", {});
  // workflow + trigger inspectors
  api.state.selected = "__trigger__";
  api.refresh();
  // add a second (AI) node, then a condition that gates it
  await env.doc.getElementById("wf-palette-list").fire("click",
    delegated("[data-add-tool]", { dataset: { addTool: "ai_generate" } }));
  api.state.selected = "s2";
  api.refresh();                       // AI node -> provider/model pickers
  await env.doc.getElementById("wf-palette-list").fire("click",
    delegated("[data-add-cond]", {}));  // condition inspector
  assert.ok(api.state.draft.steps.some((st) => st.if),
            "the condition node gated a real step");
  api.state.selected = "s2";
  api.refresh();                       // a gated step -> the `if` selects
  const ids = new Set();
  let m;
  const re = /\bel\("([^"]+)"\)/g;
  while ((m = re.exec(SOURCE))) ids.add(m[1]);
  assert.ok(ids.size > 20, "sanity: the source queries many ids");
  const missing = [...ids].filter((id) => !env.doc.has(id));
  assert.deepStrictEqual(missing, [],
    "ids queried by workflow.js that no markup provides");
});

test("the workflow script never installs inline event handlers (CSP)", () => {
  assert.doesNotMatch(SOURCE, /\son(click|change|input|load)\s*=/,
                      "script-src 'self' forbids inline handlers");
});

/* ---------------------------------------------------------------- shell -- */

test("the tab renders the reference shell: header, rail, canvas, inspector, panel", () => {
  for (const frag of ['class="wf-app"', 'class="wf-head"', 'class="wf-brand-mark"',
                      'class="wf-grid"', 'class="wf-workspace"', 'class="wf-canvas-wrap"',
                      'id="wf-run"', 'id="wf-run" title="Run now"',
                      'wf-btn wf-btn-primary', 'id="wf-add"', 'id="wf-tabs"',
                      'class="wf-tab active" data-wf-tab="log"']) {
    assert.ok(HTML.includes(frag), `index.html is missing ${frag}`);
  }
  // the app header carries the primary Run action and the phone toggles
  assert.match(HTML, /id="wf-run"[^>]*>▶ Run/);
  assert.match(HTML, /id="wf-rail-toggle"/);
  assert.match(HTML, /id="wf-inspector-toggle"/);
});

test("the stylesheet defines the reference palette, tones and phone sheet", () => {
  const css = require("node:fs").readFileSync(
    path.join(ROOT, "static/css/style.css"), "utf8");
  for (const frag of ["--wf-accent: #4f17fd", "--tone-green: #01a252",
                      "--tone-violet: #6716fd", "--tone-blue: #006bfb",
                      '--tone-magenta: #f60b7f', '"rail workspace inspector"',
                      ".wf-node.tone-violet .wf-node-icon", ".wf-inspector.open",
                      "body:has(#tab-workflow.active) .topbar"]) {
    assert.ok(css.includes(frag), `style.css is missing ${frag}`);
  }
  assert.strictEqual((css.match(/\{/g) || []).length,
                     (css.match(/\}/g) || []).length, "css braces must balance");
});

test("canvas nodes render as tinted cards with a coloured icon tile", async () => {
  const env = await booted();
  // give the workflow an AI step so a violet card is produced
  env.win.AstraWorkflow.state.toolMap = {
    get_health: TOOLS[0], ai_generate: TOOLS[1] };
  env.win.AstraWorkflow.state.draft.steps = [
    { id: "s1", name: "Ask", tool: "ai_generate", params: {}, depends_on: [] },
    { id: "s2", name: "Health", tool: "get_health", params: {}, depends_on: ["s1"] }];
  env.win.AstraWorkflow.refresh();
  const nodes = env.doc.getElementById("wf-nodes").innerHTML;
  assert.match(nodes, /wf-node-icon/);
  assert.match(nodes, /tone-violet/);       // the AI step
  assert.match(nodes, /tone-green/);        // the trigger + system step
  assert.doesNotMatch(nodes, /wf-node-bar/, "the old left bar is gone");
  assert.match(nodes, /data-node-id="s1"/);
});
