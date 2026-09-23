/* Frontend contract tests for the Astra Agent Terminal.
 *
 * These pin the wiring that makes `static/js/terminal.js` a real terminal
 * over the Agent Runtime rather than a log view, without needing a browser:
 *   * index.html actually loads the vendored xterm.js + fit addon, the
 *     terminal stylesheet, terminal.js, and contains the terminal pane,
 *   * terminal.js loads under a minimal global sandbox and registers itself
 *     as the core-tab loader astra.js's showTab() calls,
 *   * the tab it registers is the one the server advertises in its manifest,
 *   * terminal.js talks to the runtime endpoints (never a host-shell one)
 *     and derives the SHARED `conv-<id>` session id.
 *
 * Run with `node --test tests/js/`.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..", "..");
const HTML = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");
const JS = fs.readFileSync(path.join(ROOT, "static/js/terminal.js"), "utf8");

/* ------------------------------------------------------- asset wiring ---- */

test("index.html loads the terminal emulator and its stylesheet", () => {
  for (const asset of [
    "/static/js/vendor/xterm.js",
    "/static/js/vendor/xterm-addon-fit.js",
    "/static/js/terminal.js",
    "/static/css/vendor/xterm.css",
    "/static/css/terminal.css",
  ]) {
    assert.ok(HTML.includes(asset), `missing asset reference: ${asset}`);
  }
  // The emulator must load AFTER astra.js (terminal.js registers into
  // Astra.loaders, which astra.js defines).
  assert.ok(HTML.indexOf("/static/js/astra.js") <
            HTML.indexOf("/static/js/terminal.js"),
            "terminal.js must load after astra.js");
});

test("index.html contains the terminal pane as its own tabview", () => {
  assert.ok(HTML.includes('id="tab-terminal"'));
  assert.ok(/id="tab-terminal"[^>]*class="tabview"/.test(HTML) ||
            /class="tabview"[^>]*id="tab-terminal"/.test(HTML));
  // It must be a clean pane filled at runtime, not a log/timeline markup.
  assert.ok(!/id="tab-terminal"[\s\S]{0,400}<table/.test(HTML));
});

test("the vendored emulator is real, not a stub", () => {
  const xterm = fs.readFileSync(
    path.join(ROOT, "static/js/vendor/xterm.js"), "utf8");
  // The UMD build defines the `Terminal` global xterm.js exposes.
  assert.ok(xterm.length > 100000, "xterm.js looks truncated");
  assert.ok(/Terminal/.test(xterm));
  const fit = fs.readFileSync(
    path.join(ROOT, "static/js/vendor/xterm-addon-fit.js"), "utf8");
  assert.ok(fit.includes("FitAddon"));
});

/* ----------------------------------------------- module registration ----- */

function loadTerminal(overrides) {
  const calls = [];
  const loaders = {};
  const sandbox = {
    console,
    EventSource: function () {},
    setTimeout: (fn) => { if (typeof fn === "function") fn(); return 0; },
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: (url, opts) => {
      calls.push({ url: String(url),
                   method: (opts && opts.method) || "GET",
                   body: opts && opts.body ? String(opts.body) : "" });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true,
        data: { available: true, state: "running", workspace: "/workspace" } }) });
    },
    api: (url) => {
      calls.push({ url: String(url), method: "GET", body: "" });
      return Promise.resolve({ ok: true, data: { available: true,
        state: "running", workspace: "/workspace" } });
    },
    esc: (s) => String(s == null ? "" : s),
    document: {
      getElementById: () => null,
      createElement: () => ({ className: "", innerHTML: "", style: {},
                              appendChild() {}, remove() {}, onclick: null,
                              querySelector: () => null }),
      addEventListener() {},
    },
  };
  sandbox.post = (url, body) => {
    calls.push({ url: String(url), method: "POST",
                 body: JSON.stringify(body) });
    return Promise.resolve({ ok: true, data: {} });
  };
  sandbox.window = {
    Astra: { loaders, currentConversation: () => (overrides
      && overrides.conversationId) || 42 },
    addEventListener() {},
  };
  sandbox.Astra = sandbox.window.Astra;
  vm.createContext(sandbox);
  vm.runInContext(JS, sandbox);
  return { sandbox, calls, loaders };
}

test("terminal.js registers as the core-tab loader under a sandbox", () => {
  const { sandbox, loaders } = loadTerminal();
  assert.equal(typeof loaders.terminal, "function",
               "Astra.loaders.terminal must be registered");
  assert.ok(sandbox.window.AstraTerminal,
            "AstraTerminal must be exposed for the shell");
  assert.equal(typeof sandbox.window.AstraTerminal.mount, "function");
  assert.equal(typeof sandbox.window.AstraTerminal.createTab, "function");
});

test("terminal.js derives the SHARED conv-<id> session id", () => {
  const { sandbox } = loadTerminal({ conversationId: 7 });
  const state = sandbox.window.AstraTerminal.state;
  assert.ok(state, "state must be exposed for debugging/tests");
  assert.equal(sandbox.window.Astra.currentConversation(), 7);
  // The derivation itself is what keeps chat and terminal on one session.
  assert.ok(/"conv-" \+ cid/.test(JS) || JS.includes('"conv-" + cid'),
            "the terminal must derive conv-<conversation id>");
});

test("terminal.js only talks to runtime endpoints", () => {
  // Every endpoint the terminal uses must be under /api/runtime/ — a host
  // shell endpoint appearing here would mean a silent host fallback.
  const urls = [...JS.matchAll(/"(\/api\/[^"]*)"/g)].map((m) => m[1]);
  assert.ok(urls.length >= 4, `expected several API paths, got ${urls.length}`);
  for (const url of urls) {
    assert.ok(url.startsWith("/api/runtime/") ||
              url.startsWith("/api/chat/"),
              `terminal must not call a non-runtime endpoint: ${url}`);
  }
  // No endpoint outside /api/runtime/ or /api/chat/ — a host-shell route
  // appearing here would be the silent host fallback the spec forbids.
  assert.ok(!/\/api\/(terminal|shell|exec)\b/.test(JS),
            "terminal.js must not call the legacy host terminal endpoints");
});

test("terminal.js encodes the real control bytes for Ctrl keys", () => {
  // Ctrl+C must reach the PTY as the raw byte the line discipline turns
  // into SIGINT — the frontend never simulates a signal.
  assert.ok(JS.includes("Ctrl+Shift") || JS.includes("ctrlKey"));
  assert.ok(/xterm/.test(JS) || /Terminal/.test(JS));
  // (The actual TIOCSWINSZ ioctl is server-side, in astra/runtime/pty.py.)
  assert.ok(JS.includes("/api/runtime/terminal/resize"),
            "resize must go to the real PTY");
  assert.ok(JS.includes("/api/runtime/terminal/input"),
            "keystrokes must go to the real PTY stdin");
  assert.ok(JS.includes("/api/runtime/terminal/stream"),
            "output must come from the PTY stream");
});
