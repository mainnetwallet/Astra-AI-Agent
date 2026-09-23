/* Assistant-Chat HOST-terminal approval card, driving the REAL
 * static/js/astra.js over the shared DOM shim (tests/js/dom_shim.js).
 *
 * Spec: the Allow/Deny decision lives ONLY in the Assistant Chat. Astra
 * Agent Runtime is the primary environment; a host command runs only after
 * Allow. This pins:
 *   * a `host_approval` message renders the card (warning, exact command,
 *     cwd, reason, the HOST-execution warning, Deny + Allow),
 *   * clicking Allow POSTs to /api/terminal/approval/<id> with
 *     {decision:"allow"} and the card resolves to "Approved by you",
 *   * clicking Deny never asks the host to run it,
 *   * an already-resolved card (denied) renders the final state with no
 *     buttons — it survives a reload in the chat history,
 *   * the Astra Agent Terminal never contains approval UI.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { HTML, Doc, findAll, findEl, textOf, flush,
        REAL_SET_INTERVAL } = require("./dom_shim.js");

const ROOT = path.join(__dirname, "..", "..");

function approvalMessage(overrides) {
  return Object.assign({
    id: 5, role: "ai", text: "⚠ Host terminal approval is waiting in this chat.",
    action: "host_approval", artifacts: [],
    data: { approval: Object.assign({
      approval_id: "ap-1", command: "docker ps", cwd: "/srv/project",
      reason: "the Docker daemon runs on the host, not in the Agent Runtime",
      status: "pending", environment: "host",
    }, (overrides || {}).approval || {}) },
  }, {});
}

function makeEnvironment(historyMessages, responder) {
  const doc = new Doc(HTML);
  const calls = [];
  const sources = [];
  let pendingChat = null;

  const respond = (method, url, body) => {
    calls.push({ method, url, body });
    const p = String(url).split("?")[0];
    if (p === "/api/manifest") {
      return { ok: true, data: { name: "Astra",
                                 tabs: [{ tab: "assistant", label: "Assistant" }] } };
    }
    if (p === "/api/dashboard") return { ok: true, data: [] };
    if (p === "/api/chat/history") {
      return { ok: true, data: { conversation_id: 1,
                                 messages: historyMessages || [],
                                 last_id: 5, pending: false } };
    }
    if (p === "/api/events") return { ok: true, data: [] };
    if (p === "/api/chat" && method === "POST") {
      return new Promise((res) => { pendingChat = res; });
    }
    if (responder) {
      const out = responder(method, url, body);
      if (out !== undefined) return out;
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
    return { json: async () => await respond(method, url, opts && opts.body) };
  };
  globalThis.api = async (url, opts = {}) =>
    await (await globalThis.fetch(url, opts)).json();
  globalThis.post = (url, body) => globalThis.api(url, { method: "POST", body });
  globalThis.del = (url) => globalThis.api(url, { method: "DELETE" });

  class EventSourceShim {
    constructor(url) { this.url = url; sources.push(this); }
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
  // astra.js restores the transcript when the Assistant tab is shown; the
  // loader is what calls chatRestore(). Drive it like showTab() would.
  if (globalThis.window.Astra.loaders.assistant) {
    globalThis.window.Astra.loaders.assistant();
  }

  const env = {
    doc, calls,
    chatLog: () => doc.getElementById("chat-log"),
    card: () => findEl(doc.getElementById("chat-log"), "msg-host-approval"),
    async clickAllow() {
      const btn = findEl(env.card(), "hoa-allow");
      assert.ok(btn, "Allow button present");
      await btn.fire("click");
      await flush();
    },
    async clickDeny() {
      const btn = findEl(env.card(), "hoa-deny");
      assert.ok(btn, "Deny button present");
      await btn.fire("click");
      await flush();
    },
    approveCalls: () => calls.filter(
      (c) => c.url.indexOf("/api/terminal/approval") === 0),
    chatPosts: () => calls.filter((c) => c.url.split("?")[0] === "/api/chat"),
  };
  return flush().then(() => flush()).then(
    () => { globalThis.setInterval = REAL_SET_INTERVAL; return env; });
}

test("pending approval renders a chat card with Deny + Allow and the host warning", async () => {
  const env = await makeEnvironment([approvalMessage()]);
  const card = env.card();
  assert.ok(card, "approval card rendered");
  const text = textOf(card);
  assert.match(text, /Host Terminal Access Required/);
  assert.match(text, /docker ps/);
  assert.match(text, /\/srv\/project/);
  assert.match(text, /Docker daemon runs on the host/);
  assert.match(text, /HOST system/);
  assert.match(text, /outside Astra Agent Runtime/);
  assert.ok(findEl(card, "hoa-deny"), "Deny button");
  assert.ok(findEl(card, "hoa-allow"), "Allow button");
});

test("clicking Allow posts the exact decision and resolves the card", async () => {
  const env = await makeEnvironment([approvalMessage()], (method, url) => {
    if (url.indexOf("/api/terminal/approval/ap-1") === 0 && method === "POST") {
      return { ok: true, data: { reply: "✓ Approved by you",
        action: "none", artifacts: [], data: { approval: {
          approval_id: "ap-1", command: "docker ps", cwd: "/srv/project",
          status: "completed", executed: true, environment: "host",
          result: { exit_code: 0 } } } } };
    }
    return undefined;
  });
  await env.clickAllow();
  const calls = env.approveCalls();
  assert.strictEqual(calls.length, 1, "exactly one approval POST");
  assert.match(String(calls[0].url), /\/api\/terminal\/approval\/ap-1$/);
  assert.deepStrictEqual(JSON.parse(calls[0].body), { decision: "allow" });
  const text = textOf(env.card());
  assert.match(text, /Approved by you/);
  assert.match(text, /Host command executed/);
  assert.ok(!findEl(env.card(), "hoa-allow"), "buttons gone after resolution");
});

test("clicking Deny never executes the host command and shows the final state", async () => {
  const env = await makeEnvironment([approvalMessage()], (method, url) => {
    if (url.indexOf("/api/terminal/approval/ap-1") === 0 && method === "POST") {
      return { ok: true, data: { reply: "✕ Denied", action: "none",
        artifacts: [], data: { approval: {
          approval_id: "ap-1", command: "docker ps", cwd: "/srv/project",
          status: "denied", executed: false, environment: "host" } } } };
    }
    return undefined;
  });
  await env.clickDeny();
  const text = textOf(env.card());
  assert.match(text, /Denied/);
  assert.match(text, /not executed/);
});

test("a resolved (denied) card survives a reload with no buttons", async () => {
  const env = await makeEnvironment([
    approvalMessage({ approval: { status: "denied", executed: false } }),
  ]);
  const card = env.card();
  assert.ok(card, "resolved card still rendered from history");
  assert.match(textOf(card), /Denied/);
  assert.ok(!findEl(card, "hoa-allow"), "no Allow on a resolved card");
  assert.ok(!findEl(card, "hoa-deny"), "no Deny on a resolved card");
});

test("the Astra Agent Terminal never carries approval UI", () => {
  const terminalJs = fs.readFileSync(
    path.join(ROOT, "static/js/terminal.js"), "utf8");
  const indexPath = path.join(ROOT, "static/index.html");
  const index = fs.readFileSync(indexPath, "utf8");
  for (const src of [terminalJs]) {
    assert.ok(!/host_approval|terminal\/approval|hoa-allow|hoa-deny/.test(src),
              "terminal.js must not render approval UI");
  }
  // The terminal pane markup itself must not contain Allow/Deny controls.
  const pane = index.slice(index.indexOf('id="tab-terminal"'),
                           index.indexOf('id="tab-terminal"') + 4000);
  assert.ok(!/hoa-allow|hoa-deny/.test(pane));
});
