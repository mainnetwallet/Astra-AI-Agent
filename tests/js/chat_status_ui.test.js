/* Integration test for the Assistant chat execution status, driving the REAL
 * static/js/astra.js over a minimal DOM + EventSource shim.
 *
 * No browser needed: astra.js only touches the DOM through
 * getElementById/createElement/appendChild/classList/addEventListener, so this
 * shim exercises the real wiring — the composer, the SSE arrival path
 * (onmessage -> receiveEvent + chatStatusTrack), the paint code and the
 * expand/collapse handler — against the ids and markup of static/index.html.
 *
 * The acceptance sequence (gateway -> provider -> terminal.started ->
 * terminal.completed -> provider -> final response) is asserted on the DOM the
 * user actually sees, not on an internal model.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const HTML = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");
const ID_RE = /id="([^"]+)"/g;
const REAL_SET_TIMEOUT = globalThis.setTimeout.bind(globalThis);
const REAL_SET_INTERVAL = globalThis.setInterval.bind(globalThis);

/* ------------------------------------------------------------- DOM shim -- */
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
    this._attrs = {};
    this.classList = new ClassList();
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.hidden = false;
    this.className = "";
    this.checked = false;
    this.parentElement = null;
    this.scrollTop = 0;
    this.scrollHeight = 200;
    this.clientHeight = 100;
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this.children.forEach((c) => { c.parentElement = null; });
    this.children = [];
    this._html = String(v);
    this.ownerDocument._scanIds(this._html);
  }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  get previousElementSibling() {
    const p = this.parentElement;
    if (!p) return null;
    const i = p.children.indexOf(this);
    return i > 0 ? p.children[i - 1] : null;
  }
  get offsetHeight() { return 20; }
  get isConnected() {
    let n = this;
    while (n.parentElement) n = n.parentElement;
    return n === this.ownerDocument.root;
  }
  appendChild(child) {
    if (child.parentElement) {
      const i = child.parentElement.children.indexOf(child);
      if (i >= 0) child.parentElement.children.splice(i, 1);
    }
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) { this.children.splice(i, 1); child.parentElement = null; }
    return child;
  }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener() {}
  fire(type, ev) {
    const self = this;
    const event = ev || {};
    if (!event.preventDefault) event.preventDefault = () => {};
    return Promise.resolve().then(() => {
      let out;
      (self._listeners[type] || []).forEach((fn) => { out = fn(event); });
      return out;
    });
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  setAttribute(k, v) { this._attrs[k] = String(v); this[k] = String(v); }
  getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; }
  removeAttribute(k) { delete this._attrs[k]; }
  contains(node) {
    if (this === node) return true;
    return this.children.some((c) => c.contains(node));
  }
  focus() {}
  select() {}
  blur() {}
  click() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
}

