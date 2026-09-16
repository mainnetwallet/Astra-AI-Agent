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

function showTab(name) {
  $$("#nav .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tabview").forEach((v) => v.classList.toggle("active", v.id === `tab-${name}`));
  const loader = loaders[name];
  if (loader) loader();
}

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
  if (!$("#chips").children.length && !$("#chat-log").children.length) welcome();
};
function welcome() {
  const msg = `Asthagato! 👋 Ami apnar Astra AI Agent.

Banglish normal bhashay likhun — ami bujhte parbo.
Chinta korar dorkar nai: plugin jeiba ache & fresh feature add korle AMI
apnakei agami dashboard e dekhabo.

Uporer airdrop plugin er example:
• "add airdrop Hamster deadline 15 oct reward token value 500"
• "add task \\"join tg\\" to Hamster"
• "mark \\"join tg\\" in Hamster done"
• "deadlines this week", "progress", "list wallets"

'help' likhe full plugin list pao. Puro control UI teo ache.`;
  chatBubble("ai", msg);
}
function chatBubble(who, text) {
  const el = document.createElement("div");
  el.className = "bubble " + who;
  el.innerHTML = String(text || "").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  $("#chat-log").appendChild(el);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
}
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#chat-input").value.trim();
  if (!msg) return;
  chatBubble("me", msg);
  $("#chat-input").value = "";
  const t = setTimeout(() => chatBubble("ai", "…"), 300);
  try {
    const r = await post("/api/chat", { message: msg });
    clearTimeout(t);
    const last = $("#chat-log").lastElementChild;
    if (last && last.textContent === "…") last.remove();
    chatBubble("ai", r.data.reply);
    if (r.data.action && r.data.action !== "none") showTab(r.data.action);
    if (r.data.action === "dashboard") loaders.dashboard();
  } catch (err) {
    clearTimeout(t);
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
    initLogsToolbar();
    if (window.EventSource) openSse();
    else setInterval(eventsPoll, 3000);   // fallback for older browsers
  }
};

/* ------------------------------ logs toolbar --------------------------------
 * Category filters, search, pause/resume and clear for the Live activity
 * feed. Purely client-side: every rendered feed-line carries the event kind
 * + a resolved category as data attributes, and the toolbar just toggles
 * visibility / appends nothing while paused. */
const LOGS = { filter: "all", query: "", paused: false, buffer: [] };
const LOGS_MAX_BUFFER = 300;

// event kind (dotted, e.g. "router.decision") -> filter categories it
// belongs to. A kind can belong to more than one (e.g. an "ai.failed" event
// is both "api" and "errors").
function logCategories(kind, data) {
  const cats = new Set();
  const head = (kind || "").split(".")[0];
  if (head === "router" || head === "credential") cats.add("router");
  if (head === "agentrouter") cats.add("agentrouter");
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
    clearBtn.addEventListener("click", () => {
      LOGS.buffer = [];
      $("#live-feed").innerHTML = `<div class="muted">cleared — listening…</div>`;
    });
  }
}

