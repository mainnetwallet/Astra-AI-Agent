/* Astra AI Agent — core frontend bootstrap.
 *
 * Loads /api/manifest, builds the tab bar, creates a tabview per plugin tab,
 * loads each plugin's JS (which self-registers via Astra.register), renders
 * the aggregated dashboard, and provides shared chat + backup.
 *
 * Plugin authors write:  Astra.register(slug, { title, render(el), dashboard() })
 */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let body = {};
  try { body = await res.json(); } catch (_) { /* empty */ }
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: b });
const patch = (p, b) => api(p, { method: "PATCH", body: b });
const del = (p) => api(p, { method: "DELETE" });

/* ------------------------------ plugin registry ---------------------------- */
window.Astra = {
  plugins: {},
  register(slug, def) {
    Astra.plugins[slug] = { rendered: false, ...def };
  },
};

/* -------------------------------- tabs -------------------------------------- */
let MANIFEST = null;
const loaders = {};
const TAB_LABELS = {};

function showTab(name) {
  $$("#nav .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tabview").forEach((v) => v.classList.toggle("active", v.id === `tab-${name}`));
  closeNavMenu();
  try { localStorage.setItem("astra:active-tab", name); } catch (_) { /* ignore */ }
  const loader = loaders[name];
  if (loader) loader();
}

/* left-side 3-dot menu that holds the tab list */
function openNavMenu() {
  $("#nav").classList.add("open");
  $("#menu-btn").setAttribute("aria-expanded", "true");
}
function closeNavMenu() {
  $("#nav").classList.remove("open");
  $("#menu-btn").setAttribute("aria-expanded", "false");
}
$("#menu-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = $("#nav").classList.contains("open");
  if (open) closeNavMenu(); else openNavMenu();
});
document.addEventListener("click", (e) => {
  if (!$("#nav-menu").contains(e.target)) closeNavMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeNavMenu();
});

function deadlineText(d, today = new Date()) {
  if (!d) return "";
  const diff = Math.round((new Date(d + "T00:00:00") - new Date(today.toDateString())) / 86400000);
  if (diff === 0) return ' class="hl-today"';
  if (diff < 0) return ' class="hl-overdue"';
  if (diff <= 7) return ' class="hl-soon"';
  return "";
}
function deadlineTitle(d) {
  if (!d) return "";
  const diff = Math.round((new Date(d + "T00:00:00") - new Date(new Date().toDateString())) / 86400000);
  if (diff === 0) return "TODAY!";
  if (diff < 0) return "overdue " + -diff + "d";
  return "in " + diff + "d";
}

/* ------------------------------ dashboard ---------------------------------- */
loaders.dashboard = async function () {
  const r = await api("/api/dashboard");
  if (!r.ok) return;
  const blocks = r.data || [];
  $("#dash-blocks").innerHTML = blocks.length
    ? blocks.map((b) => `
        <div class="panel dash-block">
          <div class="panel-head">
            <h3>${b.icon} ${esc(b.title)}</h3>
            <button class="btn mini" data-goto="${b.slug}">Open ➜</button>
          </div>
          <div class="dash-body" id="dash-${b.slug}"></div>
          <div class="cards" id="cards-${b.slug}"></div>
          <div class="grid2" id="grid-${b.slug}"></div>
        </div>`).join("")
    : `<div class="empty">Kono plugin nai — ekhoni ekta add korun 🛠️</div>`;
  blocks.forEach((b) => {
    const cards = b.data.cards || [];
    $("#cards-" + b.slug).innerHTML = cards.map((c) => `
      <div class="card"><div class="card-v">${esc(c.v)}</div>
        <div class="card-k">${esc(c.k)}</div><div class="card-s">${esc(c.s)}</div></div>`).join("");
    // plugin-specific extended dashboard (deadlines etc.)
    const def = Astra.plugins[b.slug];
    if (def && def.dashboard) {
      const el = $("#dash-" + b.slug);
      el.innerHTML = def.dashboard(b.data);
    }
  });
};
$("#dash-blocks").addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) showTab(g.dataset.goto);
});

/* ----------------------------- assistant chat ------------------------------- */
loaders.assistant = function () {
  if (Astra.plugins._chatWired) return;
  Astra.plugins._chatWired = true;
  wireChatComposer();
};

function chatBubble(who, text, action, attachedFiles, artifacts, meta) {
  hideChatEmpty();
  const row = document.createElement("div");
  row.className = "msg " + (who === "me" ? "user" : "assistant");
  const content = document.createElement("div");
  content.className = "msg-content";
  if (who !== "me") {
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.textContent = "🚀";
    row.appendChild(avatar);
  }
  if (attachedFiles && attachedFiles.length) {
    const strip = document.createElement("div");
    strip.className = "msg-attachments";
    attachedFiles.forEach((f) => {
      const chip = document.createElement("span");
      chip.className = "msg-attach-chip";
      chip.textContent = _fileIcon(f.name) + " " + f.name;
      strip.appendChild(chip);
    });
    content.appendChild(strip);
  }
  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  textEl.innerHTML = String(text || "").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  content.appendChild(textEl);
  if (artifacts && artifacts.length) {
    const artWrap = document.createElement("div");
    artWrap.className = "msg-artifacts";
    artifacts.forEach((a) => { artWrap.appendChild(renderArtifact(a)); });
    content.appendChild(artWrap);
  }
  if (action === "confirm") {
    // inline Approve/Reject — resolved right here in chat, never by
    // sending the user off to a separate tab.
    const eid = meta && meta.execution_id;
    const wrap = document.createElement("div");
    wrap.className = "msg-confirm-actions";
    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "msg-confirm-btn msg-approve-btn";
    approveBtn.textContent = "✅ Approve";
    const rejectBtn = document.createElement("button");
    rejectBtn.type = "button";
    rejectBtn.className = "msg-confirm-btn msg-reject-btn";
    rejectBtn.textContent = "❌ Reject";
    const decide = async (allow) => {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      wrap.classList.add("resolved");
      const typingRow = chatTyping();
      try {
        const r = await post("/api/chat/resume", { execution_id: eid, allow });
        typingRow.remove();
        if (!r.ok || !r.data) {
          chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
          return;
        }
        chatBubble("ai", r.data.reply, r.data.action, null,
                   r.data.artifacts, r.data.data);
      } catch (err) {
        typingRow.remove();
        chatBubble("ai", "Server e problem — `" + err + "`");
      }
    };
    approveBtn.addEventListener("click", () => decide(true));
    rejectBtn.addEventListener("click", () => decide(false));
    wrap.appendChild(approveBtn);
    wrap.appendChild(rejectBtn);
    content.appendChild(wrap);
  } else if (action && action !== "none") {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "msg-action-link";
    link.textContent = "→ " + (TAB_LABELS[action] || action) + " e dekhun";
    link.addEventListener("click", () => showTab(action));
    content.appendChild(link);
  }
  row.appendChild(content);
  $("#chat-log").appendChild(row);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return row;
}
function chatTyping() {
  hideChatEmpty();
  const row = document.createElement("div");
  row.className = "msg assistant";
  row.innerHTML = `<div class="msg-avatar">🚀</div>
    <div class="msg-content"><div class="typing"><span></span><span></span><span></span></div></div>`;
  $("#chat-log").appendChild(row);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return row;
}
function hideChatEmpty() {
  const empty = $("#chat-empty");
  if (empty) empty.remove();
}
function wireChatComposer() {
  const input = $("#chat-input");
  const sendBtn = $("#chat-send");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
    sendBtn.disabled = !input.value.trim();
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#chat-suggestions")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".suggestion");
    if (!btn) return;
    input.value = btn.dataset.text || "";
    autosize();
    input.focus();
  });
  autosize();
}
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const msg = input.value.trim();
  const hasAttachments = _pendingAttachments.length > 0;
  if (!msg && !hasAttachments) return;
  chatBubble("me", msg, null, _pendingAttachments.map((a) => a.file));
  input.value = "";
  input.style.height = "auto";
  $("#chat-send").disabled = true;
  const typingRow = chatTyping();
  showUploadIndicator(hasAttachments);
  try {
    let r;
    if (hasAttachments) {
      const fd = new FormData();
      fd.append("message", msg);
      _pendingAttachments.forEach((a) => fd.append("files", a.file));
      clearAttachments();
      const res = await fetch("/api/chat", { method: "POST", body: fd });
      r = await res.json();
    } else {
      r = await post("/api/chat", { message: msg });
    }
    typingRow.remove();
    hideUploadIndicator();
    if (!r.ok || !r.data) {
      chatBubble("ai", "Server e problem — `" + (r.error || "unknown error") + "`");
      return;
    }
    chatBubble("ai", r.data.reply, r.data.action, null, r.data.artifacts, r.data.data);
    if (r.data.action === "dashboard") loaders.dashboard();
  } catch (err) {
    typingRow.remove();
    hideUploadIndicator();
    chatBubble("ai", "Server e problem — `" + err + "`");
  }
});


