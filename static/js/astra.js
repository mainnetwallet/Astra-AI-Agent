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

/* ------------------------------ LIVE (core tab) ------------------------------ */
loaders.live = async function () {
  if (!Astra.plugins._liveLoaded) {
    Astra.plugins._liveLoaded = true;
    healthTick();
    setInterval(healthTick, 15_000);
    toolsTick();
    setInterval(toolsTick, 30_000);
    executionsTick();
    setInterval(executionsTick, 10_000);
  }
};

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
function isImportantEvent(kind) {
  return IMPORTANT_EVENT_HEADS.has((kind || "").split(".")[0]);
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
  counts: { total: 0, api: 0, errors: 0, success: 0 },
};
const LOGS_MAX_BUFFER = 300;

const LOG_STAT_DEFS = [
  { key: "total", label: "Total" },
  { key: "api", label: "API calls" },
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

// event kind (dotted, e.g. "router.decision") -> filter categories it
// belongs to. A kind can belong to more than one (e.g. an "ai.failed" event
// is both "api" and "errors").
function logCategories(kind, data) {
  const cats = new Set();
  const head = (kind || "").split(".")[0];
  if (head === "router" || head === "credential") cats.add("router");
  if (head === "astra_gateway") cats.add("gateway");
  if (head === "provider") cats.add("providers");
  if (head === "ai") cats.add("api");
  if (kind === "router.fallback" || (data && data.fallback)) cats.add("fallback");
  if (/failed|error|\.error$/.test(kind || "")) cats.add("errors");
  if (/completed|success|\.success$/.test(kind || "") || kind === "router.decision")
    cats.add("success");
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
      LOGS.counts = { total: 0, api: 0, errors: 0, success: 0 };
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
    .map((el) => $$(".term-time, .term-msg", el).map((s) => s.textContent).join(" "));
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
loaders.providers = async function () {
  const r = await api("/api/providers");
  const list = $("#providers-list");
  if (!r.ok) { list.innerHTML = `<div class="empty">${esc(r.error || "providers unavailable")}</div>`; return; }
  const provs = (r.data && r.data.providers) ? r.data.providers : r.data;
  const rows = Object.entries(provs || {}).map(([n, p]) => {
    const dot = p.healthy ? "🟢" : (p.state === "down" ? "🔴" : "⚪");
    const lat = p.latency_avg_ms == null ? "—" : p.latency_avg_ms + "ms";
    return `<div class="row"><b>${dot} ${esc(n)}</b>` +
      `<span>${esc(p.state || p.healthy || "?")}</span>` +
      `<span>${p.models ? p.models.length : 0} models</span>` +
      `<span>${p.calls || 0} calls · ${p.errors || 0} err</span>` +
      `<span>${esc(lat)}</span></div>`;
  }).join("");
  list.innerHTML = rows || `<div class="empty">kono provider e creds nai (offline mode)</div>`;
  const btn = $("#btn-providers-refresh");
  if (btn && !btn.dataset.hooked) {
    btn.dataset.hooked = "1";
    btn.onclick = async () => { await post("/api/v1/models/refresh"); loaders.providers(); };
  }
  renderGatewayCard(r.ok ? (r.data.astra_ai_gateway || null) : null);
};

/* -------------------------- Astra AI Gateway ------------------------- */
const GATEWAY_LABELS = {
  "astra-gw-gemini": "Gemini",
  "astra-gw-groq": "Groq",
  "astra-gw-cloudflare": "Cloudflare",
  "astra-gw-bedrock": "Bedrock",
};
function renderGatewayCard(core) {
  const card = $("#gateway-card");
  if (!card) return;
  const conns = Object.entries((core && core.connections) || {});
  if (!core || core.state === "not_configured" || conns.length === 0) {
    card.innerHTML = `<div class="row"><span class="status-dot warn"></span>` +
      `<b>Not configured</b></div>` +
      `<div class="hint muted">Set any GW_*_API_KEYS / GW_*_CREDENTIALS to enable ` +
      `the Astra AI Gateway (fallback: Gemini → Groq → Cloudflare → Bedrock).</div>`;
    return;
  }
  const dots = { healthy: "ok", degraded: "warn", not_configured: "warn", unhealthy: "bad" };
  const rows = conns.map(([key, c]) => {
    const label = GATEWAY_LABELS[key] || key;
    const dot = dots[c.state] || "warn";
    const models = (c.models || []).length ? `${(c.models || []).length} model(s)` : "no models";
    return `<div class="card"><div class="card-v"><span class="status-dot ${dot}"></span>` +
      `<b>${esc(label)}</b></div><div class="card-k muted">${esc(c.state)} · ${models}</div></div>`;
  }).join("");
  const dot = dots[core.state] || "warn";
  card.innerHTML = `<div class="row"><span class="status-dot ${dot}"></span>` +
    `<b>${esc(core.state)}</b>` +
    `<span class="muted">· ${(core.models || []).length} model(s) across ${conns.length} connection(s)</span></div>` +
    `<div class="cards mini" style="margin-top:8px">${rows}</div>`;
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

async function healthTick() {
  const r = await api("/api/health");
  if (!r.ok) return;
  const h = r.data;
  const provs = Object.entries(h.checks.providers || {}).map(([n, p]) =>
    `${n}:${p.healthy ? "ok" : "local"}`).join(" | ") || "—";
  const plugins = (h.checks.plugins || []).map((p) =>
    `${p.slug}${p.ok ? "✓" : "✗"}`).join(" ");
  $("#live-health").innerHTML =
    `<div>DB <b>${esc(h.checks.database || "?")}</b> · schema ${esc(h.checks.schema_objects ?? "?")}</div>` +
    `<div>Plugins: ${esc(plugins)}</div>` +
    `<div>AI: ${esc(provs)}</div>` +
    (h.checks.scheduler ? `<div>Scheduler: ${esc(h.checks.scheduler.enabled + " of " + h.checks.scheduler.schedules + " active")}</div>` : "");
}

async function toolsTick() {
  const r = await api("/api/tools");
  if (!r.ok) return;
  const tools = r.data.tools || [];
  $("#live-tools").innerHTML = tools.length ? tools.map(renderTool).join("") :
    `<span class="muted">no tools</span>`;
}
function renderTool(t) {
  const risk = t.risk || "read";
  const badge = { read: "🔒", low_risk_write: "✏️", browser_action: "🌐",
    financial_action: "💰", system_action: "⚙️", admin: "🛡️" }[risk] || "🔧";
  return `<span class="tool-chip" title="${esc(t.description || "")} (${esc(risk)})">${badge} ${esc(t.name)}</span>`;
}

async function executionsTick() {
  const r = await api("/api/agents");
  if (!r.ok) return;
  const rec = r.data.recent || [];
  const last = rec[0];
  $("#live-state").textContent = last ? last.status || "IDLE" : "IDLE";
  if (!last) { $("#live-exec").textContent = "No active execution"; return; }
  const steps = (last.steps || 0);
  $("#live-exec").innerHTML =
    `<div><b>${esc(last.goal || "")}</b> — ${esc(last.status)}</div>` +
    `<div class="muted">${steps} step(s) · exec ${esc(last.execution_id || "")}</div>` +
    (rec.length > 1 ? `<div class="muted">last ${rec.length} runs below ↓</div>` : "");
}

async function eventsPoll() {
  if (!Astra.plugins._liveLoaded) return;
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

const LOG_BADGES = { "task.started": "▶️", "task.completed": "✅",
  "tool.executed": "🔧", "agent.completed": "🏁", "agent.failed": "❌",
  "memory.saved": "🧠", "workflow.completed": "📋", "scheduler.tick": "⏰",
  "task.created": "📌", "execution.started": "🧠",
  "router.request": "🧭", "router.decision": "🎯", "router.fallback": "↩️",
  "router.retry": "🔁", "credential.rotation": "🔑",
  "provider.health_changed": "🩺",
  "astra_gateway.request": "🌐", "astra_gateway.success": "🌐✅",
  "astra_gateway.error": "🌐❌",
  "ai.started": "📡", "ai.completed": "📨", "ai.failed": "⚠️" };

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

function feedLine(e) {
  if (LOGS.paused) return;   // pause just stops new lines from appearing
  if (!isImportantEvent(e.kind)) return;   // chat/task/tool/etc noise stays out
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

  const cats = [...logCategories(e.kind, e.data)];
  const isErr = cats.includes("errors");
  const isOk = !isErr && cats.includes("success");

  // running counters shown in the stats strip above the log list.
  // "API calls" should count distinct calls, not log lines — each call
  // fires a start/request event AND a terminal (success/error) event, so
  // only the terminal one is counted here to avoid double-counting.
  LOGS.counts.total++;
  if ((cats.includes("api") || cats.includes("gateway")) && (isErr || isOk))
    LOGS.counts.api++;
  if (isErr) LOGS.counts.errors++;
  else if (isOk) LOGS.counts.success++;
  renderLogStats();

  const div = document.createElement("div");
  div.className = "log-row term-line" + (isErr ? " err" : isOk ? " ok" : "");
  div.dataset.cats = cats.join(",");
  const when = fmtTime12((e.created_at || "").split(" ")[1] || "");
  const badge = LOG_BADGES[e.kind] || (isErr ? "❌" : isOk ? "✅" : "•");
  const dotClass = isErr ? "bad" : isOk ? "ok" : "info";
  const primaryCat = cats[0] || "general";
  const msg = `${e.kind}${e.agent ? ` [${e.agent}]` : ""} ${feedText(e.data)}`.trim();
  const text = `${e.kind} ${e.agent || ""} ${feedText(e.data)}`;
  div.dataset.text = text.toLowerCase();
  div.innerHTML =
    `<span class="term-time">[${esc(when)}]</span>` +
    `<span class="term-badge">${badge}</span>` +
    `<span class="term-dot ${dotClass}"></span>` +
    `<span class="term-cat">${esc(primaryCat)}</span>` +
    `<span class="term-msg">${esc(msg)}</span>`;
  const matchesFilter = LOGS.filter === "all" || cats.includes(LOGS.filter);
  const matchesQuery = !LOGS.query || text.toLowerCase().includes(LOGS.query);
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