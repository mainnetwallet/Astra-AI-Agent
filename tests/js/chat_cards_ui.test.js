/* Integration test for the Assistant chat EXECUTION CARDS, driving the REAL
 * static/js/astra.js over the shared minimal DOM + EventSource shim
 * (tests/js/dom_shim.js).
 *
 * What this pins, on the DOM the user actually sees:
 *   * one real terminal execution -> exactly one card,
 *   * three terminal_exec calls -> three SEPARATE cards, in order,
 *   * terminal.started -> running card, completion -> the SAME card completes,
 *   * failure/timeout/stopped -> the same card, with the right state,
 *   * browser/file/web/web3 cards never merge with terminal cards,
 *   * the compact live status keeps working above the cards,
 *   * tapping a card only expands read-only detail (never an API call),
 *   * "load full output" pages the read-only endpoint, one GET at a time.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");

const { HTML, Doc, findAll, findEl, textOf, flush,
        REAL_SET_INTERVAL } = require("./dom_shim.js");

/* --------------------------------------------------------- environment ---- */
function makeEnvironment() {
  const doc = new Doc(HTML);
  const calls = [];
  const sources = [];
  let pendingChat = null;
  let nextId = 1;
  const outputPage = (offset, length) => {
    const total = 40;
    const text = "0123456789".repeat(4).slice(offset, offset + length);
    const next = offset + text.length;
    return { status: "ok", offset, chars_returned: text.length,
             total_chars: total, text, done: next >= total,
             next_offset: next >= total ? null : next };
  };

  const respond = (method, url) => {
    calls.push({ method, url });
    const p = String(url).split("?")[0];
    if (p === "/api/manifest") {
      return { ok: true, data: { name: "Astra",
                                 tabs: [{ tab: "assistant", label: "Assistant" }] } };
    }
    if (p === "/api/dashboard") return { ok: true, data: [] };
    if (p === "/api/chat/history") {
      return { ok: true, data: { conversation_id: 1, messages: [], last_id: 0,
                                 pending: false } };
    }
    if (p === "/api/events") return { ok: true, data: [] };
    if (p === "/api/terminal/output") {
      const q = String(url).split("?")[1] || "";
      const params = {};
      q.split("&").forEach((kv) => {
        const [k, v] = kv.split("=");
        params[decodeURIComponent(k)] = decodeURIComponent(v || "");
      });
      return { ok: true, data: Object.assign(
        { stream: params.stream || "stdout", blob_id: params.blob_id || "",
          command: "pytest -q", cwd: "/repo", status: "completed",
          exit_code: 0, duration_ms: 61800, stdout_total_chars: 40 },
        outputPage(Number(params.offset || 0), Number(params.length || 6000))) };
    }
    if (p === "/api/chat" && method === "POST") {
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
    return { json: async () => await respond(method, url) };
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

  globalThis.setInterval = () => 0;
  delete require.cache[require.resolve("../../static/js/astra.js")];
  require("../../static/js/astra.js");
  globalThis.Astra = globalThis.window.Astra;

  let evId = 0;
  const env = {
    doc, calls, sources,
    win: globalThis.window,
    es: () => sources[sources.length - 1],
    event(kind, data) {
      evId += 1;
      const e = { id: evId, kind, agent: "", data: data || {},
                  created_at: "2026-09-23 10:00:00" };
      sources[sources.length - 1].onmessage({ data: JSON.stringify(e) });
      return e;
    },
    chatLog: () => doc.getElementById("chat-log"),
    async submit(text) {
      const input = doc.getElementById("chat-input");
      const form = doc.getElementById("chat-form");
      input.value = text;
      nextId += 1;
      // MUST NOT await the POST: the test needs the UI in its "working" state.
      form.requestSubmit ? form.requestSubmit() : form.fire("submit", {});
      return nextId;
    },
    resolveChat(payload) {
      const fn = pendingChat;
      pendingChat = null;
      if (fn) fn(payload);
    },
    statusRow: () => findEl(doc.getElementById("chat-log"), "chat-status-row"),
    statusLine: () => textOf(findEl(env.statusRow(), "chat-status-action")),
  };
  return flush().then(() => { globalThis.setInterval = REAL_SET_INTERVAL; return env; });
}

function turn(extra) {
  return Object.assign({ request: "req-1", trace: "req-1" }, extra || {});
}
const cardsIn = (env) => findAll(env.statusRow(), "chat-exec");
const cardButtons = (env) => findAll(env.statusRow(), "chat-exec-card");
const cardTitles = (env) => cardButtons(env).map((b) => textOf(findEl(b, "chat-exec-title")));
const cardDurs = (env) => cardButtons(env).map((b) => textOf(findEl(b, "chat-exec-dur")));

async function threeTerminalCalls(env) {
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  const cmds = ["pytest -q", "cat tests/test_security.py", "sed -i s/a/b/ astra/security.py"];
  cmds.forEach((cmd, i) => {
    env.event("tool.started", turn({ op: "tool-" + i, tool: "terminal_exec" }));
    env.event("terminal.started", { op: "term:p" + i, process_id: "p" + i,
                                    command: cmd, cwd: "/repo",
                                    stdout_blob_id: "out-p" + i,
                                    stderr_blob_id: "err-p" + i });
    env.event("terminal.completed", { op: "term:p" + i, process_id: "p" + i,
                                      command: cmd, cwd: "/repo",
                                      status: "completed", exit_code: 0,
                                      duration: 100 * (i + 1), terminal: true });
    env.event("tool.completed", turn({ op: "tool-" + i, tool: "terminal_exec",
                                       duration_ms: 100 * (i + 1) }));
  });
}

/* ------------------------------------------------------------------ tests */

test("three terminal_exec calls render three separate cards under the live status", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests and fix the failure");
  await flush();
  await threeTerminalCalls(env);

  const cards = cardsIn(env);
  assert.strictEqual(cards.length, 3, "one card per terminal execution");
  assert.deepStrictEqual(cardTitles(env),
    ["Run tests", "Read test_security.py", "Edit security.py"]);
  assert.deepStrictEqual(cardDurs(env), ["100ms", "200ms", "300ms"]);
  assert.ok(cardButtons(env).every((b) => textOf(b).charAt(0) === "\u2713"),
            "completed cards show a check mark");

  // the compact live status is still there, ABOVE the cards
  const row = env.statusRow();
  const statusBtn = findEl(row, "chat-status");
  assert.ok(statusBtn, "the compact status button survives");
  const cardsBox = findEl(row, "chat-cards");
  assert.ok(cardsBox, "the cards container lives in the status row");
  assert.strictEqual(cardsBox.children.length, 3,
                     "one card element per terminal execution");
  const order = [];
  (function walk(node) {
    if (node === statusBtn) order.push("status");
    if (node === cardsBox) order.push("cards");
    (node.children || []).forEach(walk);
  })(row);
  assert.deepStrictEqual(order, ["status", "cards"],
                         "cards render underneath the compact status");
  // three separate card DOM elements, never one merged timeline row
  assert.strictEqual(findAll(row, "chat-exec-card").length, 3);
});

test("terminal.started opens a running card and completion closes the SAME card", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("tool.started", turn({ op: "tool-1", tool: "terminal_exec" }));
  env.event("terminal.started", { op: "term:p1", process_id: "p1",
                                  command: "pytest -q", cwd: "/repo",
                                  stdout_blob_id: "out-p1" });
  let cards = cardsIn(env);
  assert.strictEqual(cards.length, 1);
  assert.ok(findEl(cards[0], "chat-exec-card").classList.contains("running"));
  assert.strictEqual(cards[0].classList.contains("open"), false);

  env.event("terminal.completed", { op: "term:p1", process_id: "p1",
                                    status: "completed", exit_code: 0,
                                    duration: 61800, terminal: true });
  cards = cardsIn(env);
  assert.strictEqual(cards.length, 1, "completion must not add a card");
  assert.ok(findEl(cards[0], "chat-exec-card").classList.contains("completed"));
  assert.strictEqual(cardDurs(env)[0], "61.8s");
});

