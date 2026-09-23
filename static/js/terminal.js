/* Astra Agent Terminal — a real PC-style terminal for Astra.
 *
 * This is not a log viewer. The pane is a genuine terminal emulator
 * (xterm.js, vendored in static/js/vendor/) attached to a REAL PTY running
 * inside the isolated Agent Runtime (astra/runtime/pty.py):
 *
 *   keystroke -> /api/runtime/terminal/input -> PTY stdin
 *   PTY stdout -> /api/runtime/terminal/stream (SSE) -> xterm.write()
 *   browser resize -> /api/runtime/terminal/resize -> TIOCSWINSZ on the PTY
 *
 * Nothing here converts output into DOM log rows, and nothing here can
 * reach a host shell: every endpoint targets the runtime.
 */
(function () {
  "use strict";

  const SESSION_STORAGE_KEY = "astra:agent-terminal-sessions";
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

  const state = {
    mounted: false,
    available: false,
    runtime: null,
    tabs: new Map(),      // sessionId -> tab record
    order: [],            // sessionId order
    active: "",
    sidebarPath: "/workspace",
    editorPath: "",
    statusTimer: null,
    statusText: "unknown",
  };

  /* ------------------------------- DOM ---------------------------------- */

  function h(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function paneEl() { return document.getElementById("tab-terminal"); }

  function buildLayout(root) {
    root.innerHTML = "";
    root.classList.add("at-app");

    // -- header ----------------------------------------------------------
    const head = h("header", "at-head");
    head.innerHTML = `
      <div class="at-brand">
        <span class="at-brand-mark" aria-hidden="true">&gt;_</span>
        <span class="at-brand-text">
          <span class="at-brand-name">Astra Agent Terminal</span>
          <span class="at-brand-sub" id="at-sub">connecting…</span>
        </span>
      </div>
      <div class="at-status" id="at-status" role="status" aria-live="polite">
        <span class="at-led" id="at-led" aria-hidden="true"></span>
        <span id="at-status-text">Runtime</span>
      </div>
      <div class="at-actions">
        <button class="at-btn at-primary" id="at-new" title="New terminal (Ctrl+Shift+T)">+ New</button>
        <button class="at-btn" id="at-reconnect" title="Reconnect this terminal">Reconnect</button>
        <button class="at-btn" id="at-restart" title="Restart this shell session">Restart</button>
        <button class="at-btn" id="at-clear" title="Clear the screen (Ctrl+L)">Clear</button>
        <button class="at-btn" id="at-sidebar" title="Toggle the workspace sidebar">Files</button>
        <button class="at-btn at-danger" id="at-stop" title="Stop the Agent Runtime (closes all sessions)">Stop</button>
        <button class="at-btn at-danger" id="at-kill" title="Kill this terminal session">Kill</button>
      </div>`;
    root.appendChild(head);

    // -- tab strip -------------------------------------------------------
    const tabs = h("div", "at-tabs", "");
    tabs.id = "at-tabs";
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "Terminal sessions");
    root.appendChild(tabs);

    // -- body ------------------------------------------------------------
    const body = h("div", "at-body");
    const side = h("aside", "at-side", "");
    side.id = "at-side";
    side.setAttribute("aria-label", "Runtime workspace");
    side.innerHTML = `
      <div class="at-side-head">
        <span class="at-side-title">Workspace</span>
        <span class="at-side-path" id="at-side-path">/workspace</span>
      </div>
      <div class="at-side-actions">
        <button class="at-btn at-tiny" id="at-up" title="Parent directory">↑</button>
        <button class="at-btn at-tiny" id="at-refresh" title="Refresh">⟳</button>
        <button class="at-btn at-tiny" id="at-newfile" title="New file">+file</button>
        <button class="at-btn at-tiny" id="at-newdir" title="New folder">+dir</button>
        <button class="at-btn at-tiny" id="at-upload" title="Upload into the runtime">↑file</button>
        <input type="file" id="at-upload-input" class="at-hidden" multiple>
      </div>
      <div class="at-side-list" id="at-side-list" role="tree"></div>
      <div class="at-editor" id="at-editor" hidden>
        <div class="at-editor-head">
          <span id="at-editor-path"></span>
          <span>
            <button class="at-btn at-tiny" id="at-editor-save">Save</button>
            <button class="at-btn at-tiny" id="at-editor-close">Close</button>
          </span>
        </div>
        <textarea id="at-editor-text" spellcheck="false"></textarea>
      </div>`;
    body.appendChild(side);

    const stage = h("div", "at-stage");
    stage.id = "at-stage";
    const unavailable = h("div", "at-unavailable", "");
    unavailable.id = "at-unavailable";
    unavailable.hidden = true;
    unavailable.innerHTML = `
      <div class="at-unavailable-card">
        <div class="at-unavailable-icon" aria-hidden="true">⚠</div>
        <h2>Agent Runtime unavailable</h2>
        <p id="at-unavailable-msg"></p>
        <p class="at-muted">Astra will not run Agent work on the host terminal.
          Nothing here is a silent fallback.</p>
        <button class="at-btn at-primary" id="at-unavailable-retry">Check again</button>
      </div>`;
    stage.appendChild(unavailable);
    const views = h("div", "at-views");
    views.id = "at-views";
    stage.appendChild(views);
    body.appendChild(stage);
    root.appendChild(body);

    // -- status bar ------------------------------------------------------
    const bar = h("footer", "at-bar");
    bar.innerHTML = `
      <span class="at-bar-item" id="at-bar-shell">shell: —</span>
      <span class="at-bar-item" id="at-bar-cwd">cwd: —</span>
      <span class="at-bar-item" id="at-bar-session">session: —</span>
      <span class="at-bar-item" id="at-bar-proc">process: —</span>
      <span class="at-bar-item at-bar-right" id="at-bar-runtime">runtime: —</span>`;
    root.appendChild(bar);

    wireHeader();
  }

  function wireHeader() {
    document.getElementById("at-new").onclick = () => createTab();
    document.getElementById("at-reconnect").onclick = () => reconnectActive();
    document.getElementById("at-restart").onclick = () => restartActive();
    document.getElementById("at-clear").onclick = () => {
      const tab = state.tabs.get(state.active);
      if (tab && tab.term) { tab.term.clear(); tab.term.focus(); }
    };
    document.getElementById("at-sidebar").onclick = () => {
      const side = document.getElementById("at-side");
      side.classList.toggle("at-side-hidden");
      setTimeout(() => fitActive(), 60);
    };
    document.getElementById("at-stop").onclick = () => lifecycle("stop");
    document.getElementById("at-kill").onclick = () => {
      const tab = state.tabs.get(state.active);
      if (!tab) return;
      if (!confirm("Kill terminal '" + tab.name + "' and its processes?")) return;
      API.close(tab.sessionId).then(() => {
        if (tab.es) tab.es.close();
        tab.term.dispose();
        tab.view.remove();
        state.tabs.delete(tab.sessionId);
        state.order = state.order.filter((s) => s !== tab.sessionId);
        if (state.active === tab.sessionId) {
          state.active = state.order[state.order.length - 1] || "";
        }
        renderTabStrip();
        activateTab(state.active);
      });
    };

    document.getElementById("at-unavailable-retry").onclick = () => refreshStatus(true);
    document.getElementById("at-up").onclick = () => {
      const p = state.sidebarPath;
      if (p === "/workspace" || p === "/") return;
      const parent = p.replace(/\/[^/]+$/, "") || "/workspace";
      state.sidebarPath = parent.startsWith("/workspace") || parent === "/"
        ? parent : "/workspace";
      listFiles();
    };
    document.getElementById("at-refresh").onclick = () => listFiles();
    document.getElementById("at-newfile").onclick = () => newEntry("file");
    document.getElementById("at-newdir").onclick = () => newEntry("dir");
    const uploadInput = document.getElementById("at-upload-input");
    document.getElementById("at-upload").onclick = () => uploadInput.click();
    uploadInput.onchange = () => doUpload(uploadInput);
    document.getElementById("at-editor-close").onclick = () => {
      document.getElementById("at-editor").hidden = true;
    };
    document.getElementById("at-editor-save").onclick = () => saveEditor();
  }

  /* ---------------------------- runtime status --------------------------- */

  async function refreshStatus(force) {
    const r = await API.status().catch(() => ({ ok: false }));
    const data = (r && r.data) || {};
    state.runtime = data;
    state.available = !!data.available;
    state.statusText = data.state || "unavailable";
    renderStatus();
    if (state.available && (force || !state.mounted)) ensureInitialTabs();
    if (!state.available && force) {
      const msg = document.getElementById("at-unavailable-msg");
      if (msg) msg.textContent = data.reason || "isolation backend not found";
    }
  }

  function renderStatus() {
    const led = document.getElementById("at-led");
    const text = document.getElementById("at-status-text");
    const sub = document.getElementById("at-sub");
    const bar = document.getElementById("at-bar-runtime");
    const rt = state.runtime || {};
    const ok = state.available;
    if (led) led.className = "at-led " + (ok ? "at-led-on" : "at-led-off");
    if (text) {
      text.textContent = ok
        ? "Runtime " + (rt.state || "ready")
        : "Runtime unavailable";
    }
    if (sub) {
      sub.textContent = ok
        ? [rt.backend, rt.container, rt.rootfs_mode].filter(Boolean).join(" · ")
        : "isolation backend not found";
    }
    if (bar) {
      bar.textContent = rt.runtime_id
        ? "runtime: " + rt.runtime_id + " (" + (rt.container || "?") + ")"
        : "runtime: —";
    }
    const un = document.getElementById("at-unavailable");
    if (un) un.hidden = ok;
    const views = document.getElementById("at-views");
    if (views && ok) views.style.visibility = "visible";
  }

  /* ------------------------------- tabs ---------------------------------- */

  function tabStripEl() { return document.getElementById("at-tabs"); }
  function viewsEl() { return document.getElementById("at-views"); }

  function renderTabStrip() {
    const strip = tabStripEl();
    if (!strip) return;
    strip.innerHTML = "";
    state.order.forEach((sid) => {
      const tab = state.tabs.get(sid);
      if (!tab) return;
      const btn = h("button", "at-tab" + (sid === state.active ? " active" : ""),
        `<span class="at-tab-dot" data-state="${esc(tab.processState || "running")}"></span>
         <span class="at-tab-name">${esc(tab.name)}</span>
         <span class="at-tab-x" title="Close tab">×</span>`);
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", sid === state.active ? "true" : "false");
      btn.onclick = (ev) => {
        if (ev.target.classList.contains("at-tab-x")) {
          closeTab(sid);
          return;
        }
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
    const plus = h("button", "at-tab at-tab-new", "+");
    plus.title = "New terminal";
    plus.onclick = () => createTab();
    strip.appendChild(plus);
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
      setTimeout(() => fitActive(), 30);
      if (tab.term) tab.term.focus();
    }
  }

  function closeTab(sid) {
    const tab = state.tabs.get(sid);
    if (!tab) return;
    // Closing a UI tab ends this PTY session only — the runtime (and every
    // other session, and all files) is untouched.
    API.close(sid).catch(() => {});
    if (tab.es) tab.es.close();
    if (tab.term) tab.term.dispose();
    if (tab.view) tab.view.remove();
    state.tabs.delete(sid);
    state.order = state.order.filter((s) => s !== sid);
    if (state.active === sid) {
      state.active = state.order[state.order.length - 1] || "";
    }
    if (!state.order.length && state.available) {
      createTab();
      return;
    }
    renderTabStrip();
    activateTab(state.active);
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
      lineHeight: 1.25,
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
      retries: 0, closed: false,
    };
    state.tabs.set(sessionId, tab);
    state.order.push(sessionId);

    // Keyboard -> PTY. xterm already encodes Enter/Backspace/arrows/Tab/
    // Home/End/PageUp/PageDown and Ctrl+<letter> control bytes; we simply
    // forward what it produces unless the browser should handle it.
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== "keydown") return true;
      // Let the browser do copy/paste/select-all/new-tab.
      if (ev.ctrlKey && ev.shiftKey && ["C", "V", "A"].includes(ev.key.toUpperCase())) {
        return false;
      }
      if (ev.metaKey) return false;
      return true;
    });
    term.onData((data) => sendInput(sessionId, data));

    renderTabStrip();
    activateTab(sessionId);

    try {
      const opened = await API.open(sessionId, 24, 80, name);
      if (!opened || !opened.ok) throw new Error((opened && (opened.error || opened.error_code)) || "open failed");
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

  function setConnected(ok, tab) {
    const led = document.getElementById("at-led");
    if (led && tab && tab.sessionId === state.active) {
      led.classList.toggle("at-led-off", !ok);
      led.classList.toggle("at-led-on", ok);
    }
    const proc = document.getElementById("at-bar-proc");
    if (proc && tab && tab.sessionId === state.active) {
      proc.textContent = "process: " + (ok ? "connected" : "disconnected");
    }
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
    const tab = state.tabs.get(state.active);
    if (!tab || !tab.fit) return;
    try { tab.fit.fit(); } catch (_) {}
    sendResize(tab);
  }

  function renderTabStatus(tab) {
    if (!tab) return;
    const shell = document.getElementById("at-bar-shell");
    const cwd = document.getElementById("at-bar-cwd");
    const sess = document.getElementById("at-bar-session");
    const proc = document.getElementById("at-bar-proc");
    if (shell) shell.textContent = "shell: bash";
    if (cwd) {
      cwd.textContent = "cwd: "
        + ((state.runtime && state.runtime.workspace) || "/workspace");
    }
    if (sess) sess.textContent = "session: " + tab.sessionId;
    if (proc) proc.textContent = "process: " + (tab.processState || "—");
  }

  function reconnectActive() {
    const tab = state.tabs.get(state.active);
    if (!tab) return;
    tab.closed = false;
    tab.retries = 0;
    openStream(tab);
  }

  async function restartActive() {
    const tab = state.tabs.get(state.active);
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
      alert("Agent Runtime: " + ((r && (r.error || r.error_code)) || "action failed"));
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

  /* ------------------------------ sidebar -------------------------------- */

  async function listFiles() {
    const list = document.getElementById("at-side-list");
    const pathEl = document.getElementById("at-side-path");
    if (!list) return;
    list.innerHTML = '<div class="at-empty">loading…</div>';
    const r = await API.files(state.sidebarPath).catch(() => ({ ok: false }));
    if (!r || !r.ok) {
      list.innerHTML = '<div class="at-empty">' + esc((r && r.error) || "unavailable") + '</div>';
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
      const row = h("button", "at-file at-file-" + entry.type,
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
    API.fileAction(body).then(() => { listFiles(); if (kind === "file") openEditor(path); });
  }

  async function openEditor(path) {
    const r = await API.fileAction({ action: "read", path });
    if (!r || !r.ok) { alert((r && r.error) || "cannot read file"); return; }
    state.editorPath = path;
    document.getElementById("at-editor").hidden = false;
    document.getElementById("at-editor-path").textContent = path;
    document.getElementById("at-editor-text").value = (r.data && r.data.text) || "";
  }

  async function saveEditor() {
    const text = document.getElementById("at-editor-text").value;
    const r = await API.fileAction({ action: "write", path: state.editorPath,
      content: text });
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

  /* ------------------------------- mount --------------------------------- */

  function mount() {
    const root = paneEl();
    if (!root || state.mounted) {
      if (state.mounted) { refreshStatus(false); fitActive(); }
      return;
    }
    state.mounted = true;
    buildLayout(root);
    window.addEventListener("resize", () => fitActive());
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => fitActive());
      ro.observe(document.getElementById("at-stage"));
    }
    listFiles();
    refreshStatus(true);
    state.statusTimer = setInterval(() => refreshStatus(false), 15000);
  }

  window.AstraTerminal = {
    mount,
    createTab,
    refreshStatus,
    state,
  };
  // Core-tab loader: astra.js's showTab() calls this the first time the
  // Astra Agent Terminal tab is opened, and on every return to it.
  if (window.Astra && Astra.loaders) Astra.loaders.terminal = mount;
})();