function applyLogsFilter() {
  const feed = $("#live-feed");
  if (!feed) return;
  $$(".feed-line", feed).forEach((el) => {
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
  renderAgentRouterCard(r.ok ? (r.data.agentrouter_core || null) : null);
  initAgentRouterTestButton();
};

/* -------------------------- AgentRouter.org gateway ------------------------- */
function renderAgentRouterCard(core) {
  const card = $("#agentrouter-card");
  if (!card) return;
  if (!core || core.state === "not_configured") {
    card.innerHTML = `<div class="row"><span class="status-dot warn"></span>` +
      `<b>Not configured</b></div>` +
      `<div class="hint muted">Set AGENTROUTER_API_KEYS to enable this gateway.</div>`;
    return;
  }
  const dot = core.state === "healthy" ? "ok" : core.state === "degraded" ? "warn" : "bad";
  card.innerHTML =
    `<div class="row"><span class="status-dot ${dot}"></span>` +
    `<b>${esc(core.state)}</b>` +
    `<span class="muted">· ${(core.models || []).length} model(s) configured</span></div>`;
}

function initAgentRouterTestButton() {
  const btn = $("#btn-agentrouter-test");
  if (!btn || btn.dataset.hooked) return;
  btn.dataset.hooked = "1";
  btn.addEventListener("click", async () => {
    const out = $("#agentrouter-result");
    btn.disabled = true;
    out.textContent = "Testing…";
    try {
      const r = await post("/api/agentrouter/health", { all_keys: true, all_models: true });
      out.innerHTML = renderAgentRouterTestResult(r);
    } catch (err) {
      out.textContent = "✗ AgentRouter test failed to run — " + err;
    } finally {
      btn.disabled = false;
    }
  });
}

function renderAgentRouterTestResult(r) {
  if (!r.ok) return `<span class="status-dot bad"></span>✗ ${esc(r.error || "request failed")}`;
  const d = r.data || {};
  if (!d.configured) {
    return `<span class="status-dot warn"></span>○ Not configured — set AGENTROUTER_API_KEYS`;
  }
  const lines = [];
  if (d.status === "ok") {
    lines.push(`<div><span class="status-dot ok"></span>✓ Connected</div>`);
    lines.push(`<div>✓ Authenticated</div>`);
    lines.push(`<div>✓ Model: ${esc(d.model || "")}</div>`);
    lines.push(`<div>✓ Response received — latency ${esc(String(d.latency_ms))} ms</div>`);
  } else {
    lines.push(`<div><span class="status-dot bad"></span>✗ AgentRouter API failed</div>`);
    lines.push(`<div>${esc(d.detail || d.status || "unknown error")}</div>`);
    lines.push(`<div class="muted">configured: ${d.configured} · reachable: ${d.reachable} · ` +
      `authenticated: ${d.authenticated}</div>`);
  }
  if (d.keys && d.keys.length) {
    lines.push(`<div class="muted" style="margin-top:6px">` +
      d.keys.map((k) => `${esc(k.key)} → ${k.status === "success" ? "✓ success" : "✗ failed"}`)
        .join(" &nbsp;·&nbsp; ") + `</div>`);
  }
  if (d.models && d.models.length) {
    lines.push(`<div class="muted" style="margin-top:6px">` +
      d.models.map((m) => `${m.working ? "✓" : "✗"} ${esc(m.model)}`).join(" &nbsp;·&nbsp; ") +
      `</div>`);
  }
  return lines.join("");
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

function openSse() {
  const es = new EventSource("/api/events/stream");
  es.onmessage = (ev) => {
    let e = {};
    try { e = JSON.parse(ev.data); } catch (_) { return; }
    feedLine(e);
  };
  es.onerror = () => { /* browser auto-reconnects */ };
}

const LOG_BADGES = { "task.started": "▶️", "task.completed": "✅",
  "tool.executed": "🔧", "agent.completed": "🏁", "agent.failed": "❌",
  "memory.saved": "🧠", "workflow.completed": "📋", "scheduler.tick": "⏰",
  "task.created": "📌", "execution.started": "🧠",
  "router.request": "🧭", "router.decision": "🎯", "router.fallback": "↩️",
  "router.retry": "🔁", "credential.rotation": "🔑",
  "provider.health_changed": "🩺",
  "agentrouter.request": "🌐", "agentrouter.success": "🌐✅",
  "agentrouter.error": "🌐❌",
  "ai.started": "📡", "ai.completed": "📨", "ai.failed": "⚠️" };

function feedLine(e) {
  if (LOGS.paused) return;   // pause just stops new lines from appearing
  const feed = $("#live-feed");
  if (!feed) return;
  if (feed.firstElementChild && feed.firstElementChild.classList.contains("muted"))
    feed.innerHTML = "";
  const div = document.createElement("div");
  const cats = [...logCategories(e.kind, e.data)];
  const isErr = cats.includes("errors");
  const isOk = !isErr && cats.includes("success");
  div.className = "feed-line" + (isErr ? " err" : isOk ? " ok" : "");
  div.dataset.cats = cats.join(",");
  const when = (e.created_at || "").split(" ")[1] || "";
  const badge = LOG_BADGES[e.kind] || "•";
  const text = `${e.kind} ${e.agent || ""} ${feedText(e.data)}`;
  div.dataset.text = text.toLowerCase();
  div.innerHTML = `${badge} <code>${esc(when)}</code> <b>${esc(e.kind)}</b> ` +
    `${esc(e.agent || "")} ${feedText(e.data)}`;
  const matchesFilter = LOGS.filter === "all" || cats.includes(LOGS.filter);
  const matchesQuery = !LOGS.query || text.toLowerCase().includes(LOGS.query);
  div.classList.toggle("hidden", !(matchesFilter && matchesQuery));
  feed.prepend(div);
  while (feed.children.length > LOGS_MAX_BUFFER) feed.removeChild(feed.lastChild);
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

/* ---------------------------------- boot ------------------------------------ */
async function boot() {
  const r = await api("/api/manifest");
  if (!r.ok) { $("#netstatus").textContent = "✗"; return; }
  MANIFEST = r.data;
  document.title = MANIFEST.name;

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
  showTab("dashboard");
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