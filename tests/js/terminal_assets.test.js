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
  // A browser global the paste tests need; never present in the sandbox by
  // default, so `navigator` stays undefined unless a test asks for it.
  if (overrides && overrides.navigator) sandbox.navigator = overrides.navigator;
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
  // Inside the MOBILE media block specifically - the stylesheet has more
  // than one @media, so the block must be selected by its query, not by
  // position.
  const mobile = CSS.split(/@media[^{]*max-width:\s*820px[^{]*\{/)[1] || "";
  assert.ok(/\.at-keys\s*\{[^}]*display:\s*flex/.test(mobile),
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

/* ----------------------- PC clipboard paste (mouse + keys) ------------- */

/* Paste is the one thing a terminal can quietly get wrong in a dangerous way:
 * paste that landed in a host shell would be the silent host fallback the
 * spec forbids. These tests drive the real handlers and pin that
 *   * a PC right-click pastes the clipboard (the browser's own menu cannot:
 *     its Paste item is greyed out over xterm's non-editable canvas),
 *   * the bytes go through xterm's paste() - i.e. onData -> the runtime's
 *     terminal/input route - and never a host-shell endpoint,
 *   * Android's touch long-press popup and the extra keys are untouched. */

function fakeTab() {
  const listeners = {};
  const pasted = [];
  const tab = {
    pasted,
    term: {
      paste: (text) => pasted.push(text),
      focusCount: 0,
      focus() { this.focusCount += 1; },
    },
    view: {
      addEventListener(type, fn) {
        (listeners[type] = listeners[type] || []).push(fn);
      },
    },
    fire(type, ev) {
      for (const fn of listeners[type] || []) fn(ev);
    },
  };
  return tab;
}

function mouseEvent(extra) {
  const ev = {
    shiftKey: false,
    prevented: 0,
    stopped: 0,
    preventDefault() { this.prevented += 1; },
    stopPropagation() { this.stopped += 1; },
  };
  return Object.assign(ev, extra || {});
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const clipboardOf = (text) => ({ navigator:
  { clipboard: { readText: async () => text } } });

test("right-click pastes the clipboard into the terminal", async () => {
  const { sandbox } = loadTerminal(clipboardOf("PASTED-TEXT"));
  const bind = sandbox.window.AstraTerminal._t.bindPasteGestures;
  assert.equal(typeof bind, "function", "bindPasteGestures must be exported");

  const tab = fakeTab();
  bind(tab);
  const ev = mouseEvent({ sourceCapabilities: { firesTouchEvents: false } });
  tab.fire("contextmenu", ev);
  await tick();

  assert.equal(ev.prevented, 1,
               "the browser menu must be suppressed and the paste done here");
  assert.deepEqual(tab.pasted, ["PASTED-TEXT"],
                   "the clipboard must reach xterm paste() -> PTY stdin");
});

test("a touch long-press keeps the browser's Copy popup (Android)", async () => {
  const { sandbox } = loadTerminal(clipboardOf("SHOULD-NOT-PASTE"));
  const { bindPasteGestures, isTouchGenerated } =
    sandbox.window.AstraTerminal._t;
  const tab = fakeTab();
  bindPasteGestures(tab);
  const ev = mouseEvent({ sourceCapabilities: { firesTouchEvents: true } });
  tab.fire("contextmenu", ev);
  await tick();

  assert.equal(ev.prevented, 0, "the native long-press menu must still open");
  assert.deepEqual(tab.pasted, [], "a long-press must never paste");
  assert.equal(isTouchGenerated({ sourceCapabilities:
    { firesTouchEvents: true } }), true);
  assert.equal(isTouchGenerated({ sourceCapabilities:
    { firesTouchEvents: false } }), false);
});

test("Shift+right-click still opens the browser's real menu", async () => {
  const { sandbox } = loadTerminal(clipboardOf("NOPE"));
  const tab = fakeTab();
  sandbox.window.AstraTerminal._t.bindPasteGestures(tab);
  const ev = mouseEvent({ shiftKey: true,
                          sourceCapabilities: { firesTouchEvents: false } });
  tab.fire("contextmenu", ev);
  await tick();
  assert.equal(ev.prevented, 0, "the escape hatch must stay native");
  assert.deepEqual(tab.pasted, []);
});

test("keyboard paste fallbacks: Ctrl+Shift+V and Shift+Insert", () => {
  const { sandbox } = loadTerminal();
  const chord = sandbox.window.AstraTerminal._t.isPasteChord;
  const key = (k, mods) => Object.assign({ type: "keydown", key: k }, mods);
  assert.equal(chord(key("v", { ctrlKey: true, shiftKey: true })), true,
               "Ctrl+Shift+V is paste");
  assert.equal(chord(key("V", { ctrlKey: true, shiftKey: true })), true);
  assert.equal(chord(key("Insert", { shiftKey: true })), true,
               "Shift+Insert is paste");
  // A bare Ctrl+V is quoted-insert and must still reach the PTY as a byte,
  // and Shift+Insert alone (no shift) is the plain Insert escape.
  assert.equal(chord(key("v", { ctrlKey: true })), false);
  assert.equal(chord(key("v", { shiftKey: true })), false);
  assert.equal(chord(key("Insert", {})), false);
  assert.equal(chord({ type: "keyup", key: "Insert", shiftKey: true }), false);
});

test("paste is wired to the runtime PTY, never to a host shell", () => {
  // Every tab gets the mouse gesture...
  assert.ok(/bindPasteGestures\(tab\)/.test(JS),
            "createTab must bind the mouse-paste gesture");
  // ...the browser menu is suppressed on the terminal view...
  assert.ok(/addEventListener\("contextmenu"/.test(JS));
  assert.ok(/if \(isPasteChord\(ev\)\) \{ term\.focus\(\); return false; \}/
              .test(JS),
            "the paste chords must be handed to the browser, not encoded");
  // ...the long-press suppression is gated to touch devices, so it can never
  // eat a PC's right-click again...
  assert.ok(/if \(isTouchInput\(\)\)[\s\S]{0,60}?contextmenu/.test(JS),
            "the long-press suppression must be touch-only");
  // ...and paste goes out through the runtime's own route.
  assert.ok([...JS.matchAll(/\.paste\(/g)].length >= 2,
            "paste must use xterm's paste()");
  assert.ok(JS.includes("/api/runtime/terminal/input"),
            "paste bytes must reach the PTY stdin route");
  assert.ok(!/\/api\/(terminal|shell|exec)\b/.test(JS),
            "no host-shell paste route may exist");
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

test("Ctrl+C copies only while text is selected; otherwise stays ^C", () => {
  const { sandbox } = loadTerminal();
  const { isCopyChord } = sandbox.window.AstraTerminal._t;
  const key = (k, mods) => Object.assign({ type: "keydown", key: k }, mods);
  const sel = { hasSelection: () => true };
  const none = { hasSelection: () => false };
  assert.equal(isCopyChord(key("c", { ctrlKey: true }), sel), true);
  assert.equal(isCopyChord(key("c", { ctrlKey: true }), none), false,
               "no selection: Ctrl+C must still reach the PTY as SIGINT");
  assert.equal(isCopyChord(key("C", { ctrlKey: true, shiftKey: true }), none), true);
  assert.equal(isCopyChord(key("c", {}), sel), false);
});

test("Ctrl+A selects all terminal text", () => {
  const { sandbox } = loadTerminal();
  const { isSelectAllChord } = sandbox.window.AstraTerminal._t;
  const key = (k, mods) => Object.assign({ type: "keydown", key: k }, mods);
  assert.equal(isSelectAllChord(key("a", { ctrlKey: true })), true);
  assert.equal(isSelectAllChord(key("a", {})), false);
  assert.equal(isSelectAllChord(key("a", { ctrlKey: true, shiftKey: true })), false);
});
