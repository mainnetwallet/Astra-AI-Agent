/* Astra Agent Terminal — a real PC/mobile terminal for Astra.
 *
 * This is not a log viewer. The pane is a genuine terminal emulator
 * (xterm.js, vendored in static/js/vendor/) attached to a REAL PTY running
 * inside the isolated Agent Runtime (astra/runtime/pty.py):
 *
 *   keystroke -> /api/runtime/terminal/input  -> PTY stdin
 *   PTY stdout -> /api/runtime/terminal/stream (SSE) -> xterm.write()
 *   browser resize -> /api/runtime/terminal/resize -> TIOCSWINSZ on the PTY
 *
 * Nothing here converts output into DOM log rows, and nothing here can
 * reach a host shell: every endpoint targets the runtime.
 *
 * The layout is intentionally the thin chrome a desktop terminal ships — one
 * header line holding the brand, the tabs and a ⋮ menu, the terminal
 * viewport taking everything else, and a single status line. The file
 * drawer and the runtime lifecycle actions live behind the ⋮ menu, so the
 * real terminal is always the primary UI. On phones this becomes a
 * Termux-style full-screen terminal with a compact extra-key row.
 */
(function () {
  "use strict";

  const MAX_TABS = 8;

  const API = {
    status: () => api("/api/runtime/status"),
    lifecycle: (action, extra) => post("/api/runtime/lifecycle",
      Object.assign({ action }, extra || {})),
    terminals: () => api("/api/runtime/terminals"),
    open: (session_id, rows, cols, title) =>
      post("/api/runtime/terminal/open", { session_id, rows, cols, title }),
    input: (session_id, data) =>
      post("/api/runtime/terminal/input", { session_id, data }),
    resize: (session_id, rows, cols) =>
      post("/api/runtime/terminal/resize", { session_id, rows, cols }),
    close: (session_id) => post("/api/runtime/terminal/close", { session_id }),
    files: (path) =>
      api("/api/runtime/files?path=" + encodeURIComponent(path || "/workspace")),
    fileAction: (body) => post("/api/runtime/files", body),
    upload: (form) => fetch("/api/runtime/upload",
      { method: "POST", body: form }).then((r) => r.json()),
  };

  // The first tab attaches to the conversation's session id (`conv-<id>`),
  // the same id the chat agent's runtime tools use — so the terminal and the
  // agent share one shell, one cwd and one filesystem.
  function conversationSessionId() {
    try {
      const cid = window.Astra && Astra.currentConversation
        ? Astra.currentConversation() : null;
      if (cid) return "conv-" + cid;
    } catch (_) { /* fall through */ }
    return "";
  }

  function esc(v) {
    if (window.esc) { try { return window.esc(v); } catch (_) {} }
    return String(v == null ? "" : v);
  }

  const state = {
    mounted: false,
    available: false,
    runtime: null,
    tabs: new Map(),      // sessionId -> tab record
    order: [],            // sessionId order
    active: "",
    sidebarPath: "/workspace",
    editorPath: "",
    sidebarOpen: false,
    statusTimer: null,
  };

  /* ------------------------------- DOM ---------------------------------- */

  function h(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function paneEl() { return document.getElementById("tab-terminal"); }
  function byId(id) { return document.getElementById(id); }

  function buildLayout(root) {
    root.innerHTML = "";
    root.classList.add("at-app");

    // -- one compact header line: brand | tabs | actions -------------------
    const top = h("header", "at-top");
    top.innerHTML = `
      <div class="at-brand">
        <span class="at-brand-mark" aria-hidden="true">&gt;_</span>
        <span>Astra Agent Terminal</span>
        <span class="at-brand-sub" id="at-sub">connecting…</span>
      </div>
      <div class="at-tabs" id="at-tabs" role="tablist" aria-label="Terminal sessions"></div>
      <div class="at-actions">
        <button class="at-icon" id="at-new" title="New terminal (Ctrl+Shift+T)" aria-label="New terminal">+</button>
        <button class="at-icon" id="at-files" title="Workspace files" aria-label="Toggle workspace files" aria-expanded="false">▤</button>
        <button class="at-icon" id="at-menu-btn" title="More" aria-label="More actions" aria-haspopup="true" aria-expanded="false">⋮</button>
      </div>
      <div class="at-menu" id="at-menu" role="menu" hidden>
        <button role="menuitem" data-act="reconnect">Reconnect <span class="at-menu-hint">same session</span></button>
        <button role="menuitem" data-act="restart">Restart session</button>
        <button role="menuitem" data-act="clear">Clear screen <span class="at-menu-hint">Ctrl+L</span></button>
        <button role="menuitem" data-act="files">Workspace files <span class="at-menu-hint">▤</span></button>
        <hr>
        <button role="menuitem" data-act="status">Runtime status</button>
        <button role="menuitem" data-act="runtime-restart">Restart runtime</button>
        <button role="menuitem" data-act="stop" class="at-danger">Stop runtime</button>
        <button role="menuitem" data-act="kill" class="at-danger">Kill this session</button>
      </div>`;
    root.appendChild(top);

    // -- terminal viewport -------------------------------------------------
    const stage = h("div", "at-stage");
    stage.id = "at-stage";
    const views = h("div", "at-views");
    views.id = "at-views";
    stage.appendChild(views);

    const unavailable = h("div", "at-unavailable");
    unavailable.id = "at-unavailable";
    unavailable.hidden = true;
    unavailable.innerHTML = `
      <div class="at-unavailable-card">
        <b>Agent Runtime unavailable</b>
        <p id="at-unavailable-msg"></p>
        <p>$ Astra will not run Agent work on the host terminal — there is
          no silent fallback.</p>
        <div class="at-unavailable-actions">
          <button class="at-btn at-primary" id="at-unavailable-retry">Check again</button>
        </div>
      </div>`;
    stage.appendChild(unavailable);

    // -- optional file drawer (hidden until asked for) ----------------------
    const side = h("aside", "at-side");
    side.id = "at-side";
    side.hidden = true;
    side.setAttribute("aria-label", "Runtime workspace files");
    side.innerHTML = `
      <div class="at-side-head">
        <span class="at-side-title">Files</span>
        <span class="at-side-path" id="at-side-path">/workspace</span>
        <button class="at-icon" id="at-side-close" aria-label="Close files">×</button>
      </div>
      <div class="at-side-actions">
        <button class="at-btn" id="at-up" title="Parent directory">↑</button>
        <button class="at-btn" id="at-refresh" title="Refresh">⟳</button>
        <button class="at-btn" id="at-newfile" title="New file">+file</button>
        <button class="at-btn" id="at-newdir" title="New folder">+dir</button>
        <button class="at-btn" id="at-upload" title="Upload into the runtime">↑file</button>
        <input type="file" id="at-upload-input" class="at-hidden" multiple>
      </div>
      <div class="at-side-list" id="at-side-list" role="tree"></div>
      <div class="at-editor" id="at-editor" hidden>
        <div class="at-editor-head">
          <span id="at-editor-path"></span>
          <span>
            <button class="at-btn" id="at-editor-save">Save</button>
            <button class="at-btn" id="at-editor-close">Close</button>
          </span>
        </div>
        <textarea id="at-editor-text" spellcheck="false"></textarea>
      </div>`;
    stage.appendChild(side);
    root.appendChild(stage);

    // -- mobile extra-key row (Termux-style) --------------------------------
    const keys = h("div", "at-keys");
    keys.id = "at-keys";
    keys.setAttribute("aria-label", "Terminal extra keys");
    EXTRA_KEYS.forEach((spec) => {
      const b = h("button", "at-key" + (spec.mod ? " at-key-mod" : ""),
        esc(spec.label));
      b.type = "button";
      b.dataset.key = spec.id;
      b.title = spec.title;
      keys.appendChild(b);
    });
    root.appendChild(keys);

    // -- one compact status line -------------------------------------------
    const bar = h("footer", "at-bar");
    bar.innerHTML = `
      <span class="at-led" id="at-led" aria-hidden="true"></span>
      <span class="at-bar-item" id="at-conn" role="status" aria-live="polite">connecting</span>
      <span class="at-sep">·</span>
      <span class="at-bar-item" id="at-bar-runtime">Agent Runtime</span>
      <span class="at-sep">·</span>
      <span class="at-bar-item" id="at-bar-shell">bash</span>
      <span class="at-sep">·</span>
      <span class="at-bar-item at-cwd" id="at-bar-cwd">/workspace</span>
      <span class="at-sep">·</span>
      <span class="at-bar-item" id="at-bar-session">session —</span>`;
    root.appendChild(bar);

    wire();
  }

  /* --------------------------- extra keys -------------------------------- */

  const EXTRA_KEYS = [
    { id: "esc", label: "ESC", data: "\x1b", title: "Escape" },
    { id: "tab", label: "TAB", data: "\t", title: "Tab" },
    { id: "ctrl", label: "CTRL", mod: true, title: "Control (sticky)" },
    { id: "alt", label: "ALT", mod: true, title: "Alt (sticky)" },
    { id: "slash", label: "/", data: "/", title: "Slash" },
    { id: "dash", label: "-", data: "-", title: "Dash" },
    { id: "home", label: "HOME", data: "\x1b[H", title: "Home" },
    { id: "end", label: "END", data: "\x1b[F", title: "End" },
    { id: "up", label: "↑", data: "\x1b[A", title: "Arrow up" },
    { id: "down", label: "↓", data: "\x1b[B", title: "Arrow down" },
    { id: "left", label: "←", data: "\x1b[D", title: "Arrow left" },
    { id: "right", label: "→", data: "\x1b[C", title: "Arrow right" },
    { id: "pgup", label: "PGUP", data: "\x1b[5~", title: "Page up" },
    { id: "pgdn", label: "PGDN", data: "\x1b[6~", title: "Page down" },
  ];

  function activeTab() { return state.tabs.get(state.active) || null; }

  function pressKey(id) {
    const tab = activeTab();
    if (!tab) return;
    const spec = EXTRA_KEYS.find((k) => k.id === id);
    if (!spec) return;
    if (spec.mod) {                 // CTRL / ALT are sticky toggles
      tab.sticky = tab.sticky || { ctrl: false, alt: false };
      tab.sticky[id] = !tab.sticky[id];
      renderKeyMods(tab);
      if (tab.term) tab.term.focus();
      return;
    }
    sendInput(tab.sessionId, spec.data);
    if (tab.term) tab.term.focus();
  }

  function renderKeyMods(tab) {
    const row = byId("at-keys");
    if (!row) return;
    const sticky = (tab && tab.sticky) || {};
    row.querySelectorAll(".at-key-mod").forEach((b) => {
      const on = !!sticky[b.dataset.key];
      b.classList.toggle("at-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* Turns a sticky Ctrl/Alt into real bytes for the NEXT input, whatever
   * produced it (physical key or the Android soft keyboard). */
  function applySticky(tab, data) {
    if (!tab || !tab.sticky || (!tab.sticky.ctrl && !tab.sticky.alt)) {
      return data;
    }
    let out = data;
    if (tab.sticky.ctrl && out.length === 1) {
      const code = out.toUpperCase().charCodeAt(0);
      if (code >= 64 && code < 128) out = String.fromCharCode(code - 64);
      else if (out === " ") out = "\x00";
      else if (out === "?") out = "\x7f";
    }
    if (tab.sticky.alt) out = "\x1b" + out;
    tab.sticky = { ctrl: false, alt: false };
    renderKeyMods(tab);
    return out;
  }

  /* ----------------------------- wiring ---------------------------------- */

  function wire() {
    byId("at-new").onclick = () => createTab();
    byId("at-files").onclick = () => toggleSidebar();
    byId("at-side-close").onclick = () => toggleSidebar(false);
    byId("at-unavailable-retry").onclick = () => refreshStatus(true);

    const menuBtn = byId("at-menu-btn");
    const menu = byId("at-menu");
    menuBtn.onclick = (ev) => {
      ev.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    };
    document.addEventListener("click", (ev) => {
      if (!menu.hidden && !menu.contains(ev.target) && ev.target !== menuBtn) {
        menu.hidden = true;
        menuBtn.setAttribute("aria-expanded", "false");
      }
    });
    menu.onclick = (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      menu.hidden = true;
      menuBtn.setAttribute("aria-expanded", "false");
      menuAction(btn.dataset.act);
    };

    const keys = byId("at-keys");
    keys.addEventListener("pointerdown", (ev) => {
      const b = ev.target.closest(".at-key");
      if (!b) return;
      ev.preventDefault();
      pressKey(b.dataset.key);
    });

    byId("at-up").onclick = () => {
      const p = state.sidebarPath;
      if (p === "/workspace" || p === "/") return;
      const parent = p.replace(/\/[^/]+$/, "") || "/workspace";
      state.sidebarPath = (parent.startsWith("/workspace") || parent === "/")
        ? parent : "/workspace";
      listFiles();
    };
    byId("at-refresh").onclick = () => listFiles();
    byId("at-newfile").onclick = () => newEntry("file");
    byId("at-newdir").onclick = () => newEntry("dir");
    const uploadInput = byId("at-upload-input");
    byId("at-upload").onclick = () => uploadInput.click();
    uploadInput.onchange = () => doUpload(uploadInput);
    byId("at-editor-close").onclick = () => { byId("at-editor").hidden = true; };
    byId("at-editor-save").onclick = () => saveEditor();
  }

  function menuAction(act) {
    const tab = activeTab();
    if (act === "reconnect") return reconnectActive();
    if (act === "restart") return restartActive();
    if (act === "clear") {
      if (tab && tab.term) { tab.term.clear(); tab.term.focus(); }
      return;
    }
    if (act === "files") return toggleSidebar();
    if (act === "status") return showRuntimeStatus();
    if (act === "runtime-restart") return lifecycle("restart");
    if (act === "stop") return lifecycle("stop");
    if (act === "kill") return killActive();
  }

  function toggleSidebar(force) {
    const side = byId("at-side");
    if (!side) return;
    const open = force === undefined ? !state.sidebarOpen : !!force;
    state.sidebarOpen = open;
    side.hidden = false;
    side.classList.toggle("at-open", open);
    if (!open) setTimeout(() => { if (!state.sidebarOpen) side.hidden = true; }, 170);
    const btn = byId("at-files");
    if (btn) {
      btn.classList.toggle("at-on", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (open) listFiles();
    setTimeout(() => fitActive(), 180);
  }

  async function killActive() {
    const tab = activeTab();
    if (!tab) return;
    if (!confirm("Kill terminal '" + tab.name + "' and its processes?")) return;
    API.close(tab.sessionId).catch(() => {});
    disposeTab(tab);
  }

  function showRuntimeStatus() {
    const rt = state.runtime || {};
    const tab = activeTab();
    const lines = [
      "Agent Runtime: " + (state.available ? (rt.state || "ready") : "UNAVAILABLE"),
      "backend: " + (rt.backend || "—"),
      "container: " + (rt.container || "—"),
      "rootfs: " + (rt.rootfs_mode || "—"),
      "workspace: " + (rt.workspace || "/workspace"),
      "session: " + (tab ? tab.sessionId : "—"),
      "process: " + (tab ? (tab.processState || "—") : "—"),
    ];
    if (!state.available && rt.reason) lines.push("reason: " + rt.reason);
    alert(lines.join("\n"));
  }

  /* ---------------------------- runtime status --------------------------- */

  async function refreshStatus(force) {
    const r = await API.status().catch(() => ({ ok: false }));
    const data = (r && r.data) || {};
    state.runtime = data;
    state.available = !!data.available;
    renderStatus();
    if (state.available && (force || !state.mounted)) ensureInitialTabs();
    if (!state.available && force) {
      const msg = byId("at-unavailable-msg");
      if (msg) msg.textContent = data.reason || "isolation backend not found";
    }
  }

  function renderStatus() {
    const rt = state.runtime || {};
    const ok = state.available;
    const led = byId("at-led");
    const sub = byId("at-sub");
    const runtimeItem = byId("at-bar-runtime");
    if (led) led.className = "at-led " + (ok ? "at-led-on" : "at-led-off");
    if (sub) {
      sub.textContent = ok
        ? ((rt.backend || "runtime") + " · " + (rt.state || "ready"))
        : "unavailable";
    }
    if (runtimeItem) {
      runtimeItem.textContent = ok
        ? "Agent Runtime " + (rt.state || "ready")
        : "Agent Runtime unavailable";
    }
    const un = byId("at-unavailable");
    if (un) un.hidden = ok;
    const views = byId("at-views");
    if (views && ok) views.style.visibility = "visible";
  }

  function setConnected(ok, tab) {
    if (!tab || tab.sessionId !== state.active) return;
    const led = byId("at-led");
    const conn = byId("at-conn");
    if (led) led.className = "at-led " + (ok ? "at-led-on" : "at-led-off");
    if (conn) conn.textContent = ok ? "connected" : "disconnected";
  }

  function renderTabStatus(tab) {
    if (!tab) return;
    const cwd = byId("at-bar-cwd");
    const sess = byId("at-bar-session");
    const shell = byId("at-bar-shell");
    if (shell) shell.textContent = "bash";
    if (cwd) {
      cwd.textContent = (state.runtime && state.runtime.workspace) || "/workspace";
    }
    if (sess) {
      const st = tab.processState || "—";
      sess.textContent = "session " + tab.sessionId + " · " + st;
    }
  }

  /* ------------------------------- tabs ---------------------------------- */

  function tabStripEl() { return byId("at-tabs"); }
  function viewsEl() { return byId("at-views"); }

  function renderTabStrip() {
    const strip = tabStripEl();
    if (!strip) return;
    strip.innerHTML = "";
    state.order.forEach((sid) => {
      const tab = state.tabs.get(sid);
      if (!tab) return;
      const btn = h("button", "at-tab" + (sid === state.active ? " active" : ""),
        `<span class="at-tab-dot" data-state="${esc(tab.processState || "starting")}"></span>
         <span class="at-tab-name">${esc(tab.name)}</span>
         <span class="at-tab-x" title="Close tab">×</span>`);
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", sid === state.active ? "true" : "false");
      btn.onclick = (ev) => {
        if (ev.target.classList.contains("at-tab-x")) return closeTab(sid);
        activateTab(sid);
      };
      btn.ondblclick = (ev) => {
        if (ev.target.classList.contains("at-tab-x")) return;
        const next = prompt("Rename terminal", tab.name);
        if (next && next.trim()) {
          tab.name = next.trim().slice(0, 32);
          renderTabStrip();
        }
      };
      strip.appendChild(btn);
    });
  }

  function activateTab(sid) {
    state.active = sid;
    state.tabs.forEach((tab, key) => {
      if (tab.view) tab.view.style.display = key === sid ? "block" : "none";
    });
    renderTabStrip();
    const tab = state.tabs.get(sid);
    if (tab) {
      renderTabStatus(tab);
      renderKeyMods(tab);
      setTimeout(() => fitActive(), 30);
      if (tab.term) tab.term.focus();
    }
  }

  function disposeTab(tab) {
    if (!tab) return;
    if (tab.es) { try { tab.es.close(); } catch (_) {} }
    if (tab.term) { try { tab.term.dispose(); } catch (_) {} }
    if (tab.view) tab.view.remove();
    state.tabs.delete(tab.sessionId);
    state.order = state.order.filter((s) => s !== tab.sessionId);
    if (state.active === tab.sessionId) {
      state.active = state.order[state.order.length - 1] || "";
    }
    if (!state.order.length && state.available) return createTab();
    renderTabStrip();
    activateTab(state.active);
  }

  function closeTab(sid) {
    const tab = state.tabs.get(sid);
    if (!tab) return;
    // Closing a UI tab ends this PTY session only — the runtime (and every
    // other session, and all files) is untouched.
    API.close(sid).catch(() => {});
    disposeTab(tab);
  }

  async function createTab(opts) {
    if (!state.available) { refreshStatus(true); return null; }
    if (state.order.length >= MAX_TABS) return null;
    const o = opts || {};
    const sessionId = o.sessionId || defaultSessionId();
    if (state.tabs.has(sessionId)) { activateTab(sessionId); return sessionId; }

    const name = o.name || ("Terminal " + (state.order.length + 1));
    const view = h("div", "at-view");
    view.dataset.session = sessionId;
    viewsEl().appendChild(view);

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontFamily: '"JetBrains Mono", "Fira Code", "SFMono-Regular", Menlo, '
        + 'Consolas, "DejaVu Sans Mono", monospace',
      fontSize: o.fontSize || 13,
      lineHeight: 1.2,
      letterSpacing: 0,
      scrollback: 10000,
      convertEol: false,
      allowProposedApi: true,
      macOptionIsMeta: true,
      theme: {
        background: "#0b0f14", foreground: "#d7e0ea",
        cursor: "#7cc4ff", cursorAccent: "#0b0f14",
        selectionBackground: "rgba(124,196,255,0.30)",
        black: "#0b0f14", red: "#ff6b73", green: "#5ad19a", yellow: "#ffd479",
        blue: "#7cc4ff", magenta: "#c79bff", cyan: "#66e0d0", white: "#d7e0ea",
        brightBlack: "#5c6b7a", brightRed: "#ff8a90", brightGreen: "#7ee0b0",
        brightYellow: "#ffe0a0", brightBlue: "#a5d8ff", brightMagenta: "#d9bcff",
        brightCyan: "#8ff0e2", brightWhite: "#ffffff",
      },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(view);

    const tab = {
      sessionId, name, term, fit, view,
      processState: "starting", offset: 0, es: null,
      retries: 0, closed: false, sticky: { ctrl: false, alt: false },
    };
    state.tabs.set(sessionId, tab);
    state.order.push(sessionId);

    // Keyboard -> PTY. xterm already encodes Enter/Backspace/arrows/Tab/
    // Home/End/PageUp/PageDown and Ctrl+<letter> control bytes; we simply
    // forward what it produces unless the browser should handle it.
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== "keydown") return true;
      // Let the browser do copy/paste/select-all/new-tab.
      if (ev.ctrlKey && ev.shiftKey
          && ["C", "V", "A", "T"].includes(ev.key.toUpperCase())) {
        return false;
      }
      if (ev.metaKey) return false;
      return true;
    });
    term.onData((data) => sendInput(sessionId, applySticky(tab, data)));

    renderTabStrip();
    activateTab(sessionId);

    try {
      const opened = await API.open(sessionId, 24, 80, name);
      if (!opened || !opened.ok) {
        throw new Error((opened && (opened.error || opened.error_code))
                        || "open failed");
      }
      tab.processState = "running";
      openStream(tab);
      fitActive();
      setTimeout(() => sendResize(tab), 120);
      renderTabStrip();
    } catch (err) {
      term.writeln("\x1b[31mAstra Agent Terminal: could not open a runtime "
        + "session\x1b[0m");
      term.writeln("\x1b[90m" + esc(String(err && err.message || err)) + "\x1b[0m");
      tab.processState = "failed";
      renderTabStrip();
    }
    return sessionId;
  }

  function defaultSessionId() {
    const conv = conversationSessionId();
    if (conv && !state.tabs.has(conv)) return conv;
    const base = conv || "term";
    let n = 1;
    while (state.tabs.has(base + "-t" + n)) n += 1;
    return base + "-t" + n;
  }

  function ensureInitialTabs() {
    if (state.order.length) return;
    // First tab = the conversation's own session (shared with the agent).
    createTab(conversationSessionId() ? { sessionId: conversationSessionId() } : {});
  }

  /* ------------------------------ transport ------------------------------ */

  function openStream(tab) {
    if (tab.es) { try { tab.es.close(); } catch (_) {} }
    const url = "/api/runtime/terminal/stream?session_id="
      + encodeURIComponent(tab.sessionId) + "&offset=" + (tab.offset || 0);
    const es = new EventSource(url);
    tab.es = es;
    tab.closed = false;

    es.addEventListener("output", (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); } catch (_) { return; }
      if (frame.truncated && tab.offset === 0) {
        tab.term.write("\x1b[90m[astra: earlier scrollback is available "
          + "through the stored output]\x1b[0m\r\n");
      }
      tab.offset = frame.next_offset != null ? frame.next_offset : tab.offset;
      tab.term.write(frame.data || "");
    });
    es.addEventListener("exit", (ev) => {
      let payload = {};
      try { payload = JSON.parse(ev.data); } catch (_) {}
      tab.processState = payload.status || "stopped";
      tab.term.write("\r\n\x1b[90m[astra: session " + tab.processState
        + " (exit " + (payload.exit_code != null ? payload.exit_code : "?")
        + ")]\x1b[0m\r\n");
      renderTabStrip();
      renderTabStatus(tab);
      try { es.close(); } catch (_) {}
    });
    es.onopen = () => { tab.retries = 0; setConnected(true, tab); };
    es.onerror = () => {
      setConnected(false, tab);
      try { es.close(); } catch (_) {}
      if (tab.closed) return;
      // Exponential backoff reconnect that resumes from the byte offset we
      // already rendered, so the screen is never duplicated or lost.
      tab.retries += 1;
      if (tab.retries > 6) return;
      setTimeout(() => { if (!tab.closed) openStream(tab); },
        Math.min(4000, 350 * Math.pow(2, tab.retries - 1)));
    };
  }

  function sendInput(sessionId, data) {
    // Fire-and-forget: keystrokes must not queue behind a slow request.
    fetch("/api/runtime/terminal/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, data }),
      keepalive: true,
    }).catch(() => {});
  }

  function sendResize(tab) {
    const dims = tab.fit && tab.fit.proposeDimensions
      ? tab.fit.proposeDimensions() : null;
    if (!dims) return;
    API.resize(tab.sessionId, dims.rows, dims.cols).catch(() => {});
  }

  function fitActive() {
    const tab = activeTab();
    if (!tab || !tab.fit) return;
    try { tab.fit.fit(); } catch (_) {}
    sendResize(tab);
  }

  function reconnectActive() {
    const tab = activeTab();
    if (!tab) return;
    tab.closed = false;
    tab.retries = 0;
    openStream(tab);
  }

  async function restartActive() {
    const tab = activeTab();
    if (!tab) return;
    if (tab.es) tab.es.close();
    await API.close(tab.sessionId).catch(() => {});
    tab.term.reset();
    tab.offset = 0;
    const opened = await API.open(tab.sessionId, tab.term.rows, tab.term.cols,
      tab.name).catch(() => null);
    if (opened && opened.ok) {
      tab.processState = "running";
      openStream(tab);
    } else {
      tab.processState = "failed";
    }
    renderTabStrip();
  }

  async function lifecycle(action, extra) {
    const r = await API.lifecycle(action, extra).catch(() => ({ ok: false }));
    if (!r || !r.ok) {
      alert("Agent Runtime: "
        + ((r && (r.error || r.error_code)) || "action failed"));
      return r;
    }
    await refreshStatus(true);
    if (action === "stop" || action === "destroy" || action === "reset") {
      state.tabs.forEach((tab) => { if (tab.es) tab.es.close(); });
    }
    if (action === "restart") {
      state.tabs.forEach((tab) => {
        tab.offset = 0;
        if (tab.term) tab.term.reset();
        openStream(tab);
      });
    }
    return r;
  }

  /* ------------------------------ file drawer ---------------------------- */

  async function listFiles() {
    const list = byId("at-side-list");
    const pathEl = byId("at-side-path");
    if (!list) return;
    list.innerHTML = '<div class="at-empty">loading…</div>';
    const r = await API.files(state.sidebarPath).catch(() => ({ ok: false }));
    if (!r || !r.ok) {
      list.innerHTML = '<div class="at-empty">'
        + esc((r && r.error) || "unavailable") + '</div>';
      return;
    }
    const data = r.data || {};
    state.sidebarPath = data.path || state.sidebarPath;
    if (pathEl) pathEl.textContent = state.sidebarPath;
    list.innerHTML = "";
    const entries = data.entries || [];
    if (!entries.length) {
      list.innerHTML = '<div class="at-empty">empty</div>';
      return;
    }
    entries.forEach((entry) => {
      const row = h("button", "at-file",
        `<span class="at-file-icon">${entry.type === "directory" ? "▸" : "·"}</span>
         <span class="at-file-name">${esc(entry.name)}</span>
         <span class="at-file-alt">${entry.type === "directory" ? "" : esc(humanSize(entry.size))}</span>`);
      row.title = entry.path;
      row.onclick = () => {
        if (entry.type === "directory") {
          state.sidebarPath = entry.path;
          listFiles();
        } else {
          openEditor(entry.path);
        }
      };
      row.oncontextmenu = (ev) => {
        ev.preventDefault();
        if (confirm("Delete " + entry.path + "?")) {
          API.fileAction({ action: "remove", path: entry.path,
            recursive: entry.type === "directory" }).then(listFiles);
        }
      };
      list.appendChild(row);
    });
  }

  function humanSize(n) {
    const v = Number(n || 0);
    if (v < 1024) return v + "B";
    if (v < 1024 * 1024) return (v / 1024).toFixed(1) + "K";
    return (v / (1024 * 1024)).toFixed(1) + "M";
  }

  function joinPath(dir, name) {
    return (dir.endsWith("/") ? dir : dir + "/") + name;
  }

  function newEntry(kind) {
    const name = prompt(kind === "dir" ? "New folder name" : "New file name");
    if (!name || !name.trim()) return;
    const clean = name.trim().replace(/[\\/]/g, "_");
    const path = joinPath(state.sidebarPath, clean);
    const body = kind === "dir"
      ? { action: "mkdir", path }
      : { action: "write", path, content: "" };
    API.fileAction(body).then(() => {
      listFiles();
      if (kind === "file") openEditor(path);
    });
  }

  async function openEditor(path) {
    const r = await API.fileAction({ action: "read", path });
    if (!r || !r.ok) { alert((r && r.error) || "cannot read file"); return; }
    state.editorPath = path;
    byId("at-editor").hidden = false;
    byId("at-editor-path").textContent = path;
    byId("at-editor-text").value = (r.data && r.data.text) || "";
  }

  async function saveEditor() {
    const text = byId("at-editor-text").value;
    const r = await API.fileAction({ path: state.editorPath,
      action: "write", content: text });
    if (!r || !r.ok) { alert((r && r.error) || "save failed"); return; }
    listFiles();
  }

  async function doUpload(input) {
    const files = Array.from(input.files || []);
    if (!files.length) return;
    const form = new FormData();
    form.append("destination", state.sidebarPath);
    form.append("extract", "true");
    files.forEach((f) => form.append("file", f, f.name));
    input.value = "";
    const r = await API.upload(form);
    if (!r || !r.ok) { alert((r && r.error) || "upload failed"); return; }
    listFiles();
  }

  /* ------------------------------ viewport ------------------------------- */

  function bindViewport() {
    const apply = () => {
      const root = paneEl();
      const vv = window.visualViewport;
      if (root) {
        // distance from the window top to the terminal (site header height)
        root.style.setProperty("--at-off",
          Math.round(root.getBoundingClientRect().top + window.scrollY) + "px");
      }
      if (root && vv && vv.height) {
        // The software keyboard shrinks the visual viewport; pin the
        // terminal to it so no output hides behind the keyboard and xterm
        // is refitted to the smaller size.
        // Subtract everything above the terminal (browser/app header) so the
        // status line and extra keys stay on-screen at the bottom.
        const top = root.getBoundingClientRect().top + window.scrollY;
        const avail = Math.max(240, Math.round(vv.height - top - 6));
        root.style.setProperty("--at-vh", avail + "px");
      }
      fitActive();
    };
    window.addEventListener("resize", apply);
    apply();                                   // size correctly on first paint
    requestAnimationFrame(apply);              // ...and once layout has settled
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", apply);
      window.visualViewport.addEventListener("scroll", apply);
    }
    if (window.ResizeObserver) {
      const stage = byId("at-stage");
      if (stage) new ResizeObserver(() => fitActive()).observe(stage);
    }
  }

  /* ------------------------------- mount --------------------------------- */

  function mount() {
    const root = paneEl();
    if (!root || state.mounted) {
      if (state.mounted) { refreshStatus(false); fitActive(); }
      return;
    }
    state.mounted = true;
    buildLayout(root);
    bindViewport();
    listFiles();
    refreshStatus(true);
    state.statusTimer = setInterval(() => refreshStatus(false), 15000);
  }

  window.AstraTerminal = {
    mount,
    createTab,
    refreshStatus,
    pressKey,
    state,
    EXTRA_KEYS,
  };
  // Core-tab loader: astra.js's showTab() calls this the first time the
  // Astra Agent Terminal tab is opened, and on every return to it.
  if (window.Astra && Astra.loaders) Astra.loaders.terminal = mount;
})();