test("a failed command marks the same card failed, never the live status", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("tool.started", turn({ op: "tool-1", tool: "terminal_exec" }));
  env.event("terminal.started", { op: "term:p1", process_id: "p1",
                                  command: "pytest -q" });
  env.event("terminal.failed", { op: "term:p1", process_id: "p1",
                                 status: "failed", exit_code: 1,
                                 duration: 900, terminal: true });
  assert.strictEqual(cardsIn(env).length, 1);
  assert.ok(findEl(cardsIn(env)[0], "chat-exec-card").classList.contains("failed"));
  assert.strictEqual(env.statusLine(), "Terminal command failed");
});

test("terminal.output updates only its own card", async () => {
  const env = await makeEnvironment();
  await env.submit("Run them");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("terminal.started", { op: "term:p1", process_id: "p1", command: "pytest" });
  env.event("terminal.started", { op: "term:p2", process_id: "p2", command: "npm test" });
  env.event("terminal.output", { op: "term:p2", process_id: "p2",
                                 stream: "stdout", chars: 12,
                                 snippet: "2 passing", status: "running" });
  const cards = cardsIn(env);
  assert.strictEqual(cards.length, 2, "each terminal start -> its own card");
  const subs = cards.map((c) => textOf(findEl(c, "chat-exec-sub")));
  assert.deepStrictEqual(subs, ["pytest", "npm test"]);
});