class Doc {
  constructor(html) {
    this._els = new Map();
    this.root = new El(this, "__root");
    this.head = new El(this, "");
    this.body = new El(this, "");
    this.title = "";
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
  querySelector(sel) {
    // astra.js's $() helper takes a CSS selector; the subset it uses is a
    // plain "#id" (everything else goes through querySelectorAll).
    const s = String(sel);
    if (s.charAt(0) === "#" && s.indexOf(" ") < 0 && s.indexOf(".") < 0) {
      return this.getElementById(s.slice(1));
    }
    return null;
  }
  querySelectorAll() { return []; }
  createElement() { return new El(this, ""); }
  createElementNS() { return new El(this, ""); }
  createDocumentFragment() { return new El(this, ""); }
  createTextNode(text) {
    const el = new El(this, "");
    el.nodeType = 3;
    el.textContent = String(text);
    return el;
  }
  addEventListener() {}
}

/* ------------------------------------------------------------- helpers --- */
function findEl(root, cls) {
  if (!root) return null;
  const names = String(root.className || "").split(/\s+/);
  if (names.indexOf(cls) >= 0) return root;
  for (const c of root.children || []) {
    const hit = findEl(c, cls);
    if (hit) return hit;
  }
  return null;
}
function findAll(root, cls, out) {
  const acc = out || [];
  if (!root) return acc;
  if (String(root.className || "").split(/\s+/).indexOf(cls) >= 0) acc.push(root);
  for (const c of root.children || []) findAll(c, cls, acc);
  return acc;
}
function textOf(el) {
  if (!el) return "";
  let s = String(el.textContent || "");
  if (!el.children || el.children.length === 0) {
    // chatBubble() sets message text through innerHTML; the shim stores it as
    // a plain string, so include it (tags stripped) for the assertions.
    s += " " + String(el._html || "");
  } else {
    for (const c of el.children) s += " " + textOf(c);
  }
  return s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}
const flush = () => new Promise((r) => REAL_SET_TIMEOUT(r, 0));

/* --------------------------------------------------------- environment ---- */
function makeEnvironment() {
  const doc = new Doc(HTML);
  const calls = [];
  const sources = [];
  let pendingChat = null;
  let nextId = 1;

  const respond = (method, url, body) => {
    calls.push({ method, url, body });
    const p = String(url).split("?")[0];
    if (p === "/api/manifest") {
      return { ok: true, data: { name: "Astra", tabs: [{ tab: "assistant", label: "Assistant" }] } };
    }
    if (p === "/api/dashboard") return { ok: true, data: [] };
    if (p === "/api/chat/history") return { ok: true, data: { conversation_id: 1, messages: [], last_id: 0, pending: false } };
    if (p === "/api/events") return { ok: true, data: [] };
    if (p === "/api/chat" && method === "POST") {
      // stays pending until the test resolves it, so the live status is
      // observable exactly as it is while a real turn runs
      return new Promise((res) => { pendingChat = res; });
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
  globalThis.fetch = async (url, opts) => {
    const method = (opts && opts.method) || "GET";
    const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
    return { json: async () => await respond(method, url, body) };
  };
  globalThis.api = async (url, opts = {}) =>
    await (await globalThis.fetch(url, opts)).json();
  globalThis.post = (url, body) => globalThis.api(url, { method: "POST", body });
  globalThis.del = (url) => globalThis.api(url, { method: "DELETE" });

  class EventSourceShim {
    constructor(url) { this.url = url; this.onmessage = null; this.onerror = null; sources.push(this); }
    close() {}
  }
  globalThis.EventSource = EventSourceShim;
  globalThis.window.EventSource = EventSourceShim;

  globalThis.AstraLog = require("../../static/js/log_model.js");
  globalThis.AstraChatStatus = require("../../static/js/chat_status.js");

  // astra.js boots on load and installs a 60s dashboard refresh; keep it from
  // holding node's event loop open (restored right after boot settles).
  globalThis.setInterval = () => 0;
  delete require.cache[require.resolve("../../static/js/astra.js")];
  require("../../static/js/astra.js");
  // astra.js publishes itself on window.Astra; in the browser that is also a
  // bare global, so mirror it like a real page does.
  globalThis.Astra = globalThis.window.Astra;

  const env = {
    doc, calls, sources,
    win: globalThis.window,
    es: () => sources[0],
    event(kind, data) {
      const ev = { id: nextId++, kind, agent: "", data: data || {},
                   created_at: "2026-09-23 10:00:0" + Math.min(9, nextId - 1) };
      assert.ok(sources[0] && sources[0].onmessage, "the live feed must be open");
      sources[0].onmessage({ data: JSON.stringify(ev) });
      return ev;
    },
    resolveChat(payload) { assert.ok(pendingChat, "no chat request pending"); pendingChat(payload); },
    chatLog: () => doc.getElementById("chat-log"),
    statusRow: () => findAll(doc.getElementById("chat-log"), "chat-status-row")[0] || null,
    statusRows: () => findAll(doc.getElementById("chat-log"), "chat-status-row"),
    statusBtn: () => findEl(doc.getElementById("chat-log"), "chat-status"),
    statusSummary: () => textOf(findEl(doc.getElementById("chat-log"), "chat-status-summary")),
    statusLine: () => textOf(findEl(doc.getElementById("chat-log"), "chat-status-action")),
    stepsPanel: () => findEl(doc.getElementById("chat-log"), "chat-steps"),
    stepRows: () => findAll(doc.getElementById("chat-log"), "chat-step"),
    // Fire the real submit handler and let it reach its first await. The
    // handler deliberately stays pending until resolveChat(), so the status
    // is observed exactly as it is while a real turn runs.
    submit(text) {
      doc.getElementById("chat-input").value = text;
      doc.getElementById("chat-form").fire("submit", {});
      return flush();
    },
  };
  return flush().then(() => { globalThis.setInterval = REAL_SET_INTERVAL; return env; });
}

function turn(extra) {
  return Object.assign({ request: "req-1", trace: "req-1" }, extra || {});
}

/* ------------------------------------------------------------------ tests */

test("the typing-only indicator is replaced by the compact execution status", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests, inspect the failure and fix it");
  await flush();

  const rows = env.statusRows();
  assert.strictEqual(rows.length, 1, "exactly one live status row");
  assert.strictEqual(env.statusSummary(), "Working…");
  assert.strictEqual(env.statusLine(), "");            // nothing invented yet
  assert.ok(findAll(env.statusRow(), "chat-status-dots").length === 1);
  assert.strictEqual(findEl(env.statusRow(), "typing"), null, "no typing-only dots left");
  assert.ok(env.statusBtn(), "the status must be a tappable control");
  assert.strictEqual(env.statusBtn()["aria-expanded"], "false");
  assert.strictEqual(env.stepsPanel().hidden, true);
  assert.deepStrictEqual(env.calls.filter((c) => c.method === "POST").map((c) => c.url),
                         ["/api/chat"]);
});

test("live lifecycle events update the one visible status line", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  const seen = [];
  const line = () => { seen.push(env.statusLine()); return env.statusLine(); };

  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  assert.strictEqual(line(), "Understanding your request");
  env.event("astra_gateway.request", turn({ provider: "" }));
  assert.strictEqual(line(), "Selecting AI provider");
  env.event("chat.pipeline.assigned", turn({ provider: "openrouter" }));
  env.event("ai.started", { provider: "openrouter" });
  assert.strictEqual(line(), "Thinking with OpenRouter");
  env.event("ai.completed", { provider: "openrouter" });
  env.event("tool.started", turn({ op: "tool-1", tool: "terminal_exec" }));
  env.event("terminal.started", { op: "term-1", process_id: "p1",
                                  command: "pytest -q --token=secret" });
  assert.strictEqual(line(), "Running terminal command");
  env.event("terminal.completed", { op: "term-1", process_id: "p1",
                                    duration_ms: 3400, status: "completed",
                                    terminal: true, stdout: "123 passed" });
  assert.strictEqual(line(), "Terminal command completed");
  env.event("ai.started", { provider: "openrouter" });
  assert.strictEqual(line(), "Thinking with OpenRouter");
  env.event("chat.pipeline.finished", turn({ op: "chat:req-1", status: "COMPLETE",
                                             terminal: true }));
  // the compact state shows only ONE line per event, never every step
  assert.strictEqual(env.statusRows().length, 1);
  // the root terminal closed the turn: the compact line becomes the summary
  assert.match(env.statusSummary(), /^\u2713 Completed · 5 steps · \d+(\.\d+)?s$/);
  assert.strictEqual(env.statusLine(), "");
  assert.ok(!env.statusSummary().includes("Running terminal command"));
  // no raw tool output or argument ever reaches the compact line
  for (const s of seen) {
    assert.ok(!s.includes("123 passed"), s);
    assert.ok(!s.includes("secret"), s);
    assert.ok(s.length <= 140, s);
  }
});

test("tapping the status expands the real step timeline and executes nothing", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests and fix the failure");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("astra_gateway.request", turn({ provider: "" }));
  env.event("ai.started", { provider: "openrouter" });
  env.event("ai.completed", { provider: "openrouter" });
  env.event("tool.started", turn({ op: "tool-1", tool: "terminal_exec" }));
  env.event("terminal.started", { op: "term-1", process_id: "p1", command: "pytest" });

  const btn = env.statusBtn();
  const callsBefore = env.calls.length;
  const sendDisabledBefore = env.doc.getElementById("chat-send").disabled;

  await btn.fire("click", {});
  assert.strictEqual(btn["aria-expanded"], "true");
  assert.strictEqual(env.stepsPanel().hidden, false);
  const labels = env.stepRows().map(textOf);
  assert.ok(labels.length >= 3, "the timeline lists the real operations");
  assert.ok(labels.some((l) => l.includes("Understanding request")), labels.join("|"));
  assert.ok(labels.some((l) => l.includes("Running terminal command")), labels.join("|"));
  assert.ok(labels.some((l) => l.includes("Reviewing terminal output")),
            "the deterministic next step is shown as pending");
  const marks = env.stepRows().map((r) => textOf(r).trim().charAt(0));
  assert.ok(marks.includes("✓") || marks.includes("\u2713"));
  assert.ok(marks.includes("\u25CF"), "the running step is the active dot");

  await btn.fire("click", {});
  assert.strictEqual(btn["aria-expanded"], "false");
  assert.strictEqual(env.stepsPanel().hidden, true);

  // clicking never re-runs a tool: no new request, no send, no new log rows
  assert.strictEqual(env.calls.length, callsBefore);
  assert.strictEqual(env.doc.getElementById("chat-send").disabled, sendDisabledBefore);
  assert.strictEqual(env.calls.filter((c) => c.url === "/api/chat").length, 1);
});

