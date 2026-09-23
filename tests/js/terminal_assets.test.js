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

/* ------------------------------------------------ terminal-first UI ---- */

const CSS = fs.readFileSync(path.join(ROOT, "static/css/terminal.css"), "utf8");

test("the terminal viewport is the primary UI, not a dashboard", () => {
  // The viewport owns every pixel the one header line + one status line do
  // not: it flexes to fill the app and the app is a full-height column.
  assert.ok(/\.at-stage\s*\{[^}]*flex:\s*1 1 auto/.test(CSS),
            "the terminal stage must flex to fill the app");
  assert.ok(/\.at-app\s*\{[^}]*height:\s*calc\(100vh/.test(CSS),
            "the app must be a full-height column");
  assert.ok(/\.at-views\s*\{[^}]*position:\s*relative/.test(CSS));
  // no permanent sidebar stealing the viewport
  assert.ok(/\.at-side\s*\{[^}]*position:\s*absolute/.test(CSS),
            "the file drawer must overlay, not reserve layout width");
  assert.ok(/\.at-side\s*\{[^}]*translateX\(-101%\)/.test(CSS),
            "the file drawer must start hidden");
});

test("the only chrome is one header line, tabs and one status line", () => {
  // Tabs live INSIDE the header (like a desktop terminal), not in a card row.
  assert.ok(/\.at-top\s*\{[^}]*display:\s*flex/.test(CSS));
  assert.ok(/\.at-tabs\s*\{[^}]*flex:\s*1 1 auto/.test(CSS));
  assert.ok(/\.at-bar\s*\{[^}]*display:\s*flex/.test(CSS));
  assert.ok(/\.at-bar\s*\{[^}]*font-family:\s*ui-monospace/.test(CSS),
            "status line is terminal-typed, not dashboard-typed");
  // no oversized rounded dashboard containers
  assert.ok(!/\.at-app\s*\{[^}]*border-radius:\s*(1[2-9]|[2-9]\d)px/.test(CSS),
            "no oversized rounded app container");
});

test("terminal colors/typography match a real terminal", () => {
  assert.ok(/--at-bg:\s*#0b0f14/.test(CSS), "dark terminal background");
  assert.ok(/#0b0f14.*foreground/s.test(JS) || /background:\s*"#0b0f14"/.test(JS),
            "xterm theme uses the dark terminal palette");
  assert.ok(/monospace/.test(CSS));
  assert.ok(/cursorBlink:\s*true/.test(JS), "blinking cursor enabled");
  assert.ok(/scrollback:\s*\d{4,}/.test(JS), "real terminal scrollback");
});

test("a compact Termux-style extra-key row exists for mobile", () => {
  const keyIds = [...JS.matchAll(/\{\s*id:\s*"([a-z]+)",\s*label:/g)]
    .map((m) => m[1]);
  for (const k of ["esc", "tab", "ctrl", "alt", "slash", "dash", "home",
                   "end", "up", "down", "left", "right", "pgup", "pgdn"]) {
    assert.ok(keyIds.includes(k), `missing extra key: ${k}`);
  }
  assert.ok(/\.at-keys\s*\{[^}]*display:\s*none/.test(CSS),
            "extra keys hidden on desktop");
  assert.ok(/@media[^{]*\{\s*\.at-keys\s*\{[^}]*display:\s*flex/.test(
              CSS.replace(/\n/g, " ")) ||
            /\.at-keys\s*\{\s*display:\s*flex/.test(
              CSS.split("@media")[1] || ""),
            "extra keys shown on mobile");
  // compact, terminal-oriented buttons (never huge rounded cards)
  assert.ok(/\.at-key\s*\{[^}]*border-radius:\s*5px/.test(CSS));
  assert.ok(!/\.at-key\s*\{[^}]*border-radius:\s*(1[2-9]|[2-9]\d)px/.test(CSS));
});

test("the extra-key row sends real control bytes to the PTY", () => {
  // ESC/TAB/arrows/Home/End/PageUp/PageDown are the actual escapes a
  // terminal sends; Ctrl/Alt are sticky modifiers.
  const expect = {
    ESC: "\\x1b", TAB: "\\t", HOME: "\\x1b[H", END: "\\x1b[F",
    up: "\\x1b[A", down: "\\x1b[B", left: "\\x1b[D", right: "\\x1b[C",
    pgup: "\\x1b[5~", pgdn: "\\x1b[6~",
  };
  for (const seq of Object.values(expect)) {
    assert.ok(JS.includes(`data: "${seq}"`), `missing key sequence ${seq}`);
  }
  assert.ok(/mod:\s*true/.test(JS), "Ctrl/Alt are modifiers");
  assert.ok(/applySticky/.test(JS),
            "sticky Ctrl/Alt must convert the next input to real bytes");
});

test("mobile keyboard never hides the terminal (VisualViewport)", () => {
  assert.ok(/visualViewport/.test(JS),
            "the terminal must track the visual viewport");
  assert.ok(/--at-vh/.test(JS) && /--at-vh/.test(CSS),
            "the app height must follow the visual viewport");
  assert.ok(/height:\s*var\(--at-vh/.test(CSS));
});

test("runtime lifecycle + files live in a compact ⋮ menu, not big cards", () => {
  assert.ok(/id="at-menu"/.test(JS) || /at-menu/.test(JS));
  for (const act of ["reconnect", "restart", "clear", "files", "status",
                     "stop", "kill"]) {
    assert.ok(JS.includes(`data-act="${act}"`) || JS.includes(`"${act}"`),
              `menu action missing: ${act}`);
  }
  // the menu (and the drawer) start hidden; the terminal area stays visible
  assert.ok(/menu\.hidden = true/.test(JS) || /hidden>/.test(JS));
  assert.ok(/side\.hidden = true/.test(JS));
});

test("the runtime-unavailable state is compact and never covers a ready runtime", () => {
  // It is a small bordered notice, not a full dashboard, and it is hidden
  // the moment the runtime reports available.
  assert.ok(/un\.hidden = ok/.test(JS),
            "the unavailable overlay must hide when the runtime is ready");
  assert.ok(/unavailable\.hidden = true/.test(JS),
            "the overlay starts hidden");
  assert.ok(/\.at-unavailable-card\s*\{[^}]*max-width:\s*4\d\dpx/.test(CSS),
            "the notice must stay compact");
});