/* -------------------------------- backup ------------------------------------ */
$("#btn-export").addEventListener("click", async () => {
  const r = await api("/api/export");
  if (!r.ok) return;
  const blob = new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `astra-backup-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  $("#backup-info").textContent = "✅ Export done!";
});
$("#btn-import").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const data = JSON.parse(await f.text());
  const r = await post("/api/import", { data });
  const parts = Object.entries(r.data || {})
    .map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(", ");
  $("#backup-info").textContent = "✅ Imported — " + (parts || "done");
  loaders.dashboard();
});

/* ------------------------------ ACTIVITY LOG (core tab) ---------------------- */
loaders.logs = async function () {
  if (!Astra.plugins._logsLoaded) {
    Astra.plugins._logsLoaded = true;
    initLogsToolbar();
    const lastId = await loadLogsHistory();
    if (window.EventSource) openSse(lastId);
    else setInterval(eventsPoll, 3000);   // fallback for older browsers
  }
};

// The Logs panel is meant for the stuff that actually matters operationally
// — Gateway calls, provider/API calls, and provider health — not every
// internal step of a chat turn (agent/task/tool/memory/workflow/router
// events all fire per chat message and would drown those out).
const IMPORTANT_EVENT_HEADS = new Set(["astra_gateway", "ai", "provider"]);
function isImportantEvent(e) {
  const kind = (e && e.kind) || "";
  if (isCorrectionEvent(kind)) return true;
  if (!IMPORTANT_EVENT_HEADS.has(kind.split(".")[0])) return false;
  // The Gateway's own connections also emit ai.started/completed while
  // streaming, but every Gateway call is already reported (with its
  // AI + model) by the astra_gateway.* wrapper events — showing both
  // would list — and count — the same call twice.
  if (kind.startsWith("ai.") && e.agent === "gateway") return false;
  return true;
}
// Gateway result-supervision events ("the reply wasn't what was asked for,
// asking the Provider again") — shown so a rejected reply is explainable.
function isCorrectionEvent(kind) {
  return /^gateway\.(task_completion|supervision)\.correction_/.test(kind || "");
}

// Everything that already happened before the Logs tab/SSE connection
// opened lives in the events table — load it once up front so the panel
// shows the full picture, not just events from this moment forward.
// Returns the newest id loaded (0 if none), so the live SSE stream can
// pick up from exactly there with no gap and no duplicates.
async function loadLogsHistory() {
  const feed = $("#live-feed");
  try {
    // fetch more than the display buffer needs since most rows get
    // filtered out by isImportantEvent() below.
    const r = await api("/api/events?limit=500");
    const rows = (r && r.ok && r.data) ? r.data : [];
    if (!rows.length) return 0;
    if (feed && feed.firstElementChild && feed.firstElementChild.classList.contains("empty"))
      feed.innerHTML = "";
    // rows arrive newest-first; feedLine() always prepends, so process
    // oldest-first to end up with the same newest-on-top order live
    // events get.
    [...rows].reverse().forEach(feedLine);
    return rows[0].id || 0;
  } catch (_) {
    return 0;
  }
}

/* ------------------------------ logs toolbar --------------------------------
 * Category filters, search, pause/resume and clear for the Live activity
 * feed. Purely client-side: every rendered feed-line carries the event kind
 * + a resolved category as data attributes, and the toolbar just toggles
 * visibility / appends nothing while paused. */
const LOGS = {
  filter: "all", query: "", paused: false, buffer: [],
  counts: { total: 0, gateway: 0, provider: 0, errors: 0, success: 0 },
};
const LOGS_MAX_BUFFER = 300;

// Two separate API-call counters: Gateway (its own 4 AI services) and
// Provider (the Provider system's providers). Success/Errors count the
// outcomes of those same calls, so Success + Errors = Gateway + Provider.
const LOG_STAT_DEFS = [
  { key: "total", label: "Total" },
  { key: "gateway", label: "Gateway API calls" },
  { key: "provider", label: "Provider API calls" },
  { key: "success", label: "Success" },
  { key: "errors", label: "Errors" },
];

function renderLogStats() {
  const el = $("#logs-stats");
  if (el) el.innerHTML = LOG_STAT_DEFS.map((d) =>
    `<div class="card"><div class="card-v">${LOGS.counts[d.key] || 0}</div>` +
    `<div class="card-k">${esc(d.label)}</div></div>`).join("");
  const count = $("#logs-count");
  if (count) count.textContent = `${LOGS.counts.total} events`;
}

// Which system an event belongs to: the Astra AI Gateway (its own four AI
// services) or the Provider system.
function logSource(e) {
  const k = (e && e.kind) || "";
  const head = k.split(".")[0];
  if (head === "astra_gateway" || isCorrectionEvent(k)) return "gateway";
  if (head === "ai") return e.agent === "gateway" ? "gateway" : "provider";
  if (head === "provider") return "provider";
  return "";
}

// "ok" | "err" | "info" — how the line is coloured / filtered.
function logStatus(e) {
  const k = (e && e.kind) || "";
  const d = (e && e.data) || {};
  if (k === "astra_gateway.success" || k === "ai.completed") return "ok";
  if (isCorrectionEvent(k))
    return /correction_(failed|exhausted)$/.test(k) ? "err"
         : /correction_succeeded$/.test(k) ? "ok" : "info";
  if (k === "astra_gateway.error" || k === "astra_gateway.stream_interrupted" ||
      k === "ai.failed" || k === "provider.failed") return "err";
  if (k === "provider.health_changed") return d.healthy === false ? "err" : "ok";
  return "info";
}

// "ok" | "err" | null — set ONLY for the terminal event of one real API
// call. Every call fires a start/request event and exactly one terminal
// event, so counting terminals counts calls (not log lines). Aggregate
// "all providers failed" summaries and stream_interrupted follow-ups are
// not extra calls.
function logOutcome(e) {
  const k = (e && e.kind) || "";
  const d = (e && e.data) || {};
  if (k === "astra_gateway.success") return "ok";
  if (k === "astra_gateway.error") return "err";
  if (logSource(e) === "provider" && !d.aggregate) {
    if (k === "ai.completed") return "ok";
    if (k === "ai.failed") return "err";
  }
  return null;
}

// filter-chip categories for one event: gateway | providers | errors | success
function logCategories(e) {
  const cats = new Set();
  const src = logSource(e);
  if (src === "gateway") cats.add("gateway");
  if (src === "provider") cats.add("providers");
  const st = logStatus(e);
  if (st === "err") cats.add("errors");
  if (st === "ok") cats.add("success");
  return cats;
}

function initLogsToolbar() {
  const bar = $("#logs-filters");
  if (bar && !bar.dataset.hooked) {
    bar.dataset.hooked = "1";
    bar.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      $$(".chip", bar).forEach((c) => c.classList.toggle("active", c === chip));
      LOGS.filter = chip.dataset.filter;
      applyLogsFilter();
    });
  }
  const search = $("#logs-search");
  if (search && !search.dataset.hooked) {
    search.dataset.hooked = "1";
    search.addEventListener("input", () => {
      LOGS.query = search.value.trim().toLowerCase();
      applyLogsFilter();
    });
  }
  const pauseBtn = $("#btn-logs-pause");
  if (pauseBtn && !pauseBtn.dataset.hooked) {
    pauseBtn.dataset.hooked = "1";
    pauseBtn.addEventListener("click", () => {
      LOGS.paused = !LOGS.paused;
      pauseBtn.textContent = LOGS.paused ? "▶ Resume" : "⏸ Pause";
      pauseBtn.classList.toggle("on", LOGS.paused);
    });
  }
  const clearBtn = $("#btn-logs-clear");
  if (clearBtn && !clearBtn.dataset.hooked) {
    clearBtn.dataset.hooked = "1";
    clearBtn.addEventListener("click", async () => {
      LOGS.buffer = [];
      LOGS.counts = { total: 0, gateway: 0, provider: 0, errors: 0, success: 0 };
      renderLogStats();
      $("#live-feed").innerHTML = `<div class="empty">cleared — listening…</div>`;
      // Also wipe the persisted history server-side — otherwise a page
      // refresh reloads the same old events right back into the panel.
      try { await del("/api/events"); } catch (_) { /* best-effort */ }
    });
  }
  const copyBtn = $("#btn-logs-copy");
  if (copyBtn && !copyBtn.dataset.hooked) {
    copyBtn.dataset.hooked = "1";
    copyBtn.addEventListener("click", () => copyLogsToClipboard(copyBtn));
  }
  renderLogStats();
}

// Copies the currently-visible (i.e. filter/search-matched) log lines as
// plain text, newest-first, in the same order they're shown on screen.
async function copyLogsToClipboard(btn) {
  const feed = $("#live-feed");
  const lines = $$(".term-line", feed)
    .filter((el) => !el.classList.contains("hidden"))
    .map((el) => $$(".term-time, .term-src, .term-msg", el).map((s) => s.textContent).join(" "));
  const out = lines.join("\n");
  const flash = (label) => {
    if (!btn) return;
    const prev = btn.textContent;
    btn.textContent = label;
    setTimeout(() => { btn.textContent = prev; }, 1400);
  };
  if (!out) { flash("Nothing to copy"); return; }
  try {
    await navigator.clipboard.writeText(out);
    flash("✅ Copied");
  } catch (_) {
    // clipboard API unavailable/blocked (e.g. non-https localhost webview) —
    // fall back to a hidden textarea + execCommand.
    try {
      const ta = document.createElement("textarea");
      ta.value = out;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      flash("✅ Copied");
    } catch (_e) {
      flash("⚠️ Copy failed");
    }
  }
}

function applyLogsFilter() {
  const feed = $("#live-feed");
  if (!feed) return;
  $$(".log-row", feed).forEach((el) => {
    const cats = (el.dataset.cats || "").split(",");
    const matchesFilter = LOGS.filter === "all" || cats.includes(LOGS.filter);
    const matchesQuery = !LOGS.query || (el.dataset.text || "").includes(LOGS.query);
    el.classList.toggle("hidden", !(matchesFilter && matchesQuery));
  });
}

/* ------------------------------ providers (core) --------------------------- */
// Model-level test results kept client-side so a redraw from /api/providers
// (which only knows aggregate provider health, not "model X took 42ms just
// now") never wipes out what the last Test click just showed.
const PROVIDER_MODEL_RESULTS = {};   // provider name -> [{model, ok, latency_ms, error, pending}]
// Provider name -> that provider's model id list, refreshed on every
// /api/providers render. The per-model test click reads from here instead
// of re-fetching, so it knows exactly which models to fire requests for.
const PROVIDER_MODELS = {};
// Provider name -> its API keys as the server reports them (secret-free:
// {key_id, label:"key 1", healthy, in_cooldown, last_error}). Read by the
// per-key test so it knows which keys to fire every model through.
const PROVIDER_KEYS = {};
// Providers/connections temporarily revealed by their own Test click while
// the main Hide/Show toggle is set to hidden — session-only (never saved to
// localStorage), so a real page refresh drops back to fully hidden per the
// main toggle's persisted state, exactly like it did before this override
// existed. Cleared whenever the main toggle itself is clicked.
const FORCE_SHOWN_PROVIDERS = new Set();
const FORCE_SHOWN_GATEWAY = new Set();

// One API key's result for one model. `k` = {key_id, label, pending?, ok?,
// latency_ms?, error?}; `ok` undefined/null means "never tested".
function keyChipHtml(k) {
  const id = `data-key="${esc(k.key_id)}"`;
  if (k.pending) return `<span class="key-chip pending" ${id}>⏳ ${esc(k.label)}</span>`;
  if (k.ok === undefined || k.ok === null)
    return `<span class="key-chip none" ${id}>${esc(k.label)} · not tested</span>`;
  const when = k.tested_at ? ` title="${esc(k.tested_at)}"` : "";
  if (k.ok) return `<span class="key-chip ok" ${id}${when}>✓ ${esc(k.label)} · ${k.latency_ms}ms</span>`;
  return `<span class="key-chip bad" ${id}${when || ` title="${esc(k.error || "")}"`}>` +
    `❌ ${esc(k.label)} · ${esc(k.error || "error")}</span>`;
}

// Saved (server-side) per-key results -> the same row shape a live test
// produces, so a page reload shows the last known state of every key.
function savedKeyRows(models, keys, keyResults) {
  let any = false;
  const rows = models.map((m) => ({
    model: m,
    keys: keys.map((k) => {
      const r = ((keyResults || {})[m] || {})[k.key_id];
      if (r) any = true;
      return r ? { key_id: k.key_id, label: k.label, ok: r.ok, latency_ms: r.latency_ms,
                   error: r.error, tested_at: r.tested_at }
               : { key_id: k.key_id, label: k.label };
    }),
  }));
  return any ? rows : [];
}

// Saved (server-side) per-model Gateway health -> the same row shape a
// live test produces, so a page reload shows each connection's last
// known state instead of an empty table. Mirrors savedKeyRows above,
// which already does this for provider per-key results — the Gateway
// card was missing this restoration entirely, so it went blank on every
// reload even though the server has the data (routing_state health).
function savedGatewayModelRows(models, modelHealth) {
  let any = false;
  const rows = (models || []).map((modelId) => {
    const h = (modelHealth || {})[modelId];
    const tested = h && ((h.success_count || 0) + (h.failure_count || 0) > 0);
    if (!tested) return { model: modelId, untested: true };
    any = true;
    const lastOk = h.last_success && (!h.last_failure || h.last_success > h.last_failure);
    return lastOk
      ? { model: modelId, ok: true, latency_ms: Math.round(h.average_latency_ms || 0) }
      : { model: modelId, ok: false, error: "last test failed" };
  });
  return any ? rows : [];
}

function modelHealthRowsHtml(results) {
  if (!results || !results.length) {
    return "";
  }
  return results.map((m) => {
    if (m.untested) {
      return `<div class="model-health-row" data-model-row="${esc(m.model)}">` +
        `<span class="status-dot"></span>` +
        `<span class="model-name">${esc(m.model)}</span>` +
        `<span class="model-latency muted">not tested yet</span></div>`;
    }
    if (m.keys) {
      const anyPending = m.keys.some((k) => k.pending);
      const anyOk = m.keys.some((k) => k.ok === true);
      const anyTested = m.keys.some((k) => k.ok === true || k.ok === false);
      const cls = anyPending ? "pending" : (anyOk ? "ok" : (anyTested ? "bad" : "none"));
      const dot = anyPending ? "warn" : (anyOk ? "ok" : (anyTested ? "bad" : "warn"));
      return `<div class="model-health-row keyed ${cls}" data-model-row="${esc(m.model)}">` +
        `<span class="status-dot ${dot}"></span>` +
        `<span class="model-name">${esc(m.model)}</span>` +
        `<div class="key-results">${m.keys.map(keyChipHtml).join("")}</div></div>`;
    }
    if (m.pending) {
      return `<div class="model-health-row pending" data-model-row="${esc(m.model)}">` +
        `<span class="status-dot warn"></span>` +
        `<span class="model-name">${esc(m.model)}</span>` +
        `<span class="model-latency">⏳ testing…</span></div>`;
    }
    const cls = m.ok ? "ok" : "bad";
    const right = m.ok ? `${m.latency_ms}ms` : `❌ ${esc(m.error || "error")}`;
    return `<div class="model-health-row ${cls}" data-model-row="${esc(m.model)}">` +
      `<span class="status-dot ${m.ok ? "ok" : "bad"}"></span>` +
      `<span class="model-name">${esc(m.model)}</span>` +
      `<span class="model-latency">${right}</span></div>`;
  }).join("");
}

// Shared by every "test" button (single provider, single gateway
// connection, and the combined Test All): seed every model as a pending
// row, fire one request per model in parallel via `testOneFn`, and paint
// each row into `tableEl` the instant that model's own response lands —
// no waiting on the slowest model before the fast ones show up.
// `resultsArray` is mutated in place (it's the same array object held in
// PROVIDER_MODEL_RESULTS / GATEWAY_MODEL_RESULTS) so a later re-render
// from /api/providers still sees each finished result. Returns the
// Promise that resolves once every model has settled (success or error).
function streamModelTests(models, tableEl, resultsArray, testOneFn, onResult) {
  resultsArray.length = 0;
  models.forEach((m) => resultsArray.push({ model: m, pending: true }));
  if (tableEl) tableEl.innerHTML = modelHealthRowsHtml(resultsArray);

  const updateRow = (result) => {
    const idx = resultsArray.findIndex((r) => r.model === result.model);
    if (idx >= 0) resultsArray[idx] = result; else resultsArray.push(result);
    if (!tableEl) return;
    const rowEl = $(`[data-model-row="${CSS.escape(result.model)}"]`, tableEl);
    const html = modelHealthRowsHtml([result]);
    if (rowEl) rowEl.outerHTML = html; else tableEl.insertAdjacentHTML("beforeend", html);
  };

  const probes = models.map((modelId) =>
    testOneFn(modelId)
      .then((result) => { updateRow(result); if (onResult) onResult(result); })
      .catch((e) => {
        const result = { model: modelId, ok: false, latency_ms: 0, error: String(e) };
        updateRow(result);
        if (onResult) onResult(result);
      })
  );
  // Promise.allSettled (not Promise.all) so one model erroring can never
  // stop the caller from moving on once the rest are done — every
  // individual result was already saved+shown the moment it arrived.
  return Promise.allSettled(probes);
}

// Hide/show toggle state (AI Providers health + Astra AI Gateway model
// tables), persisted so it survives a real browser refresh — a plain
// DOM class survives re-renders within the page's lifetime but resets on
// reload since the whole document/JS is torn down and rebuilt.
const MODELS_HIDDEN_KEY = "astra_models_hidden";
function _loadModelsHiddenState() {
  try { return JSON.parse(localStorage.getItem(MODELS_HIDDEN_KEY) || "{}"); }
  catch (_e) { return {}; }
}
function _saveModelsHiddenState(state) {
  try { localStorage.setItem(MODELS_HIDDEN_KEY, JSON.stringify(state)); }
  catch (_e) { /* storage unavailable/full — toggle still works this session */ }
}

loaders.providers = async function () {
  const r = await api("/api/providers");
  const list = $("#providers-list");
  // Re-apply the persisted hide/show state on every load — classList
  // survives re-renders within one page session (innerHTML only replaces
  // children), but a real browser refresh tears down the whole DOM/JS, so
  // without this the class (and thus the hidden tables) reset to shown.
  if (_loadModelsHiddenState().providers) list.classList.add("models-hidden");
  if (!r.ok) {
    // A transient failure (a burst of parallel "Test all" requests is
    // exactly when the backend is most likely to hiccup) must never wipe
    // rows that are already on screen — only show the error state if
    // this is genuinely the first load. `list.dataset.hooked` is set the
    // moment a successful render has completed at least once, so it's a
    // reliable "have we shown real data before" flag.
    if (!list.dataset.hooked) {
      list.innerHTML = `<div class="empty">${esc(r.error || "providers unavailable")}</div>`;
    }
    return;
  }
  const provs = (r.data && r.data.providers) ? r.data.providers : r.data;
  const rows = Object.entries(provs || {}).map(([n, p]) => {
    const dot = p.healthy ? "ok" : (p.state === "down" ? "bad" : "warn");
    const modelCount = p.models ? p.models.length : 0;
    PROVIDER_MODELS[n] = p.models || [];
    PROVIDER_KEYS[n] = p.keys || [];
    // First paint after a reload: show the last saved per-key results.
    if (!(PROVIDER_MODEL_RESULTS[n] || []).length && (p.keys || []).length) {
      PROVIDER_MODEL_RESULTS[n] = savedKeyRows(p.models || [], p.keys, p.key_results);
    }
    const keyCount = (p.keys || []).length;
    const forceShow = FORCE_SHOWN_PROVIDERS.has(n) ? " force-show" : "";
    return `<div class="provider-card${forceShow}" data-provider-row="${esc(n)}">` +
      `<div class="provider-card-head">` +
      `<span class="status-dot ${dot}"></span><b>${esc(n)}</b>` +
      `<span class="grow muted" data-role="provider-counts" ` +
      `data-calls="${p.calls || 0}" data-errors="${p.errors || 0}" ` +
      `data-label="${esc(p.state || (p.healthy ? "healthy" : "?"))}" data-modelcount="${modelCount}" ` +
      `data-keycount="${keyCount}">` +
      `${esc(p.state || (p.healthy ? "healthy" : "?"))} · ` +
      `${modelCount} model(s) · ${keyCount} key(s) · ${p.calls || 0} calls · ${p.errors || 0} err</span>` +
      `<button class="btn mini" data-role="provider-test" data-provider="${esc(n)}">🧪 Test (${modelCount || 0} model${modelCount === 1 ? "" : "s"}${keyCount > 1 ? ` × ${keyCount} keys` : ""})</button>` +
      `</div>` +
      `<div class="model-health-table" data-role="model-table">` +
      modelHealthRowsHtml(PROVIDER_MODEL_RESULTS[n]) +
      `</div></div>`;
  }).join("");
  list.innerHTML = rows || `<div class="empty">kono provider e creds nai (offline mode)</div>`;
  // Hide/show toggle for every provider's per-model rows (gemini-3.7-flash,
  // key chips, etc.) — one button, same click alternates hide <-> show.
  // The state lives as a class on #providers-list itself (not on the rows
  // just rebuilt above), so it survives every re-render: list.innerHTML
  // replaces the children each time, never this element's own classList.
  const toggleBtn = $("#btn-providers-toggle-models");
  if (toggleBtn && !toggleBtn.dataset.hooked) {
    toggleBtn.dataset.hooked = "1";
    toggleBtn.onclick = () => {
      const hidden = list.classList.toggle("models-hidden");
      toggleBtn.textContent = hidden ? "👁 Show models" : "🙈 Hide models";
      const st = _loadModelsHiddenState();
      st.providers = hidden;
      _saveModelsHiddenState(st);
      // Flipping the main toggle either way makes any per-provider
      // "revealed by testing it" override moot — clear it so it doesn't
      // linger and confuse the next hide.
      FORCE_SHOWN_PROVIDERS.clear();
    };
  }
  if (toggleBtn) {
    toggleBtn.textContent = list.classList.contains("models-hidden")
      ? "👁 Show models" : "🙈 Hide models";
  }
  // per-provider manual test: click fires ONE request PER MODEL, all in
  // parallel — each model's row flips from "testing…" to its result (and
  // is saved server-side, see test_provider_model) the instant that one
  // model's own response comes back, independent of every other model.
  // Nothing here waits for the slowest model before showing the fast ones.
  if (!list.dataset.hooked) {
    list.dataset.hooked = "1";
    list.addEventListener("click", async (ev) => {
      const b = ev.target.closest('[data-role="provider-test"]');
      if (!b) return;
      const name = b.dataset.provider;
      // Testing one specific provider reveals just that provider's card,
      // even while the main toggle is hidden — everything else stays
      // hidden. Session-only (see FORCE_SHOWN_PROVIDERS above): a real
      // page refresh drops back to fully hidden.
      if (list.classList.contains("models-hidden")) {
        FORCE_SHOWN_PROVIDERS.add(name);
        // Reveal immediately too, not just on the resync at the end —
        // the Set alone only takes effect the next time rows are rebuilt.
        const cardEl = $(`[data-provider-row="${CSS.escape(name)}"]`);
        if (cardEl) cardEl.classList.add("force-show");
      }
      b.disabled = true;
      const prevLabel = b.textContent;
      await testProviderStreaming(name, b);
      b.disabled = false;
      b.textContent = prevLabel;
      // final resync with the server's own aggregate numbers (covers any
      // other counters — latency, credential health — the header doesn't
      // track locally), without touching the per-model rows already painted.
      loaders.providers();
    });
  }
  const testAllBtn = $("#btn-providers-test-all");
  if (testAllBtn && !testAllBtn.dataset.hooked) {
    testAllBtn.dataset.hooked = "1";
    testAllBtn.onclick = async () => {
      testAllBtn.disabled = true;
      const prevLabel = testAllBtn.textContent;
      // Same streaming UI as a single provider/connection Test click, just
      // fired for every provider AND every Gateway connection at once, all
      // in parallel — every model's row (and every provider's live call/
      // err count) updates the instant that one model's own result lands,
      // nothing here waits for the whole run to finish.
      const providerNames = Object.keys(PROVIDER_MODELS);
      const connectionKeys = Object.keys(GATEWAY_MODELS);
      const total = providerNames.length + connectionKeys.length;
      let done = 0;
      testAllBtn.textContent = `⏳ Testing all (0/${total})…`;
      const bumpProgress = () => {
        done += 1;
        testAllBtn.textContent = `⏳ Testing all (${done}/${total})…`;
      };
      try {
        await Promise.allSettled([
          ...providerNames.map((name) =>
            testProviderStreaming(name).then(bumpProgress)),
          ...connectionKeys.map((key) =>
            testGatewayConnectionStreaming(key).then(bumpProgress)),
        ]);
      } finally {
        testAllBtn.disabled = false;
        testAllBtn.textContent = prevLabel;
        loaders.providers();
      }
    };
  }
  renderGatewayCard(r.ok ? (r.data.astra_ai_gateway || null) : null);
};

// Streams a single provider's every model test, live — the same routine
// the single "🧪 Test" button uses, factored out so Test All can run it
// for every provider in parallel without duplicating the logic. `btn`
// (optional) gets its label updated while this provider's own test runs;
// omit it when called as part of a bulk Test All (the bulk button owns
// its own progress label instead).
async function testProviderStreaming(name, btn) {
  const models = PROVIDER_MODELS[name] || [];
  const tableEl = $(`[data-provider-row="${CSS.escape(name)}"] [data-role="model-table"]`);
  if (!models.length) {
    if (tableEl) tableEl.innerHTML = `<div class="model-health-empty">no model configured</div>`;
    return;
  }
  if (btn) btn.textContent = `⏳ Testing ${models.length} model${models.length === 1 ? "" : "s"}…`;

  // Wipe THIS provider's previously saved calls/errors/latency before the
  // fresh run starts — a test's numbers should be this test's own result,
  // not the old save with new numbers piled on top. Scoped to just this
  // one provider; every other provider's saved health is left untouched.
  const countsEl = $(`[data-provider-row="${CSS.escape(name)}"] [data-role="provider-counts"]`);
  const renderCounts = (calls, errors) => {
    if (!countsEl) return;
    countsEl.dataset.calls = String(calls);
    countsEl.dataset.errors = String(errors);
    countsEl.textContent = `${countsEl.dataset.label} · ${countsEl.dataset.modelcount} model(s) · ` +
      `${countsEl.dataset.keycount || 0} key(s) · ${calls} calls · ${errors} err`;
  };
  try {
    const reset = await post(`/api/v1/providers/${encodeURIComponent(name)}/reset-health`);
    renderCounts((reset.ok && reset.data && reset.data.calls) || 0,
                 (reset.ok && reset.data && reset.data.errors) || 0);
  } catch (_e) { /* reset failing shouldn't block the test itself */ }

  // "20 calls · 3 err" in the header — bumped by +1 call (and +1 err on
  // failure) the instant EACH model's own result lands, on top of the
  // zeroed count the reset above just set, instead of waiting for the
  // whole test batch to finish before the number moves.
  const bumpCounts = (ok) => {
    if (!countsEl) return;
    renderCounts((parseInt(countsEl.dataset.calls, 10) || 0) + 1,
                 (parseInt(countsEl.dataset.errors, 10) || 0) + (ok ? 0 : 1));
  };

  // Provider has API keys the server told us about: test EVERY model through
  // EVERY key (one request per model x key, all in parallel). Each key's chip
  // flips from "testing" to its result the moment that one call returns, and
  // the server saves it against (provider, key, model).
  const keys = PROVIDER_KEYS[name] || [];
  if (keys.length) {
    await testProviderKeysStreaming(name, models, keys, tableEl, (r) => bumpCounts(r.ok));
    return;
  }

  PROVIDER_MODEL_RESULTS[name] = PROVIDER_MODEL_RESULTS[name] || [];
  await streamModelTests(models, tableEl, PROVIDER_MODEL_RESULTS[name],
    (modelId) => post(`/api/v1/providers/${encodeURIComponent(name)}/test/${encodeURIComponent(modelId)}`)
      .then((res) => (res.ok && res.data) ? res.data :
        { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" }),
    (result) => bumpCounts(result.ok));
}

async function testProviderKeysStreaming(name, models, keys, tableEl, onResult) {
  const rows = models.map((m) => ({
    model: m,
    keys: keys.map((k) => ({ key_id: k.key_id, label: k.label, pending: true })),
  }));
  PROVIDER_MODEL_RESULTS[name] = rows;
  if (tableEl) tableEl.innerHTML = modelHealthRowsHtml(rows);
  const repaint = (row) => {
    if (!tableEl) return;
    const el = $(`[data-model-row="${CSS.escape(row.model)}"]`, tableEl);
    if (el) el.outerHTML = modelHealthRowsHtml([row]);
  };
  const probes = [];
  rows.forEach((row) => row.keys.forEach((slot) => {
    probes.push(
      post(`/api/v1/providers/${encodeURIComponent(name)}/test/${encodeURIComponent(row.model)}` +
           `?key=${encodeURIComponent(slot.key_id)}`)
        .then((res) => (res.ok && res.data) ? res.data
          : { ok: false, latency_ms: 0, error: res.error || "test failed" })
        .catch((e) => ({ ok: false, latency_ms: 0, error: String(e) }))
        .then((r) => {
          Object.assign(slot, { pending: false, ok: !!r.ok, latency_ms: r.latency_ms,
                                error: r.error, tested_at: new Date().toLocaleString() });
          repaint(row);
          if (onResult) onResult(r);
        }));
  }));
  // allSettled: one key/model failing never stops the rest from finishing.
  return Promise.allSettled(probes);
}

/* -------------------------- Astra AI Gateway ------------------------- */
const GATEWAY_LABELS = {
  "astra-gw-gemini": "Gemini",
  "astra-gw-groq": "Groq",
  "astra-gw-cloudflare": "Cloudflare",
  "astra-gw-bedrock": "Bedrock",
};
// Model-level Gateway test results, kept client-side for the same reason
// as PROVIDER_MODEL_RESULTS above.
const GATEWAY_MODEL_RESULTS = {};   // connection name -> [{model, ok, latency_ms, error, pending}]
// Connection key -> its model id list, refreshed on every render — same
// role as PROVIDER_MODELS, read by the per-model test instead of
// re-fetching.
const GATEWAY_MODELS = {};

// Best-average-latency across a connection's tracked models — used purely
// to *display* connections fastest/healthiest first, mirroring how the
// Gateway itself prefers a healthy, low-latency target when it fallbacks.
function _connAvgLatency(c) {
  const rows = Object.values(c.model_health || {});
  if (!rows.length) return null;
  const withLatency = rows.filter((h) => h.average_latency_ms || h.avg_latency_ms);
  if (!withLatency.length) return null;
  const vals = withLatency.map((h) => h.average_latency_ms ?? h.avg_latency_ms);
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

// Streams one Gateway connection's every model test, live — the exact
// same per-model, parallel, no-waiting behaviour testProviderStreaming
// uses for providers, so a single connection's "🧪 Test" and the bulk
// Test All look and behave identically. `btn` (optional) gets its label
// updated while this connection's own test runs.
async function testGatewayConnectionStreaming(key, btn) {
  const models = GATEWAY_MODELS[key] || [];
  const tableEl = $(`[data-gw-conn="${CSS.escape(key)}"] [data-role="gw-model-table"]`);
  if (!models.length) {
    if (tableEl) tableEl.innerHTML = `<div class="model-health-empty">no model configured</div>`;
    return;
  }
  if (btn) btn.textContent = `⏳ Testing ${models.length} model${models.length === 1 ? "" : "s"}…`;
  GATEWAY_MODEL_RESULTS[key] = GATEWAY_MODEL_RESULTS[key] || [];
  await streamModelTests(models, tableEl, GATEWAY_MODEL_RESULTS[key],
    (modelId) => post(`/api/v1/gateway/${encodeURIComponent(key)}/test/${encodeURIComponent(modelId)}`)
      .then((res) => (res.ok && res.data) ? res.data :
        { model: modelId, ok: false, latency_ms: 0, error: res.error || "test failed" }));
}

async function runGatewayConnectionTest(key, btn, card) {
  // Testing one specific connection reveals just that connection's card,
  // even while the main toggle is hidden — same override as the provider
  // side. Session-only: cleared on toggle click, never persisted, so a
  // real page refresh drops back to fully hidden.
  if (card && card.classList.contains("models-hidden")) {
    FORCE_SHOWN_GATEWAY.add(key);
    const cardEl = $(`[data-gw-conn="${CSS.escape(key)}"]`);
    if (cardEl) cardEl.classList.add("force-show");
  }
  if (btn) { btn.disabled = true; btn.dataset.prev = btn.textContent; }
  try {
    await testGatewayConnectionStreaming(key, btn);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.prev; }
    loaders.providers();
  }
}

function renderGatewayCard(core) {
  const card = $("#gateway-card");
  if (!card) return;
  // Same persisted hide/show restore as loaders.providers — must happen
  // before the early "not configured" return too, so the state is already
  // applied by the time the connection rows exist on a later render.
  if (_loadModelsHiddenState().gateway) card.classList.add("models-hidden");
  const conns = Object.entries((core && core.connections) || {});
  if (!core || core.state === "not_configured" || conns.length === 0) {
    card.innerHTML = `<div class="row"><span class="status-dot warn"></span>` +
      `<b>Not configured</b></div>` +
      `<div class="hint muted">Set any GW_*_API_KEYS / GW_*_CREDENTIALS to enable ` +
      `the Astra AI Gateway (fallback: Gemini → Groq → Cloudflare → Bedrock).</div>`;
    return;
  }
  const dots = { healthy: "ok", degraded: "warn", not_configured: "warn", unhealthy: "bad" };
  // healthy-and-fastest first, so the card visually matches actual
  // fallback preference — never a fixed Gemini→Groq→Cloudflare→Bedrock order.
  const ranked = conns.slice().sort(([, a], [, b]) => {
    const ah = a.state === "healthy" ? 0 : 1;
    const bh = b.state === "healthy" ? 0 : 1;
    if (ah !== bh) return ah - bh;
    const al = _connAvgLatency(a); const bl = _connAvgLatency(b);
    if (al == null && bl == null) return 0;
    if (al == null) return 1;
    if (bl == null) return -1;
    return al - bl;
  });
  const rows = ranked.map(([key, c]) => {
    const label = GATEWAY_LABELS[key] || key;
    const dot = dots[c.state] || "warn";
    const modelCount = (c.models || []).length;
    const models = modelCount ? `${modelCount} model(s)` : "no models";
    GATEWAY_MODELS[key] = c.models || [];
    // First paint after a reload: show the last saved per-model results
    // (same idea as PROVIDER_MODEL_RESULTS restoration in loaders.providers),
    // instead of leaving this connection's table empty until someone
    // clicks Test again.
    if (!(GATEWAY_MODEL_RESULTS[key] || []).length) {
      GATEWAY_MODEL_RESULTS[key] = savedGatewayModelRows(c.models || [], c.model_health || {});
    }
    const forceShow = FORCE_SHOWN_GATEWAY.has(key) ? " force-show" : "";
    return `<div class="provider-card${forceShow}" data-gw-conn="${esc(key)}">` +
      `<div class="provider-card-head">` +
      `<span class="status-dot ${dot}"></span><b>${esc(label)}</b>` +
      `<span class="grow muted">${esc(c.state)} · ${models}</span>` +
      `<button class="btn mini" data-role="gw-test" data-conn="${esc(key)}">🧪 Test (${modelCount || 0} model${modelCount === 1 ? "" : "s"})</button>` +
      `</div>` +
      `<div class="model-health-table" data-role="gw-model-table">` +
      modelHealthRowsHtml(GATEWAY_MODEL_RESULTS[key]) +
      `</div></div>`;
  }).join("");
  card.innerHTML = `<div class="list" style="margin-top:8px">${rows}</div>`;

  // per-connection Test button — tests every model of just that ONE
  // connection (e.g. only Gemini's models), not the whole gateway.
  if (!card.dataset.hooked) {
    card.dataset.hooked = "1";
    card.addEventListener("click", (ev) => {
      const b = ev.target.closest('[data-role="gw-test"]');
      if (!b) return;
      runGatewayConnectionTest(b.dataset.conn, b, card);
    });
  }

  const testBtn = $("#btn-gateway-test");
  if (testBtn && !testBtn.dataset.hooked) {
    testBtn.dataset.hooked = "1";
    testBtn.onclick = async () => {
      testBtn.disabled = true;
      const prevLabel = testBtn.textContent;
      const keys = Object.keys(GATEWAY_MODELS);
      const total = keys.length;
      let done = 0;
      testBtn.textContent = `⏳ Testing gateway (0/${total})…`;
      try {
        await Promise.allSettled(keys.map((key) =>
          testGatewayConnectionStreaming(key).then(() => {
            done += 1;
            testBtn.textContent = `⏳ Testing gateway (${done}/${total})…`;
          })));
      } finally {
        testBtn.disabled = false;
        testBtn.textContent = prevLabel;
        loaders.providers();
      }
    };
  }

  // Hide/show toggle for every connection's per-model rows — same pattern
  // as #btn-providers-toggle-models: state lives as a class on #gateway-card
  // itself, so it survives this function's own re-renders.
  const gwToggleBtn = $("#btn-gateway-toggle-models");
  if (gwToggleBtn && !gwToggleBtn.dataset.hooked) {
    gwToggleBtn.dataset.hooked = "1";
    gwToggleBtn.onclick = () => {
      const hidden = card.classList.toggle("models-hidden");
      gwToggleBtn.textContent = hidden ? "👁 Show models" : "🙈 Hide models";
      const st = _loadModelsHiddenState();
      st.gateway = hidden;
      _saveModelsHiddenState(st);
      FORCE_SHOWN_GATEWAY.clear();
    };
  }
  if (gwToggleBtn) {
    gwToggleBtn.textContent = card.classList.contains("models-hidden")
      ? "👁 Show models" : "🙈 Hide models";
  }
}

/* -------------------------------- router (core) ---------------------------- */
loaders.router = async function () {
  const [m, sr] = await Promise.all([api("/api/v1/models"), api("/api/v1/router/stats")]);
  const list = $("#router-list");
  const blocks = [];
  if (m.ok) {
    const models = m.data.models || [];
    blocks.push(`<div class="panel"><div class="panel-head"><h3>Model registry</h3></div><div class="table">` +
      (models.length ? models.map((md) =>
        `<div class="row"><b>${esc(md.display_name || md.model_id)}</b>` +
        `<span>${esc(md.provider)}</span>` +
        `<span>${esc((md.capabilities || []).slice(0, 5).join("・"))}</span>` +
        `<span>${md.preferred ? "★" : ""}</span></div>`).join("")
        : `<span class="muted">no models — provider API key add korle ekhane asbe</span>`) +
      `</div></div>`);
  }
  if (sr.ok) {
    const task = sr.data.task || {};
    const rows = Object.entries(task).map(([k, v]) =>
      `<div class="row"><b>${esc(k)}</b><span>${esc(String(v))}</span></div>`).join("");
    blocks.push(`<div class="panel"><div class="panel-head"><h3>Task routing stats</h3></div>` +
      `<div class="table">${rows || `<span class="muted">routing kora ekhono bondho — kotha bolo age</span>`}</div></div>`);
  }
  list.innerHTML = blocks.join("");
};

/* ------------------------------ wallet / web3 (core) ----------------------- */
loaders.web3 = async function () {
  const [pol, txs] = await Promise.all([
    api("/api/v1/web3/transaction-policy"), api("/api/v1/web3/transactions")]);
  const lim = (n) => n == null ? "—" : (Number(n) / 1e18).toFixed(4) + " ETH";
  if (pol.ok) {
    const d = pol.data, p = d.policy || {};
    const stopped = d.stopped ? "🚨 EMERGENCY STOP" : "running";
    $("#web3-policy").innerHTML =
      `<div class="table">` +
      `<div class="row"><b>Mode</b><span>${esc(d.mode)} (CONFIRM = review, AUTO = policy-approved)</span></div>` +
      `<div class="row"><b>Status</b><span>${esc(stopped)}</span></div>` +
      `<div class="row"><b>Max per tx</b><span>${esc(lim(p.tx_limit_wei))}</span></div>` +
      `<div class="row"><b>Max daily</b><span>${esc(lim(p.daily_limit_wei))}</span></div>` +
      `<div class="row"><b>Allowlist</b><span>${p.recipients_allowed?.length || 0} recipients · ${p.contracts_allowed?.length || 0} contracts · ${p.wallets_allowed?.length || 0} wallets</span></div>` +
      `</div>`;
  } else {
    $("#web3-policy").innerHTML = `<span class="muted">Web3 unavailable</span>`;
  }
  if (txs.ok) {
    const rows = (txs.data.transactions || []).map((t) =>
      `<div class="row"><b>${esc(String(t.tx_id || t.tx_hash || "").slice(0, 12))}…</b>` +
      `<span>${esc(t.status || "?")}</span>` +
      `<span>${esc(lim(t.value_wei))}</span></div>`).join("");
    $("#web3-txs").innerHTML = rows || `<span class="muted">kono transaction nei</span>`;
  }
};

async function eventsPoll() {
  const r = await api("/api/events?limit=30");
  if (!r.ok) return;
  renderEvents((r.data || []).slice(-10));
}

function openSse(afterId) {
  const url = afterId ? `/api/events/stream?after_id=${encodeURIComponent(afterId)}`
                       : "/api/events/stream";
  const es = new EventSource(url);
  es.onmessage = (ev) => {
    let e = {};
    try { e = JSON.parse(ev.data); } catch (_) { return; }
    feedLine(e);
  };
  es.onerror = () => { /* browser auto-reconnects, resuming via Last-Event-ID */ };
}

// "HH:MM:SS" (24h, as sent by the backend) -> "HH:MM:SS AM/PM" for the
// terminal-style console readout.
function fmtTime12(hms) {
  if (!hms) return "";
  const bits = hms.split(":");
  const h = parseInt(bits[0], 10);
  if (Number.isNaN(h)) return hms;
  const period = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 || 12;
  return `${String(h12).padStart(2, "0")}:${bits[1] || "00"}:${bits[2] || "00"} ${period}`;
}

const GW_NAMES = { gemini: "Gemini", groq: "Groq", cloudflare: "Cloudflare",
                   bedrock: "AWS Bedrock" };
const SRC_LABEL = { gateway: "GATEWAY", provider: "PROVIDER" };

function fmtMs(ms) {
  const n = Number(ms);
  if (ms == null || Number.isNaN(n)) return "";
  return n >= 1000 ? (n / 1000).toFixed(1) + "s" : Math.round(n) + "ms";
}

// "Groq → llama-3.3-70b" : exactly which AI/provider and which model.
function logTarget(src, d) {
  let name = d.provider || "";
  if (src === "gateway") name = GW_NAMES[name] || name;
  if (!name && !d.model) return "";
  return `<b class="term-target">${esc(name || "?")}${d.model ? " → " + esc(d.model) : ""}</b>`;
}

function logBadge(e, status) {
  const k = e.kind || "";
  if (k === "astra_gateway.request") return "🧭";
  if (k.endsWith(".started")) return "📡";
  if (k === "provider.health_changed") return "🩺";
  if (isCorrectionEvent(k) && status === "info") return "🔁";
  return status === "err" ? "❌" : status === "ok" ? "✅" : "•";
}

// One human-readable message per event: WHAT happened + WHICH AI/provider
// and model it happened to (html, already escaped).
function logMessage(e, src) {
  const k = e.kind || "";
  const d = e.data || {};
  const t = logTarget(src, d);
  const lat = d.latency_ms != null ? ` · ${fmtMs(d.latency_ms)}` : "";
  const why = d.error || d.reason;
  const whyTxt = why ? ` · ${esc(String(why))}` : "";
  switch (k) {
    case "astra_gateway.request":
      return d.category === "explicit_model"
        ? `Routing request · explicit model ${esc(d.model || "?")}`
        : `Routing request · ${esc(d.category || "?")} · ${d.candidates != null ? d.candidates : "?"} candidate(s)`;
    case "astra_gateway.success":
    case "ai.completed":
      return `API call OK · ${t}${lat}${d.length != null && d.latency_ms == null ? ` · ${d.length} chars` : ""}`;
    case "astra_gateway.error":
      return `API call FAILED · ${t}${whyTxt}`;
    case "astra_gateway.stream_interrupted":
      return `Stream interrupted · ${t} · partial reply already sent${whyTxt}`;
    case "ai.started":
      return `Calling · ${t}`;
    case "ai.failed":
      if (d.aggregate)
        return `All providers failed · ${d.attempts != null ? d.attempts : "?"} attempt(s)${whyTxt}`;
      return `API call FAILED · ${t}${d.attempt > 1 ? ` · attempt ${d.attempt}` : ""}${whyTxt}`;
    case "gateway.task_completion.correction_requested":
    case "gateway.supervision.correction_requested":
      return `Reply rejected, asking again · ${logTarget("provider", d)}` +
             ` · attempt ${d.attempt != null ? d.attempt : "?"}${d.reason ? " · " + esc(String(d.reason)) : ""}` +
             `${d.got ? ` · got: "${esc(String(d.got))}"` : ""}`;
    case "gateway.task_completion.correction_succeeded":
    case "gateway.supervision.correction_succeeded":
      return `Correction OK · ${logTarget("provider", d)} · attempt ${d.attempt != null ? d.attempt : "?"}`;
    case "gateway.task_completion.correction_failed":
    case "gateway.supervision.correction_failed":
      return `Correction FAILED · ${logTarget("provider", d)}${whyTxt}`;
    case "gateway.task_completion.correction_exhausted":
    case "gateway.supervision.correction_exhausted":
      return `Corrections exhausted · ${logTarget("provider", d)} · ${d.attempts != null ? d.attempts : "?"} attempt(s)${d.reason ? " · " + esc(String(d.reason)) : ""}`;
    case "provider.health_changed":
      return `Health changed · ${esc(d.provider || "?")} → ${d.healthy === false ? "DOWN" : "UP"}${whyTxt}`;
    default:
      return `${esc(k)} ${feedText(d)}`.trim();
  }
}

function feedLine(e) {
  if (LOGS.paused) return;   // pause just stops new lines from appearing
  if (!isImportantEvent(e)) return;   // chat/task/tool/etc noise stays out
  const feed = $("#live-feed");
  if (!feed) return;
  if (feed.firstElementChild && feed.firstElementChild.classList.contains("empty"))
    feed.innerHTML = "";

  // New lines are prepended to the top (newest-first). If the user has
  // scrolled down into older entries, inserting above them must not yank
  // their view — so remember where they were and where the top of the
  // scrollable content is right now.
  const pinnedToTop = feed.scrollTop <= 4;
  const prevScrollTop = feed.scrollTop;
  const prevScrollHeight = feed.scrollHeight;

  const src = logSource(e);
  const status = logStatus(e);
  const cats = [...logCategories(e)];

  // Running counters for the stats strip. Gateway / Provider API calls
  // are counted once per call (terminal event only — see logOutcome), and
  // Success/Errors are the outcomes of those same calls.
  LOGS.counts.total++;
  const outcome = logOutcome(e);
  if (outcome && LOGS.counts[src] != null) {
    LOGS.counts[src]++;
    if (outcome === "ok") LOGS.counts.success++; else LOGS.counts.errors++;
  }
  renderLogStats();

  const div = document.createElement("div");
  div.className = "log-row term-line" + (status === "err" ? " err" : status === "ok" ? " ok" : "");
  div.dataset.cats = cats.join(",");
  const when = fmtTime12((e.created_at || "").split(" ")[1] || "");
  div.innerHTML =
    `<span class="term-time">[${esc(when)}]</span>` +
    `<span class="term-src ${src === "gateway" ? "gw" : "pv"}">${SRC_LABEL[src] || "SYSTEM"}</span>` +
    `<span class="term-badge">${logBadge(e, status)}</span>` +
    `<span class="term-msg">${logMessage(e, src)}</span>`;
  const text = `${SRC_LABEL[src] || ""} ${div.textContent}`.toLowerCase();
  div.dataset.text = text;
  const matchesFilter = LOGS.filter === "all" || cats.includes(LOGS.filter);
  const matchesQuery = !LOGS.query || text.includes(LOGS.query);
  div.classList.toggle("hidden", !(matchesFilter && matchesQuery));
  feed.prepend(div);
  while (feed.children.length > LOGS_MAX_BUFFER) feed.removeChild(feed.lastChild);

  // Restore the scroll anchor: stay pinned to the newest line if that's
  // where the user already was, otherwise hold their reading position
  // steady by compensating for the height just added above it.
  if (pinnedToTop) {
    feed.scrollTop = 0;
  } else {
    feed.scrollTop = prevScrollTop + (feed.scrollHeight - prevScrollHeight);
  }
}
function feedText(d) {
  if (!d) return "";
  const parts = [];
  if (d.task) parts.push("task:" + d.task);
  if (d.provider) parts.push(d.provider);
  if (d.model) parts.push(d.model);
  if (d.latency_ms != null) parts.push(d.latency_ms + "ms");
  if (d.error) parts.push("err:" + d.error);
  if (d.reason) parts.push(d.reason);
  if (parts.length) return "· " + esc(parts.join(" "));
  const pick = ["goal", "tool", "step", "title", "name", "description", "status", "workflow"];
  for (const k of pick) if (d[k] && typeof d[k] === "string") return "· " + esc(d[k]);
  try { return "· " + esc(JSON.stringify(d)); } catch (_) { return ""; }
}
function renderEvents(rows) {
  rows.forEach(feedLine);
}

/* ------------------------------ attachments (multimodal) --------------------- */
const _pendingAttachments = [];
let _attachIdCounter = 0;

function _fileIcon(name) {
  const ext = (name || "").split(".").pop().toLowerCase();
  const map = {
    pdf: "📄", doc: "📄", docx: "📄", txt: "📝", md: "📝",
    csv: "📊", tsv: "📊", xls: "📊", xlsx: "📊",
    ppt: "📽️", pptx: "📽️",
    json: "🔧", xml: "🔧", yaml: "🔧", toml: "🔧",
    png: "🖼️", jpg: "🖼️", jpeg: "🖼️", webp: "🖼️", gif: "🖼️",
    mp3: "🎵", wav: "🎵", m4a: "🎵", aac: "🎵", ogg: "🎵", flac: "🎵", opus: "🎵",
    mp4: "🎬", webm: "🎬", mov: "🎬", avi: "🎬", mkv: "🎬", m4v: "🎬",
    zip: "📦", tar: "📦", tgz: "📦", gz: "📦",
  };
  return map[ext] || "📎";
}

function _humanSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function attachFiles(files) {
  for (const f of files) {
    _pendingAttachments.push({ id: ++_attachIdCounter, file: f });
  }
  renderAttachmentPreviews();
  updateSendBtnState();
}

function removeAttachment(id) {
  const idx = _pendingAttachments.findIndex((a) => a.id === id);
  if (idx >= 0) _pendingAttachments.splice(idx, 1);
  renderAttachmentPreviews();
  updateSendBtnState();
}

function clearAttachments() {
  _pendingAttachments.length = 0;
  renderAttachmentPreviews();
}

function updateSendBtnState() {
  const input = $("#chat-input");
  const sendBtn = $("#chat-send");
  if (sendBtn) sendBtn.disabled = !input.value.trim() && !_pendingAttachments.length;
}

function renderAttachmentPreviews() {
  const bar = $("#attachments-bar");
  if (!bar) return;
  if (!_pendingAttachments.length) { bar.hidden = true; bar.innerHTML = ""; return; }
  bar.hidden = false;
  bar.innerHTML = _pendingAttachments.map((a) => {
    const f = a.file;
    const icon = _fileIcon(f.name);
    let preview = "";
    if (f.type.startsWith("image/")) {
      // NOTE: blob: URLs require CSP img-src to include blob: (updated in web.py)
      const url = URL.createObjectURL(f);
      preview = `<img src="${url}" alt="${esc(f.name)}" onload="URL.revokeObjectURL(this.src)">`;
    } else if (f.type.startsWith("audio/")) {
      preview = `<span class="attachment-type-icon">🎵</span>`;
    } else if (f.type.startsWith("video/")) {
      preview = `<span class="attachment-type-icon">🎬</span>`;
    } else {
      preview = `<span class="attachment-type-icon">${icon}</span>`;
    }
    return `<div class="attachment-preview" data-id="${a.id}">
      ${preview}
      <div class="attachment-info">
        <span class="attachment-name">${esc(f.name)}</span>
        <span class="attachment-size">${_humanSize(f.size)}</span>
      </div>
      <button type="button" class="attachment-remove" data-id="${a.id}" aria-label="Remove">✕</button>
    </div>`;
  }).join("");
}

// wire attach button + file input
document.addEventListener("click", (e) => {
  if (e.target.closest("#chat-attach")) {
    $("#chat-file-input").click();
  }
  const rmBtn = e.target.closest(".attachment-remove");
  if (rmBtn) removeAttachment(Number(rmBtn.dataset.id));
});
const _fileInput = $("#chat-file-input");
if (_fileInput) _fileInput.addEventListener("change", (e) => {
  if (e.target.files.length) attachFiles(e.target.files);
  e.target.value = "";
});

// drag-and-drop on chat area
const _chatShell = $(".chat-shell");
if (_chatShell) {
  _chatShell.addEventListener("dragover", (e) => { e.preventDefault(); _chatShell.classList.add("drag-over"); });
  _chatShell.addEventListener("dragleave", (e) => { if (!_chatShell.contains(e.relatedTarget)) _chatShell.classList.remove("drag-over"); });
  _chatShell.addEventListener("drop", (e) => {
    e.preventDefault(); _chatShell.classList.remove("drag-over");
    if (e.dataTransfer.files.length) attachFiles(e.dataTransfer.files);
  });
}

/* ------------------------------ artifact display ----------------------------- */
function renderArtifact(a) {
  const el = document.createElement("div");
  el.className = "artifact-card";
  const url = `/api/v1/artifacts/${encodeURIComponent(a.id)}/${encodeURIComponent(a.filename)}`;
  const type = (a.type || a.mime || "").split("/")[0];
  if (type === "image") {
    el.innerHTML = `<img class="artifact-image" src="${esc(url)}" alt="${esc(a.filename)}">
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>`;
  } else if (type === "audio") {
    el.innerHTML = `<audio class="artifact-audio" controls src="${esc(url)}"></audio>
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>`;
  } else if (type === "video") {
    el.innerHTML = `<video class="artifact-video" controls src="${esc(url)}"></video>
      <div class="artifact-label">${_fileIcon(a.filename)} ${esc(a.filename)}</div>`;
  } else {
    el.className = "artifact-card artifact-file";
    el.innerHTML = `<span class="artifact-file-icon">${_fileIcon(a.filename)}</span>
      <div class="artifact-file-info">
        <span class="artifact-file-name">${esc(a.filename)}</span>
        ${a.size ? `<span class="artifact-file-size">${_humanSize(a.size)}</span>` : ""}
      </div>
      <a class="btn mini" href="${esc(url)}" download="${esc(a.filename)}">Download</a>`;
  }
  return el;
}

/* ------------------------------ upload indicator ----------------------------- */
function showUploadIndicator(hasFiles) {
  if (!hasFiles) return;
  let el = $("#upload-indicator");
  if (!el) {
    el = document.createElement("div");
    el.id = "upload-indicator";
    el.className = "upload-indicator";
    el.innerHTML = `<span class="upload-spinner"></span> Uploading & processing…`;
    $(".chat-shell")?.insertBefore(el, $("#chat-form")?.nextSibling || null);
  }
  el.hidden = false;
}
function hideUploadIndicator() {
  const el = $("#upload-indicator");
  if (el) el.hidden = true;
}

/* ---------------------------------- boot ------------------------------------ */
async function boot() {
  const r = await api("/api/manifest");
  if (!r.ok) { $("#netstatus").textContent = "✗"; return; }
  MANIFEST = r.data;
  document.title = MANIFEST.name;
  MANIFEST.tabs.forEach((t) => { TAB_LABELS[t.tab] = t.label; });

  // tab bar
  $("#nav").innerHTML = MANIFEST.tabs.map((t) =>
    `<button data-tab="${esc(t.tab)}" class="tab${t.tab === "dashboard" ? " active" : ""}">${t.label}</button>`).join("");
  $$("#nav .tab").forEach((t) =>
    t.addEventListener("click", () => showTab(t.dataset.tab)));

  // per-plugin tabview + load plugin JS
  const scriptLoads = [];
  MANIFEST.tabs.forEach((t) => {
    if (!t.plugin) return;
    const section = document.createElement("section");
    section.id = `tab-${t.plugin}`;
    section.className = "tabview";
    section.innerHTML = `<div class="empty">Loading ${esc(t.title)}…</div>`;
    $("#view").appendChild(section);
    scriptLoads.push(loadScript(t.js).then(() => {
      const def = Astra.plugins[t.plugin];
      loaders[t.plugin] = async () => {
        if (!def) return;
        if (!Astra.plugins[t.plugin].rendered) {
          Astra.plugins[t.plugin].rendered = true;
          if (def.render) def.render($("#tab-" + t.plugin));
        }
      };
    }));
  });
  await Promise.all(scriptLoads);

  loaders.dashboard();
  // Reopen whichever tab was active before the last refresh, if it still
  // exists; otherwise fall back to Dashboard.
  let initialTab = "dashboard";
  try {
    const saved = localStorage.getItem("astra:active-tab");
    if (saved && $("#tab-" + saved)) initialTab = saved;
  } catch (_) { /* ignore */ }
  showTab(initialTab);
  setInterval(() => {
    if ($("#tab-dashboard").classList.contains("active")) loaders.dashboard();
  }, 60_000);
}

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}

boot();