test("browser/file/web3 cards never merge with terminal cards", async () => {
  const env = await makeEnvironment();
  await env.submit("Investigate");
  await flush();
  env.event("chat.pipeline.started", turn({ op: "chat:req-1" }));
  env.event("tool.started", turn({ op: "t1", tool: "terminal_exec" }));
  env.event("terminal.started", { op: "term:p1", process_id: "p1", command: "pytest" });
  env.event("tool.started", turn({ op: "t2", tool: "browser_open" }));
  env.event("tool.started", turn({ op: "t3", tool: "read_file" }));
  env.event("tool.started", turn({ op: "t4", tool: "search_web" }));
  env.event("tool.started", turn({ op: "t5", tool: "token_balance" }));
  const cards = cardsIn(env);
  assert.strictEqual(cards.length, 5);
  assert.deepStrictEqual(
    cards.map((c) => String(c.className).split(/\s+/)[1]),
    ["terminal", "browser", "file", "web", "web3"]);
});

test("tapping a card only opens read-only detail and executes nothing", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  await threeTerminalCalls(env);
  const card = cardsIn(env)[0];
  const callsBefore = env.calls.length;
  const sendDisabled = env.doc.getElementById("chat-send").disabled;

  await findEl(card, "chat-exec-card").fire("click", {});
  const detail = findEl(card, "chat-exec-detail");
  assert.strictEqual(detail.hidden, false, "the detail panel opens");
  assert.strictEqual(card.classList.contains("open"), true);
  const text = textOf(detail);
  assert.ok(text.includes("Run tests"), text);
  assert.ok(text.includes("pytest -q"), text);
  assert.ok(text.includes("/repo"), text);
  assert.ok(text.includes("Completed"), text);
  assert.ok(text.includes("100ms"), text);
  // clicking NEVER runs a tool or re-issues the request
  assert.strictEqual(env.calls.length, callsBefore, "no request from a tap");
  assert.strictEqual(env.doc.getElementById("chat-send").disabled, sendDisabled);
  assert.strictEqual(env.calls.filter((c) => c.url === "/api/chat").length, 1);

  await findEl(card, "chat-exec-card").fire("click", {});
  assert.strictEqual(detail.hidden, true, "tapping again collapses it");
});