test("completion keeps a summary and the reply lands after the status", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("tool.started", turn({ op: "tool-1", tool: "terminal_exec" }));
  env.event("terminal.started", { op: "term-1", process_id: "p1", command: "pytest" });
  env.event("terminal.completed", { op: "term-1", process_id: "p1",
                                    duration_ms: 42000, status: "completed",
                                    terminal: true });
  env.event("chat.pipeline.finished", turn({ op: "chat:req-1", terminal: true }));
  env.resolveChat({ ok: true, data: { reply: "All tests pass now.", action: "none" } });
  await flush();

  const log = env.chatLog();
  const summary = env.statusSummary();
  assert.match(summary, /^\u2713 Completed · \d+ steps/);
  assert.ok(/^\u2713 Completed · 2 steps · \d/.test(summary), summary);
  const rowIndex = log.children.indexOf(env.statusRow());
  const lastIndex = log.children.length - 1;
  assert.ok(rowIndex >= 0 && rowIndex < lastIndex,
            "the status sits above the reply bubble");
  assert.match(textOf(log.children[lastIndex]), /All tests pass now\./);
  assert.strictEqual(env.stepsPanel().hidden, true, "completed status starts collapsed");
  assert.strictEqual(env.statusBtn().classList.contains("completed"), true);
  assert.strictEqual(env.statusBtn().classList.contains("working"), false);
});

