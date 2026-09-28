/* ASTRA AI Agent OS — sidebar shell, Command Center and System Map.
 *
 * Integrates with the existing SPA (astra.js): it reuses api(), esc(), $/$$,
 * showTab(), Astra.loaders and the ONE shared SSE feed (ensureEventStream).
 * All data comes from existing endpoints; anything the backend does not
 * expose renders as "Unavailable". No credentials are ever read or shown.
 */
"use strict";
(function () {
  const M = window.SystemMapModel;
  const OS = (window.AstraOS = { data: {}, events: [], seen: new Set(), tab: null });
  const MAX_EVENTS = 500;

  /* ------------------------------- navigation ------------------------------ */
  // target: an EXISTING tab id (reused, not re-implemented) or a System Map focus.
  const NAV = [
    { id: "system-map", label: "System Map", ic: "🗺️", tab: "system-map" },
    { id: "chat", label: "Chat", ic: "💬", tab: "assistant" },
    { id: "agents", label: "Agents", ic: "🤖", tab: "system-map", focus: "agents" },
    { id: "sched", label: "Scheduler", ic: "⏱️", tab: "workflow" },
    { id: "web3", label: "Web3 Center", ic: "⛓️", tab: "web3" },
    { id: "tools", label: "Tool Center", ic: "🔧", tab: "system-map", focus: "tools" },
    { id: "wf", label: "Workflow Studio", ic: "🔀", tab: "workflow" },
    { id: "ws", label: "Workspace", ic: "🗂️", children: [
      { label: "Terminal", tab: "terminal" }, { label: "Files", tab: "terminal" },
      { label: "Browser", tab: "system-map", focus: "workspace" }] },
    { id: "control", label: "AI Control Plane", ic: "🧠", children: [
      { label: "Providers Health", tab: "providers" }, { label: "Router", tab: "router" }] },
    { id: "command-center", label: "Command Center", ic: "🏠", tab: "command-center" },
    { id: "mem", label: "Memory Center", ic: "🧬", tab: "system-map", focus: "memory" },
    { id: "sec", label: "Security Center", ic: "🛡️", tab: "system-map", focus: "security" },
    { id: "health", label: "System Health", ic: "❤️", tab: "command-center", anchor: "cc-health" },
    { id: "activity", label: "Activity Center", ic: "📡", tab: "logs" },
  ];
  // Rendered below the Quick Actions block (after "Add Tool").
  const NAV_BOTTOM = [
    { id: "settings", label: "Settings", ic: "⚙️", children: [
      { label: "Backup", tab: "backup" }, { label: "Legacy Dashboard", tab: "dashboard" }] },
  ];
  const QUICK = [
    { label: "New Chat", ic: "💬", run: () => { go({ tab: "assistant" }); const b = document.getElementById("chat-clear"); if (b) b.click(); } },
    { label: "Run Workflow", ic: "▶️", run: () => go({ tab: "workflow" }) },
    { label: "Open Terminal", ic: "🖥️", run: () => go({ tab: "terminal" }) },
    { label: "Create Task", ic: "✅", run: () => go({ tab: "workflow" }) },
    { label: "Add Tool", ic: "➕", run: () => go({ tab: "system-map", focus: "tools" }) },
  ];
  const PATH_TABS = { "/command-center": "command-center", "/system-map": "system-map" };

  function go(t, push = true) {
    OS.pendingFocus = t.focus || null;
    OS.pendingAnchor = t.anchor || null;
    const path = t.tab === "command-center" || t.tab === "system-map" ? "/" + t.tab : "/#" + t.tab;
    if (push && location.pathname + location.hash !== path) history.pushState({ tab: t.tab }, "", path);
    showTab(t.tab);
  }

  function buildSidebar() {
    const side = document.getElementById("os-sidebar");
    const item = (n, sub) => `<button class="os-item${sub ? " sub" : ""}" data-t="${esc(n.tab || "")}" data-f="${esc(n.focus || "")}" data-a="${esc(n.anchor || "")}">` +
      (sub ? "" : `<span class="ic">${n.ic}</span>`) + `<span>${esc(n.label)}</span></button>`;
    const navHtml = (n) => n.children
      ? `<div class="os-group" data-g="${n.id}"><button class="os-item" data-toggle="${n.id}"><span class="ic">${n.ic}</span><span>${esc(n.label)}</span><span class="chev">⌄</span></button>` +
        `<div class="os-children">${n.children.map((c) => item(c, true)).join("")}</div></div>`
      : item(n, false);
    side.innerHTML = `<div class="os-brand"><img src="/static/img/logo_icon.png" alt=""><div><b>ASTRA</b><span>AI Agent OS</span></div></div>` +
      NAV.map(navHtml).join("") +
      `<div class="os-quick"><h4>Quick Actions</h4>${QUICK.map((q, i) => `<button class="os-item" data-q="${i}"><span class="ic">${q.ic}</span><span>${esc(q.label)}</span></button>`).join("")}</div>` +
      `<div class="os-bottom">${NAV_BOTTOM.map(navHtml).join("")}</div>`;
    side.addEventListener("click", (e) => {
      const tg = e.target.closest("[data-toggle]");
      if (tg) { tg.parentElement.classList.toggle("open"); return; }
      const q = e.target.closest("[data-q]");
      if (q) { QUICK[+q.dataset.q].run(); closeSide(); return; }
      const b = e.target.closest("[data-t]");
      if (b && b.dataset.t) { go({ tab: b.dataset.t, focus: b.dataset.f, anchor: b.dataset.a }); closeSide(); }
    });
  }
  const closeSide = () => document.body.classList.remove("side-open");

  function syncSidebar(tab) {
    const focus = OS.pendingFocus || "", anchor = OS.pendingAnchor || "";
    const items = $$("#os-sidebar .os-item[data-t]").filter((b) => b.dataset.t === tab);
    // A focus/anchor-specific item wins; otherwise the plain item for the tab.
    const win = items.find((b) => (b.dataset.f && b.dataset.f === focus) || (b.dataset.a && b.dataset.a === anchor))
      || items.find((b) => !b.dataset.f && !b.dataset.a) || items[0];
    $$("#os-sidebar .os-item[data-t]").forEach((b) => b.classList.toggle("active", b === win));
    if (win && win.classList.contains("sub")) win.closest(".os-group").classList.add("open");
  }

  /* --------------------------------- data ---------------------------------- */
  const get = async (p) => { const r = await api(p); return r && r.ok ? r.data : null; };

  async function refreshData() {
    const [health, prov, gw, rstat, rstatus, tools, mem, exp, wfs, sch, tasks, rt, web3, metrics, sm] = await Promise.all([
      get("/api/health"), get("/api/providers"), get("/api/gateway/health"), get("/api/router/stats"),
      get("/api/router/status"), get("/api/tools"), get("/api/memory"), get("/api/experiences"),
      get("/api/workflows"), get("/api/schedules"), get("/api/tasks"), get("/api/runtime/status"),
      get("/api/web3/transaction-policy"), get("/api/metrics"), get("/api/system-map"),
    ]);
    const d = OS.data;
    d.health = health; d.providersRaw = prov; d.providerCards = M.providerCards(prov);
    d.gateway = gw; d.routerStats = rstat; d.router = rstatus;
    d.tools = tools ? M.toolGroups(tools) : null;
    d.memory = Array.isArray(mem) ? mem : null; d.memoryCount = d.memory ? d.memory.length : null; d.memoryOk = !!mem || Array.isArray(mem);
    d.experiences = exp; d.workflows = Array.isArray(wfs) ? wfs : null; d.workflowsOk = Array.isArray(wfs);
    d.schedules = Array.isArray(sch) ? sch : null; d.tasks = Array.isArray(tasks) ? tasks : null;
    d.runtime = rt; d.metrics = metrics;
    d.web3 = web3 ? { mode: web3.mode, stopped: !!web3.stopped, chains: web3.policy && web3.policy.chains_allowed ? web3.policy.chains_allowed.length : null } : null;
    // Optional aggregate endpoint (agents registry / security flags). Absent => Unavailable.
    d.agents = sm && Array.isArray(sm.agents) ? sm.agents : [];
    d.security = sm && Array.isArray(sm.security) ? sm.security : null;
    d.eventsOk = OS.eventsLoaded === true;
    d.sseState = typeof SSE_STATE !== "undefined" ? SSE_STATE : null;
    d.eventCount = OS.events.length;
    return d;
  }

  async function loadHistory() {
    const r = await api("/api/events?limit=200");
    if (r && r.ok) {
      OS.eventsLoaded = true;
      (r.data || []).slice().sort((a, b) => a.id - b.id).forEach(pushEvent);
      const last = OS.events.length ? OS.events[OS.events.length - 1].id : 0;
      if (typeof ensureEventStream === "function") ensureEventStream(last);   // ONE shared SSE feed
    }
  }
  function pushEvent(e) {
    if (!e || e.id == null || OS.seen.has(e.id)) return false;
    OS.seen.add(e.id); OS.events.push(e);
    if (OS.events.length > MAX_EVENTS) OS.seen.delete(OS.events.shift().id);
    return true;
  }
  let raf = 0;
  document.addEventListener("astra:event", (ev) => {
    if (!pushEvent(ev.detail)) return;
    OS.eventsLoaded = true;
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; liveUpdate(); });   // batch → incremental
  });

  /* ------------------------------- helpers --------------------------------- */
  const dur = (ms) => ms == null ? "—" : ms < 1000 ? ms + " ms" : ms < 60000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms / 60000) + " min";
  const val = (v) => (v == null || v === "" ? "Unavailable" : String(v));
  const dot = (s) => `<span class="dot ${esc(s)}"></span>`;
  const isActive = (name) => OS.tab === name;

  /* ---------------------------- Command Center ------------------------------ */
  function ccShell() {
    $("#tab-command-center").innerHTML = `<div class="os-page">
      <h1 class="os-title">ASTRA <em>Command Center</em></h1>
      <div class="os-sub">Real-time control center for your Personal AI OS</div>
      <div class="os-grid os-kpis" id="cc-kpis"></div>
      <div class="os-grid cc-cols">
        <div class="os-panel"><h3>⚡ LIVE OPERATIONS</h3><div id="cc-ops"></div></div>
        <div class="os-panel"><h3>🧭 AI ROUTING</h3><div id="cc-routing"></div></div>
      </div>
      <div class="os-grid cc-cols3">
        <div class="os-panel"><h3>🔌 PROVIDER HEALTH</h3><div id="cc-prov"></div></div>
        <div class="os-panel"><h3>🤖 ACTIVE AGENTS</h3><div id="cc-agents"></div></div>
        <div class="os-panel"><h3>🔧 TOOL ACTIVITY</h3><div id="cc-tools"></div></div>
      </div>
      <div class="os-grid cc-cols3">
        <div class="os-panel"><h3>🔀 WORKFLOW ACTIVITY</h3><div id="cc-wf"></div></div>
        <div class="os-panel"><h3>🧬 MEMORY / EXPERIENCE</h3><div id="cc-mem"></div></div>
        <div class="os-panel" id="cc-health"><h3>❤️ SYSTEM HEALTH</h3><div id="cc-hgrid"></div></div>
      </div>
      <div class="os-panel" style="margin-top:14px"><h3>📡 LIVE EVENT STREAM <span class="small muted" id="cc-sse"></span></h3>
        <div class="os-chips" id="cc-filters">${["all", "Gateway", "Router", "Provider", "Agent", "Tool", "Memory", "Workflow", "Web3", "errors"].map((f, i) => `<button class="os-chip${i ? "" : " active"}" data-f="${f}">${f === "all" ? "All" : f === "errors" ? "Errors" : f}</button>`).join("")}</div>
        <div class="os-feed" id="cc-feed"></div></div></div>`;
    $("#cc-filters").addEventListener("click", (e) => {
      const c = e.target.closest("[data-f]"); if (!c) return;
      OS.evFilter = c.dataset.f; $$("#cc-filters .os-chip").forEach((x) => x.classList.toggle("active", x === c)); renderFeed();
    });
    $("#tab-command-center").addEventListener("click", (e) => {
      const r = e.target.closest("[data-op]"); if (r) return openTrace(r.dataset.op);
      const h = e.target.closest("[data-goto]"); if (h) return go({ tab: h.dataset.goto });
      const p = e.target.closest("[data-prov]"); if (p) return openProvider(p.dataset.prov);
    });
    OS.ccBuilt = true;
  }

  function empty(msg) { return `<div class="os-empty">${esc(msg)}</div>`; }

  function renderCC() {
    if (!OS.ccBuilt) ccShell();
    const d = OS.data, ops = M.operationsFromEvents(OS.events);
    OS.ops = ops;
    $("#cc-kpis").innerHTML = M.kpis(d, ops).map((k) =>
      `<div class="os-kpi ${k.tone}"><div class="l">${esc(k.label)}</div><div class="v">${esc(k.value)}</div><div class="s">${esc(k.sub)}</div></div>`).join("");
    renderOps(); renderRouting(); renderProviders(); renderAgents(); renderToolActivity();
    renderWorkflow(); renderMemory(); renderHealth(); renderFeed();
  }

  function renderOps() {
    const running = (OS.ops || []).slice(0, 25);
    $("#cc-ops").innerHTML = running.length ? `<table class="os-table"><tr><th>ID</th><th>Type</th><th>Status</th><th>Provider</th><th>Model</th><th>Agent</th><th>Duration</th><th>Step</th></tr>` +
      running.map((o) => `<tr class="click" data-op="${esc(o.id)}"><td class="mono">${esc(o.id)}</td><td>${esc(o.type)}</td><td>${dot(o.status)}${esc(o.status)}</td><td>${esc(o.provider || "—")}</td><td>${esc(o.model || "—")}</td><td>${esc(o.agent || "—")}</td><td>${dur(o.durationMs)}</td><td>${esc(o.step || "—")}</td></tr>`).join("") + `</table>`
      : empty(OS.eventsLoaded ? "No operations in the recent event history" : "Unavailable — event history could not be loaded");
  }

  function renderRouting() {
    const d = OS.data, gw = d.gateway, last = (d.routerStats && d.routerStats.last_route) || {};
    const kv = (k, v) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;
    $("#cc-routing").innerHTML =
      kv("Gateway (orchestration + verification)", gw ? gw.state : "Unavailable") +
      kv("AstraRouter (routing brain)", d.router ? "active" : "Unavailable") +
      kv("Selected provider", last.provider || "None yet") + kv("Selected model", last.model || "None yet") +
      kv("Last route reason", last.reason || last.task_type || "Unavailable") +
      `<div class="small muted" style="margin-top:8px">Gateway ≠ Provider · AstraRouter ≠ Provider</div>`;
  }

  function renderProviders() {
    const c = OS.data.providerCards || [];
    $("#cc-prov").innerHTML = c.length ? c.map((p) =>
      `<div class="kv" data-prov="${esc(p.name)}" style="cursor:pointer"><span>${dot(p.status)}${esc(p.name)} <span class="small muted">${p.modelCount} models</span></span>` +
      `<span>${p.latencyMs == null ? "—" : Math.round(p.latencyMs) + " ms"} · ${p.successRate == null ? "—" : p.successRate + "%"}</span></div>`).join("")
      : empty("No providers configured");
  }

  function renderAgents() {
    const run = (OS.ops || []).filter((o) => o.type === "Agent" && o.status === "running");
    $("#cc-agents").innerHTML = run.length ? run.map((o) =>
      `<div class="kv" data-op="${esc(o.id)}" style="cursor:pointer"><span>${dot("running")}${esc(o.agent || "agent")}</span><span>${esc(o.step)} · ${dur(o.durationMs)}</span></div>`).join("")
      : empty("No agents running");
  }

  function renderToolActivity() {
    const rows = (OS.ops || []).filter((o) => o.type === "Tool" || o.type === "Terminal").slice(0, 8);
    $("#cc-tools").innerHTML = rows.length ? rows.map((o) =>
      `<div class="kv" data-op="${esc(o.id)}" style="cursor:pointer"><span>${dot(o.status)}${esc(o.tool || o.step)}</span><span>${esc(o.agent || "—")} · ${dur(o.durationMs)}</span></div>`).join("")
      : empty("No recent tool calls");
  }

  function renderWorkflow() {
    const d = OS.data, wfOps = (OS.ops || []).filter((o) => o.type === "Workflow");
    const n = (s) => wfOps.filter((o) => o.status === s).length;
    $("#cc-wf").innerHTML =
      `<div class="kv"><span>Defined</span><span>${val(d.workflows && d.workflows.length)}</span></div>` +
      `<div class="kv"><span>Scheduled</span><span>${val(d.schedules && d.schedules.length)}</span></div>` +
      `<div class="kv"><span>Running</span><span>${d.eventsOk ? n("running") : "Unavailable"}</span></div>` +
      `<div class="kv"><span>Completed (recent)</span><span>${d.eventsOk ? n("completed") : "Unavailable"}</span></div>` +
      `<div class="kv"><span>Failed (recent)</span><span>${d.eventsOk ? n("failed") : "Unavailable"}</span></div>` +
      `<div class="os-bar" style="margin-top:8px"><button class="os-btn" data-goto="workflow">Run Workflow</button><button class="os-btn" data-goto="workflow">Create Workflow</button><button class="os-btn" data-goto="workflow">Open Scheduler</button></div>`;
  }

  function renderMemory() {
    const d = OS.data, e = d.experiences;
    const recent = (d.memory || []).slice(0, 4).map((m) => `<div class="small muted">• ${esc(String(m.content || m.text || m.key || "").slice(0, 80))}</div>`).join("");
    $("#cc-mem").innerHTML =
      `<div class="kv"><span>Memories (recent list)</span><span>${val(d.memoryCount)}</span></div>` +
      `<div class="kv"><span>Experience records</span><span>${e ? e.total : "Unavailable"}</span></div>` +
      `<div class="kv"><span>Successful outcomes</span><span>${e ? e.successful : "Unavailable"}</span></div>` + (recent || "");
  }

  function renderHealth() {
    const rows = M.subsystemHealth(OS.data);
    $("#cc-hgrid").innerHTML = rows.map((h) =>
      `<div class="kv"><span>${dot(h.status === "healthy" ? "online" : h.status === "degraded" ? "degraded" : "")}${esc(h.name)}</span><span>${esc(h.status)}${h.detail ? " · " + esc(h.detail) : ""}</span></div>`).join("");
  }

  function renderFeed() {
    const rows = M.filterEvents(OS.events.slice(-150).map(M.eventRow), OS.evFilter || "all").slice(-60).reverse();
    const el = $("#cc-feed"); if (!el) return;
    el.innerHTML = rows.length ? rows.map((r) => `<div class="row${r.error ? " err" : ""}">${esc(r.time)}<span class="src">[${esc(r.source)}]</span>${esc(r.text)}</div>`).join("") : empty("Listening for events…");
    const s = $("#cc-sse"); if (s) s.textContent = "● " + (typeof SSE_STATE !== "undefined" ? SSE_STATE : "unavailable");
  }

  // SSE → touch only the panels that depend on events (no full re-render).
  function liveUpdate() {
    OS.data.eventCount = OS.events.length; OS.data.eventsOk = true;
    if (isActive("command-center") && OS.ccBuilt) {
      OS.ops = M.operationsFromEvents(OS.events);
      renderOps(); renderAgents(); renderToolActivity(); renderFeed();
    }
    if (isActive("system-map") && OS.mapBuilt) mapLive();
  }

  /* ------------------------------ detail drawer ----------------------------- */
  function drawer(title, html) {
    let d = document.getElementById("os-drawer");
    d.innerHTML = `<button class="x" aria-label="Close">×</button><h3>${esc(title)}</h3>${html}`;
    d.classList.add("open");
  }
  const kvHtml = (rows) => rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(val(v))}</span></div>`).join("");

  function openProvider(name) {
    const p = (OS.data.providerCards || []).find((x) => x.name === name); if (!p) return;
    drawer("Provider · " + p.name, kvHtml([["Status", p.status], ["State", p.state], ["Models", p.modelCount], ["Latency (avg)", p.latencyMs == null ? null : Math.round(p.latencyMs) + " ms"],
      ["Calls", p.calls], ["Errors", p.errors], ["Success rate", p.successRate == null ? null : p.successRate + "%"], ["Credentials configured", p.keyCount]]) +
      `<h4>Models</h4><div class="small">${p.models.slice(0, 80).map(esc).join("<br>") || "Unavailable"}</div><div class="small muted" style="margin-top:8px">API keys are never shown.</div>`);
  }
  function openTool(name) {
    let t = null; ((OS.data.tools || {}).groups || []).forEach((g) => g.tools.forEach((x) => { if (x.name === name) t = x; }));
    if (!t) return;
    drawer("Tool · " + t.name, `<p class="small">${esc(t.description)}</p>` + kvHtml([["Category", t.category], ["Risk", t.risk], ["Requires confirmation", t.requiresConfirmation ? "Yes" : "No"],
      ["Agent-forbidden", t.agentForbidden ? "Yes" : "No"], ["Source", t.plugin], ["Calls", t.calls], ["Errors", t.errors]]) +
      `<h4>Arguments schema</h4><pre>${esc(t.schema && Object.keys(t.schema).length ? JSON.stringify(t.schema, null, 2) : "Unavailable")}</pre>`);
  }
  function openTrace(id) {
    const evs = OS.events.filter((e) => e.data && (e.data.op === id || e.data.operation_id === id || e.data.request_id === id));
    const o = (OS.ops || []).find((x) => x.id === id);
    drawer("Execution Trace · " + id, (o ? kvHtml([["Type", o.type], ["Status", o.status], ["Provider", o.provider], ["Model", o.model], ["Agent", o.agent], ["Duration", dur(o.durationMs)]]) : "") +
      `<h4>Events</h4><div class="os-feed">${evs.map(M.eventRow).map((r) => `<div class="row${r.error ? " err" : ""}">${esc(r.time)} ${esc(r.text)}</div>`).join("") || empty("No events")}</div>`);
  }
  function openNode(id) {
    const n = M.buildNodes(OS.data).find((x) => x.id === id); if (!n) return;
    let extra = "";
    if (id === "providers") extra = (OS.data.providerCards || []).map((p) => `<div class="kv" data-dprov="${esc(p.name)}" style="cursor:pointer"><span>${dot(p.status)}${esc(p.name)}</span><span>${p.modelCount} models ›</span></div>`).join("");
    if (id === "tools") extra = ((OS.data.tools || {}).groups || []).map((g) => `<h4>${esc(g.name)} (${g.tools.length})</h4>` + g.tools.map((t) => `<div class="kv" data-dtool="${esc(t.name)}" style="cursor:pointer"><span>${esc(t.name)}</span><span>›</span></div>`).join("")).join("");
    const tabFor = { workflows: "workflow", web3: "web3", workspace: "terminal", router: "router", monitoring: "logs" }[id];
    drawer(n.title, kvHtml(n.lines.map((l) => [l.k, l.v])) + extra + (tabFor ? `<div class="os-bar" style="margin-top:12px"><button class="os-btn" data-dgo="${tabFor}">Open page ›</button></div>` : ""));
  }
  document.addEventListener("click", (e) => {
    const d = document.getElementById("os-drawer");
    if (!d) return;
    if (e.target.closest("#os-drawer .x")) return d.classList.remove("open");
    const a = e.target.closest("#os-drawer [data-dprov]"); if (a) return openProvider(a.dataset.dprov);
    const b = e.target.closest("#os-drawer [data-dtool]"); if (b) return openTool(b.dataset.dtool);
    const c = e.target.closest("#os-drawer [data-dgo]"); if (c) { d.classList.remove("open"); go({ tab: c.dataset.dgo }); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { document.getElementById("os-drawer").classList.remove("open"); const w = $(".map-wrap.full"); if (w) w.classList.remove("full"); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); document.getElementById("os-search").focus(); }
  });

  /* ------------------------------- System Map ------------------------------- */
  const V = { x: 0, y: 0, k: 1 };
  function mapShell() {
    $("#tab-system-map").innerHTML = `<div class="os-page">
      <h1 class="os-title">ASTRA <em>System Map</em></h1>
      <div class="os-sub">Complete architecture, real-time data flow and component status of ASTRA AI Agent OS</div>
      <div class="os-bar">
        <div class="os-seg" id="map-views"><button class="active" data-v="map">Map View</button><button data-v="list">List View</button><button data-v="metrics">Metrics View</button></div>
        <button class="os-btn" id="map-fit">Auto Layout</button><button class="os-btn" id="map-full">Fullscreen</button>
        <button class="os-btn" id="map-zin" aria-label="Zoom in">＋</button><button class="os-btn" id="map-zout" aria-label="Zoom out">－</button>
      </div>
      <div class="os-chips" id="map-filters"><button class="os-chip active" data-g="all">All</button>${["entry", "core", "providers", "agents", "tools", "memory", "workflows", "web3", "workspace", "security", "monitoring"].map((g) => `<button class="os-chip" data-g="${g}">${g}</button>`).join("")}</div>
      <div id="map-body"></div></div>`;
    OS.mapView = "map"; OS.mapFilter = "all"; OS.mapBuilt = true;
    $("#map-views").addEventListener("click", (e) => { const b = e.target.closest("[data-v]"); if (!b) return; OS.mapView = b.dataset.v; $$("#map-views button").forEach((x) => x.classList.toggle("active", x === b)); renderMap(true); });
    $("#map-filters").addEventListener("click", (e) => { const b = e.target.closest("[data-g]"); if (!b) return; OS.mapFilter = b.dataset.g; $$("#map-filters .os-chip").forEach((x) => x.classList.toggle("active", x === b)); applyDim(); });
    $("#map-fit").addEventListener("click", fit);
    $("#map-full").addEventListener("click", () => { const w = $(".map-wrap"); if (w) { w.classList.toggle("full"); setTimeout(fit, 30); } });
    $("#map-zin").addEventListener("click", () => zoomAt(1.2)); $("#map-zout").addEventListener("click", () => zoomAt(1 / 1.2));
  }

  function renderMap(force) {
    if (!OS.mapBuilt) mapShell();
    const body = $("#map-body"), d = OS.data;
    if (OS.mapView === "map") {
      if (force || !$(".map-wrap")) buildMapDom(body);
      mapLive(true);
      if (OS.pendingFocus) { focusGroup(OS.pendingFocus); OS.pendingFocus = null; }
    } else if (OS.mapView === "list") {
      body.innerHTML = M.buildNodes(d).map((n) => `<div class="os-panel" style="margin-bottom:10px"><h3>${esc(n.title)}</h3>${kvHtml(n.lines.map((l) => [l.k, l.v]))}</div>`).join("");
    } else {
      const c = d.providerCards || [], sum = M.providerSummary(c), req = d.metrics && d.metrics.requests;
      body.innerHTML = `<div class="os-grid os-kpis">${M.kpis(d, OS.ops || []).map((k) => `<div class="os-kpi ${k.tone}"><div class="l">${esc(k.label)}</div><div class="v">${esc(k.value)}</div><div class="s">${esc(k.sub)}</div></div>`).join("")}</div>` +
        `<div class="os-panel"><h3>Providers (${sum.total})</h3>${c.length ? `<table class="os-table"><tr><th>Name</th><th>Status</th><th>Models</th><th>Latency</th><th>Calls</th><th>Errors</th></tr>${c.map((p) => `<tr><td>${esc(p.name)}</td><td>${dot(p.status)}${esc(p.status)}</td><td>${p.modelCount}</td><td>${p.latencyMs == null ? "—" : Math.round(p.latencyMs) + " ms"}</td><td>${val(p.calls)}</td><td>${val(p.errors)}</td></tr>`).join("")}</table>` : empty("No providers configured")}</div>` +
        `<div class="os-panel" style="margin-top:12px"><h3>API requests</h3>${req ? kvHtml([["Total", req.count], ["Errors", req.errors], ["Uptime (s)", d.metrics.uptime_s]]) : empty("Unavailable")}</div>`;
    }
  }

  function buildMapDom(body) {
    body.innerHTML = `<div class="map-wrap" id="map-wrap"><div class="map-stage" id="map-stage"><svg class="map-edges" id="map-edges" width="1480" height="830"></svg></div><div class="map-hint">drag to pan · wheel / pinch to zoom · click a node</div></div>` +
      `<div class="os-panel" style="margin-top:12px"><h3>🔁 AGENT TOOL LOOP</h3><div class="map-loop" id="map-loop"></div></div>`;
    const stage = $("#map-stage");
    M.buildNodes(OS.data).forEach((n) => {
      const el = document.createElement("div");
      el.className = `map-node tone-${n.tone}`; el.dataset.id = n.id; el.dataset.g = n.group;
      Object.assign(el.style, { left: n.x + "px", top: n.y + "px", width: n.w + "px", height: n.h + "px" });
      el.innerHTML = `<h4>${esc(n.title)}</h4><div class="ln-box"></div>`;
      stage.appendChild(el);
    });
    drawEdges(); bindPanZoom($("#map-wrap")); OS.nodeSig = {}; requestAnimationFrame(() => requestAnimationFrame(fit));
    $("#map-stage").addEventListener("click", (e) => {
      if (OS.dragMoved) return;
      const n = e.target.closest(".map-node"); if (!n) return;
      $$(".map-node").forEach((x) => x.classList.toggle("sel", x === n));
      hlEdges(n.dataset.id); openNode(n.dataset.id);
    });
  }

  function drawEdges() {
    const nodes = Object.fromEntries(M.NODE_DEFS.map((n) => [n.id, n]));
    $("#map-edges").innerHTML = M.EDGE_DEFS.map(([a, b, label], i) => {
      const A = nodes[a], B = nodes[b];
      const horiz = B.x >= A.x + A.w - 10;
      const x1 = horiz ? A.x + A.w : A.x + A.w / 2, y1 = horiz ? A.y + A.h / 2 : A.y + A.h;
      const x2 = horiz ? B.x : B.x + B.w / 2, y2 = horiz ? B.y + B.h / 2 : B.y;
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const p = horiz ? `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` : `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
      return `<path data-e="${a}|${b}" d="${p}"/><text x="${mx}" y="${my - 4}" text-anchor="middle">${esc(label)}</text>`;
    }).join("");
  }
  function hlEdges(id) {
    const nb = M.neighbours(id);
    $$("#map-edges path").forEach((p) => { const [a, b] = p.dataset.e.split("|"); p.classList.toggle("hl", a === id || b === id); });
    $$(".map-node").forEach((n) => n.classList.toggle("dim", !nb.has(n.dataset.id)));
    OS.hlNode = id;
  }
  function applyDim() {
    const q = OS.search ? M.searchNodes(M.buildNodes(OS.data), OS.search) : null;
    $$(".map-node").forEach((n) => {
      const okG = OS.mapFilter === "all" || n.dataset.g === OS.mapFilter;
      const okQ = !q || q.has(n.dataset.id);
      n.classList.toggle("dim", !(okG && okQ));
    });
  }
  function focusGroup(f) {
    const map = { agents: "agents", tools: "tools", memory: "memory", security: "security", workspace: "workspace" };
    const id = map[f]; if (!id) return;
    const n = $(`.map-node[data-id="${id}"]`); if (!n) return;
    $$(".map-node").forEach((x) => x.classList.toggle("sel", x === n)); hlEdges(id); openNode(id);
  }

  // Update only nodes whose live content changed (signature compare).
  function mapLive(all) {
    if (!$("#map-stage")) return;
    const nodes = M.buildNodes(OS.data);
    nodes.forEach((n) => {
      const sig = JSON.stringify(n.lines);
      if (OS.nodeSig[n.id] === sig && !all) return;
      if (OS.nodeSig[n.id] === sig) return;
      OS.nodeSig[n.id] = sig;
      const box = $(`.map-node[data-id="${n.id}"] .ln-box`);
      if (box) box.innerHTML = n.lines.slice(0, 9).map((l) => `<div class="ln"><span>${dot(l.s === "info" ? "" : l.s)}${esc(l.k)}</span><span>${esc(val(l.v))}</span></div>`).join("");
    });
    const step = M.activeLoopStep(OS.events);
    const loop = $("#map-loop");
    if (loop) loop.innerHTML = M.TOOL_LOOP.map((s, i) => `<span class="step${i === step ? " on" : ""}">${esc(s)}</span>${i < M.TOOL_LOOP.length - 1 ? "→" : ""}`).join("");
  }

  function applyView() { const s = $("#map-stage"); if (s) s.style.transform = `translate(${V.x}px,${V.y}px) scale(${V.k})`; }
  function fit() {
    const w = $("#map-wrap"); if (!w) return;
    if (w.clientWidth < 700) {            // small screens: readable scale, pan to explore
      V.k = 0.55; V.x = 8; V.y = 8; applyView(); return;
    }
    const k = Math.min(w.clientWidth / 1480, w.clientHeight / 830);
    V.k = Math.max(0.25, Math.min(1.5, k)); V.x = (w.clientWidth - 1480 * V.k) / 2; V.y = (w.clientHeight - 830 * V.k) / 2;
    applyView();
  }
  function zoomAt(f, cx, cy) {
    const w = $("#map-wrap"); if (!w) return;
    cx = cx == null ? w.clientWidth / 2 : cx; cy = cy == null ? w.clientHeight / 2 : cy;
    const k = Math.max(0.2, Math.min(3, V.k * f)), r = k / V.k;
    V.x = cx - (cx - V.x) * r; V.y = cy - (cy - V.y) * r; V.k = k; applyView();
  }
  function bindPanZoom(w) {
    const pts = new Map(); let last = 0;
    w.addEventListener("pointerdown", (e) => { pts.set(e.pointerId, { x: e.clientX, y: e.clientY }); OS.dragMoved = false; last = pts.size === 2 ? Math.hypot(...[...pts.values()].reduce((a, p) => [a[0] - p.x, a[1] - p.y], [0, 0])) : 0; });
    w.addEventListener("pointermove", (e) => {
      if (!pts.has(e.pointerId)) return;
      const p = pts.get(e.pointerId), dx = e.clientX - p.x, dy = e.clientY - p.y;
      p.x = e.clientX; p.y = e.clientY;
      if (pts.size === 1) { if (Math.abs(dx) + Math.abs(dy) > 1) OS.dragMoved = true; V.x += dx; V.y += dy; applyView(); }
      else if (pts.size === 2) {
        const [a, b] = [...pts.values()], dist = Math.hypot(a.x - b.x, a.y - b.y);
        if (last) { const r = w.getBoundingClientRect(); zoomAt(dist / last, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top); OS.dragMoved = true; }
        last = dist;
      }
    });
    const up = (e) => { pts.delete(e.pointerId); last = 0; setTimeout(() => { OS.dragMoved = false; }, 0); };
    w.addEventListener("pointerup", up); w.addEventListener("pointercancel", up);
    w.addEventListener("wheel", (e) => { e.preventDefault(); const r = w.getBoundingClientRect(); zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
    window.addEventListener("resize", () => { if (isActive("system-map")) fit(); });
  }

  /* ------------------------------- tab loaders ------------------------------ */
  let pollTimer = 0;
  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      if (document.hidden || !(isActive("command-center") || isActive("system-map"))) return;
      await refreshData();
      isActive("command-center") ? renderCC() : renderMap(false);
    }, 10000);
  }
  async function open(tab) {
    if (!OS.eventsLoaded) await loadHistory();
    await refreshData();
    if (tab === "command-center") { renderCC(); if (OS.pendingAnchor) { const a = document.getElementById(OS.pendingAnchor); if (a) a.scrollIntoView({ behavior: "smooth" }); OS.pendingAnchor = null; } }
    else renderMap(true);
    startPolling();
  }
  Astra.loaders["command-center"] = () => open("command-center");
  Astra.loaders["system-map"] = () => open("system-map");

  /* ------------------------- shell wiring + routing ------------------------- */
  function initialTab(fallback) {
    if (PATH_TABS[location.pathname]) return PATH_TABS[location.pathname];
    const h = location.hash.replace(/^#/, "");
    if (h && $("#tab-" + h)) return h;
    return !fallback || fallback === "dashboard" ? "command-center" : fallback;
  }
  const origShowTab = window.showTab; let first = true;
  window.showTab = function (name) {
    if (first) { first = false; name = initialTab(name); }
    OS.tab = name;
    origShowTab(name);
    syncSidebar(name);
    const pill = document.getElementById("os-online");
    if (pill && OS.data.health) { pill.textContent = OS.data.health.ok ? "● System Online" : "● Degraded"; pill.classList.toggle("bad", !OS.data.health.ok); }
  };
  window.addEventListener("popstate", () => { const t = initialTab(null); OS.pendingFocus = null; showTab(t); });

  function initShell() {
    document.body.classList.add("os");
    buildSidebar();
    const tb = document.querySelector(".topbar");
    tb.insertAdjacentHTML("afterbegin", `<button id="os-burger" aria-label="Menu">☰</button>`);
    tb.insertAdjacentHTML("beforeend", `<input id="os-search" type="search" placeholder="Search agents, tools, models, tasks…  (Ctrl K)" aria-label="Search"><span class="os-online" id="os-online">● System Online</span><span class="os-clock" id="os-clock"></span>`);
    document.getElementById("os-burger").addEventListener("click", () => document.body.classList.toggle("side-open"));
    document.getElementById("os-scrim").addEventListener("click", closeSide);
    const tick = () => { document.getElementById("os-clock").textContent = new Date().toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" }); };
    tick(); setInterval(tick, 30000);
    let t = 0;
    document.getElementById("os-search").addEventListener("input", (e) => {           // debounced
      clearTimeout(t); const v = e.target.value;
      t = setTimeout(() => { OS.search = v; if (v && !isActive("system-map")) go({ tab: "system-map" }); else applyDim(); }, 200);
    });
    document.getElementById("os-search").addEventListener("keydown", (e) => { if (e.key === "Enter" && isActive("system-map")) { OS.search = e.target.value; applyDim(); } });
  }
  initShell();
  OS.hooks = { initialTab };
})();