test("'load full output' pages the read-only endpoint, never re-runs the command", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  await threeTerminalCalls(env);
  const card = cardsIn(env)[0];
  await findEl(card, "chat-exec-card").fire("click", {});

  const before = env.calls.filter((c) => c.url.startsWith("/api/terminal/output")).length;
  assert.strictEqual(before, 0, "expanding alone fetches nothing");

  const more = findEl(card, "chat-exec-more");
  assert.ok(more, "terminal cards offer full output");
  await more.fire("click", {});
  await flush();
  const gets = env.calls.filter((c) => c.url.startsWith("/api/terminal/output"));
  assert.strictEqual(gets.length, 1, "exactly one paged GET");
  assert.strictEqual(gets[0].method, "GET");
  assert.ok(gets[0].url.includes("blob_id=out-p0"), gets[0].url);
  assert.ok(!gets[0].url.includes("process_id="), "the blob id wins");
  const pre = findEl(card, "chat-exec-pre");
  assert.match(pre.textContent, /^0123456789/);
});

test("a new turn starts with a clean card list", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  await threeTerminalCalls(env);
  env.event("chat.pipeline.finished", turn({ op: "chat:req-1", terminal: true }));
  env.resolveChat({ ok: true, data: { reply: "Done.", action: "none" } });
  await flush();
  assert.strictEqual(cardsIn(env).length, 3, "the finished turn keeps its cards");

  await env.submit("Now do something else");
  await flush();
  assert.strictEqual(cardsIn(env).length, 0, "a new turn owns a fresh list");
  env.event("chat.pipeline.started", turn({ op: "chat:req-2", request: "req-2",
                                            trace: "req-2" }));
  env.event("terminal.started", { op: "term:q1", process_id: "q1", command: "ls" });
  assert.strictEqual(cardsIn(env).length, 1);
  assert.strictEqual(cardTitles(env)[0], "List files");
});

test("cards never carry an inline width and keep readable text", async () => {
  const env = await makeEnvironment();
  await env.submit("Run the tests");
  await flush();
  await threeTerminalCalls(env);
  for (const card of cardsIn(env)) {
    const btn = findEl(card, "chat-exec-card");
    assert.strictEqual(btn.style.width, undefined,
                       "no inline width: the stylesheet owns the mobile layout");
    assert.ok(findEl(btn, "chat-exec-title"));
    assert.ok(textOf(btn).length > 0);
  }
});

/* --------------------------------------------------------- image artifacts ---
 * The generated image must render as an image card (not a generic file card)
 * with Open + Download, driven by the artifact dict the server actually
 * sends (astra.core.artifacts.Artifact.to_dict: artifact_type / mime_type).
 */
test("a generated image artifact renders as an image card with Open + Download", async () => {
  const env = await makeEnvironment();
  await env.submit("akta chobi banao");
  await flush();
  env.resolveChat({ ok: true, data: {
    reply: "Image ta generate hoyeche - niche dekho.",
    action: "none",
    artifacts: [{ id: "img1", filename: "generated.png",
                  artifact_type: "image", mime_type: "image/png",
                  size: 208, validated: true }],
  } });
  await flush();

  const cards = findAll(env.chatLog(), "artifact-card");
  assert.strictEqual(cards.length, 1, "exactly one artifact card");
  const html = cards[0].innerHTML;
  const url = "/api/v1/artifacts/img1/generated.png";
  assert.match(html, /<img class="artifact-image"/,
               "the artifact renders as an <img>, not a file row");
  assert.match(html, new RegExp('src="' + url + '"'));
  assert.match(html, />Open</, "an Open link is offered");
  assert.match(html, /target="_blank"/, "Open loads the full image in a new tab");
  assert.match(html, new RegExp('href="' + url + '" download="generated\\.png"'),
               "a Download link is offered");
  // the reply bubble stays short: the payload is the artifact, not the text
  assert.doesNotMatch(textOf(env.chatLog()), /base64/);
});