test("a failed turn shows one human reason and never a secret", async () => {
  const env = await makeEnvironment();
  await env.submit("Deploy it");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("chat.pipeline.failed", turn({
    op: "chat:req-1", terminal: true,
    error: "ProviderError: 401 unauthorized for token " +
           "ghp_000000000000000000000000000000000000",
  }));
  const summary = env.statusSummary();
  assert.match(summary, /^\u2715 Failed · /);
  assert.ok(!summary.includes("ghp_"), summary);
  assert.ok(env.statusSummary().length <= 145, summary);
  env.resolveChat({ ok: false, error: "no provider available" });
  await flush();
  assert.match(env.statusSummary(), /^\u2715 Failed · /);
});

test("tool output and protocol payloads never flood the chat status", async () => {
  const env = await makeEnvironment();
  await env.submit("Inspect the logs");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("terminal.started", { op: "term-1", process_id: "p1", command: "cat dump.json" });
  env.event("terminal.output", { op: "term-1", process_id: "p1", stream: "stdout",
    snippet: '{"tool":"terminal_exec","args":{"api_key":"sk-abcdefghijklmnopqrstuvwxyz"},' +
             '"stdout":"' + "x".repeat(400) + '"}', chars: 900, status: "running" });
  const line = env.statusLine();
  assert.strictEqual(line, "Running terminal command");
  assert.ok(!line.includes("tool"), line);
  assert.ok(!line.includes("sk-"), line);
  assert.ok(line.length < 60, line);
  const rowText = textOf(env.statusRow());
  assert.ok(!rowText.includes("sk-"), rowText);
  assert.ok(!rowText.includes("api_key"), rowText);
});

test("the shared live feed is opened once for log + chat", async () => {
  const env = await makeEnvironment();
  await env.submit("hi");
  await flush();
  assert.strictEqual(env.sources.length, 1, "one EventSource for the whole app");
  assert.strictEqual(env.es().url, "/api/events/stream");
  // opening the Activity Log tab afterwards must reuse it, never open a second
  await env.win.Astra.loaders.logs();
  await flush();
  assert.strictEqual(env.sources.length, 1);
});

test("a stale/out-of-order event cannot overwrite the shown operation", async () => {
  const env = await makeEnvironment();
  await env.submit("Run it");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("terminal.started", { op: "term-A", process_id: "pA", command: "sleep 60" });
  assert.strictEqual(env.statusLine(), "Running terminal command");
  // an old, unrelated terminal completion replayed late (lower id)
  const stale = { id: 2, kind: "terminal.completed", agent: "",
                  data: { op: "term-OLD", process_id: "pOLD", status: "completed" },
                  created_at: "2026-09-23 09:00:00" };
  env.es().onmessage({ data: JSON.stringify(stale) });
  assert.strictEqual(env.statusLine(), "Running terminal command");
  assert.strictEqual(env.stepRows().filter((r) => r.classList.contains("active")).length,
                     env.stepsPanel().hidden ? 0 : 1);
});
