/* ASTRA System Map / Command Center — pure data model.
 *
 * DOM-free on purpose (like log_model.js) so it can be unit-tested under
 * node. Everything here maps REAL backend payloads (/api/providers,
 * /api/tools, /api/events, /api/health ...) to view data. Nothing is
 * invented: a value the backend did not supply is `null` and the UI renders
 * it as "Unavailable". Secrets are never read: provider `keys`, `base_url`
 * and any credential material are deliberately ignored.
 */
(function (root) {
  "use strict";

  const UNAVAILABLE = null;

  /* ------------------------------ helpers -------------------------------- */
  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const arr = (v) => (Array.isArray(v) ? v : []);

  /** Provider state string -> one of online|degraded|rate_limited|offline|unknown. */
  function providerStatus(state, healthy) {
    const s = String(state || "").toLowerCase();
    if (/rate|cool|limit|429|quota/.test(s)) return "rate_limited";
    if (/degrad|warn|partial/.test(s)) return "degraded";
    if (/down|disabled|unhealthy|offline|fail|error/.test(s)) return "offline";
    if (s === "healthy" || s === "ok" || s === "online") return "online";
    if (healthy === true) return "online";
    if (healthy === false) return "offline";
    return "unknown";
  }

  /* ------------------------------ providers ------------------------------ */
  /** /api/providers -> [{name,status,models,modelCount,latencyMs,calls,errors,successRate,keyCount}] */
  function providerCards(payload) {
    const map = isObj(payload) && isObj(payload.providers) ? payload.providers : {};
    return Object.keys(map).sort().map((name) => {
      const p = isObj(map[name]) ? map[name] : {};
      const calls = num(p.calls);
      const errors = num(p.errors);
      const models = arr(p.models).map(String);
      return {
        name,
        status: providerStatus(p.state, p.healthy),
        state: p.state == null ? UNAVAILABLE : String(p.state),
        models,
        modelCount: models.length,
        latencyMs: num(p.latency_avg_ms),
        calls,
        errors,
        successRate: calls && calls > 0 ? Math.round(((calls - (errors || 0)) / calls) * 1000) / 10 : null,
        // Only a COUNT of credentials — never the keys themselves.
        keyCount: num(p.credentials),
      };
    });
  }

  /** Worst-first summary used by the KPI card. */
  function providerSummary(cards) {
    const c = { online: 0, degraded: 0, rate_limited: 0, offline: 0, unknown: 0 };
    cards.forEach((p) => { c[p.status] = (c[p.status] || 0) + 1; });
    return { total: cards.length, ...c };
  }

  /* -------------------------------- tools -------------------------------- */
  const GROUP_LABELS = {
    builtin: "Built-in", browser: "Browser", file: "File", files: "File",
    terminal: "Terminal", runtime: "Terminal", web3: "Web3", custom: "Custom",
  };
  function toolGroupOf(t) {
    const cat = String(t.category || "").toLowerCase();
    if (t.plugin && t.plugin !== "core") return "Custom";
    if (GROUP_LABELS[cat]) return GROUP_LABELS[cat];
    if (!cat) return "Built-in";
    return cat.charAt(0).toUpperCase() + cat.slice(1);
  }
  /** /api/tools -> {groups:[{name,tools:[…]}], total, stats} (real registry only). */
  function toolGroups(payload) {
    const tools = arr(isObj(payload) ? payload.tools : []);
    const stats = isObj(payload) && isObj(payload.stats) ? payload.stats : {};
    const by = {};
    tools.forEach((t) => {
      if (!isObj(t) || !t.name) return;
      const g = toolGroupOf(t);
      const st = isObj(stats[t.name]) ? stats[t.name] : {};
      (by[g] = by[g] || []).push({
        name: String(t.name),
        description: String(t.description || "").trim(),
        category: t.category || null,
        schema: isObj(t.input_schema) ? t.input_schema : null,
        risk: t.risk_level || null,
        requiresConfirmation: !!t.requires_confirmation,
        agentForbidden: !!t.agent_forbidden,
        plugin: t.plugin || null,
        calls: num(st.calls),
        errors: num(st.errors),
      });
    });
    const groups = Object.keys(by).sort().map((name) => ({
      name, tools: by[name].sort((a, b) => a.name.localeCompare(b.name)),
    }));
    return { groups, total: tools.length, stats };
  }

  /* -------------------------------- events ------------------------------- */
  function sourceOfKind(kind) {
    const head = String(kind || "").split(".")[0];
    switch (head) {
      case "astra_gateway": case "gateway": case "chat": case "supervision": return "Gateway";
      case "router": case "credential": return "Router";
      case "ai": case "provider": case "image": return "Provider";
      case "agent": return "Agent";
      case "tool": return "Tool";
      case "terminal": case "host_terminal": return "Terminal";
      case "memory": case "experience": return "Memory";
      case "workflow": case "scheduler": case "task": return "Workflow";
      case "web3": return "Web3";
      case "browser": return "Browser";
      case "operation": return "System";
      default: return "System";
    }
  }
  const TERMINAL_SUFFIX = ["completed", "succeeded", "failed", "error", "timeout", "cancelled",
    "rejected", "confirmed", "done", "finished", "exhausted", "interrupted", "closed", "stopped"];
  const START_SUFFIX = ["started", "request", "created", "opened"];
  function suffixOf(kind) { return String(kind || "").split(".").pop(); }
  function isTerminalEvent(ev) {
    return (ev && ev.data && ev.data.terminal === true) || TERMINAL_SUFFIX.includes(suffixOf(ev && ev.kind));
  }
  function isErrorEvent(ev) {
    const s = suffixOf(ev && ev.kind);
    return /fail|error|timeout|exhausted|rejected|blocked/.test(s) || /\.(failed|error)$/.test(ev && ev.kind || "");
  }
  /** One event -> a display row. Text derives only from the event itself. */
  function eventRow(ev) {
    const d = isObj(ev.data) ? ev.data : {};
    const detail = d.message || d.tool || d.name || d.provider || d.model || d.workflow || d.goal || "";
    const t = String(ev.created_at || "");
    return {
      id: ev.id,
      time: t.length >= 19 ? t.slice(11, 19) : t,
      source: sourceOfKind(ev.kind),
      kind: String(ev.kind || ""),
      text: String(ev.kind || "") + (detail ? " — " + String(detail).slice(0, 120) : ""),
      error: isErrorEvent(ev),
    };
  }

  /**
   * Live operations derived from real lifecycle events.
   * A start event (carrying `op`) opens an operation; a terminal event with
   * the same `op` closes it. Operations open without a close are "running".
   */
  function operationsFromEvents(events, now) {
    const ops = new Map();
    arr(events).forEach((ev) => {
      if (!ev || !ev.kind) return;
      const d = isObj(ev.data) ? ev.data : {};
      const op = d.op || d.operation_id || d.request_id || null;
      if (!op) return;
      const key = String(op);
      const cur = ops.get(key) || {
        id: key, type: sourceOfKind(ev.kind), agent: ev.agent || null,
        provider: null, model: null, tool: null, step: null,
        startedAt: null, endedAt: null, status: "running", kind: ev.kind,
      };
      if (ev.agent && !cur.agent) cur.agent = ev.agent;
      if (d.provider) cur.provider = String(d.provider);
      if (d.model) cur.model = String(d.model);
      if (d.tool) cur.tool = String(d.tool);
      cur.step = ev.kind;
      cur.kind = ev.kind;
      const ts = Date.parse(String(ev.created_at || "").replace(" ", "T"));
      // Only a non-terminal event dates the start. If the window opens on a
      // terminal event (the start is older than the loaded history) the start
      // was never observed, so durationMs stays null -> "Unavailable" rather
      // than a fabricated 0 ms.
      if (!cur.startedAt && !isTerminalEvent(ev) && !isNaN(ts)) cur.startedAt = ts;
      if (isTerminalEvent(ev)) {
        cur.status = isErrorEvent(ev) ? "failed" : "completed";
        cur.endedAt = isNaN(ts) ? null : ts;
      }
      ops.set(key, cur);
    });
    const t = typeof now === "number" ? now : Date.now();
    return [...ops.values()].map((o) => ({
      ...o,
      durationMs: o.startedAt ? Math.max(0, (o.endedAt || t) - o.startedAt) : null,
    })).sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
  }

  /** Keep only events for a source / errors; used by the stream filter. */
  function filterEvents(rows, filter) {
    if (!filter || filter === "all") return rows;
    if (filter === "errors") return rows.filter((r) => r.error);
    return rows.filter((r) => r.source.toLowerCase() === filter.toLowerCase());
  }

  /* -------------------------------- health ------------------------------- */
  /** /api/health + others -> per-subsystem {name,status:healthy|degraded|unavailable,detail}. */
  function subsystemHealth(d) {
    const h = isObj(d.health) ? d.health : null;
    const checks = h && isObj(h.checks) ? h.checks : {};
    const rows = [];
    const add = (name, status, detail) => rows.push({ name, status, detail: detail == null ? null : String(detail) });

    add("API", h ? (h.ok === false ? "degraded" : "healthy") : "unavailable",
      h ? (checks.database === "ok" ? "database ok" : checks.database) : null);

    const gw = d.gateway;
    add("Gateway", isObj(gw) ? (/not_configured/.test(gw.state) ? "unavailable"
      : (gw.state === "healthy" ? "healthy" : "degraded")) : "unavailable",
      isObj(gw) ? gw.state : null);

    const cards = d.providerCards || [];
    if (isObj(d.router) || cards.length) {
      add("Router", "healthy", cards.length + " provider(s) registered");
    } else {
      add("Router", isObj(d.routerStats) ? "healthy" : "unavailable", null);
    }

    const sum = providerSummary(cards);
    add("Providers", !cards.length ? "unavailable"
      : (sum.online === cards.length ? "healthy" : (sum.online ? "degraded" : "degraded")),
      cards.length ? `${sum.online}/${cards.length} online` : "none configured");

    add("ToolRegistry", d.tools && d.tools.total ? "healthy" : "unavailable",
      d.tools ? d.tools.total + " tools" : null);
    add("Memory", d.memoryOk ? "healthy" : "unavailable", null);
    add("Workflow Engine", d.workflowsOk ? "healthy" : "unavailable", null);
    add("EventBus", d.eventsOk ? "healthy" : "unavailable", d.sseState || null);
    add("Web3", isObj(d.web3) ? (d.web3.stopped ? "degraded" : "healthy") : "unavailable",
      isObj(d.web3) ? "mode " + d.web3.mode + (d.web3.stopped ? " (stopped)" : "") : null);
    const rt = d.runtime;
    add("Terminal", isObj(rt) ? (rt.available ? "healthy" : "unavailable") : "unavailable",
      isObj(rt) ? (rt.available ? rt.state : "runtime unavailable") : null);
    return rows;
  }

  /* --------------------------- system map graph --------------------------- */
  /**
   * Static architecture skeleton: node ids, layout slots and edges taken from
   * the documented ASTRA architecture (ARCHITECTURE.md / chat_pipeline.py).
   * Contents of each node (providers, tools, agents ...) are filled in from
   * live data by buildNodes(); nothing runtime-shaped is hardcoded here.
   */
  const NODE_DEFS = [
    { id: "interfaces", title: "User & Interfaces", group: "entry", x: 20, y: 20, w: 300, h: 190, tone: "blue" },
    { id: "gateway", title: "ASTRA Gateway", group: "core", x: 400, y: 20, w: 300, h: 190, tone: "purple" },
    { id: "router", title: "AstraRouter", group: "core", x: 780, y: 20, w: 280, h: 190, tone: "green" },
    { id: "providers", title: "AI Providers", group: "providers", x: 1140, y: 20, w: 320, h: 250, tone: "amber" },
    { id: "agents", title: "Agent Orchestration", group: "agents", x: 20, y: 300, w: 560, h: 200, tone: "green" },
    { id: "tools", title: "Tool System (ToolRegistry)", group: "tools", x: 640, y: 300, w: 470, h: 200, tone: "cyan" },
    { id: "external", title: "External Services", group: "tools", x: 1170, y: 300, w: 290, h: 200, tone: "violet" },
    { id: "memory", title: "Memory System", group: "memory", x: 20, y: 580, w: 240, h: 220, tone: "red" },
    { id: "workflows", title: "Workflow & Task Engine", group: "workflows", x: 280, y: 580, w: 240, h: 220, tone: "blue" },
    { id: "web3", title: "Web3 Manager", group: "web3", x: 540, y: 580, w: 240, h: 220, tone: "amber" },
    { id: "workspace", title: "Workspace", group: "workspace", x: 800, y: 580, w: 230, h: 220, tone: "cyan" },
    { id: "security", title: "Security & Policy", group: "security", x: 1050, y: 580, w: 200, h: 220, tone: "purple" },
    { id: "monitoring", title: "Monitoring & Events", group: "monitoring", x: 1270, y: 580, w: 190, h: 220, tone: "magenta" },
  ];
  // [from, to, label] — the real request path per chat_pipeline.py.
  const EDGE_DEFS = [
    ["interfaces", "gateway", "Request"],
    ["gateway", "router", "Route request"],
    ["router", "providers", "Select provider"],
    ["gateway", "agents", "Agent tool loop"],
    ["agents", "tools", "Tool call"],
    ["tools", "external", "External calls"],
    ["agents", "memory", "Memory"],
    ["tools", "workflows", "Workflows"],
    ["tools", "web3", "Web3 tools"],
    ["tools", "workspace", "Terminal / files / browser"],
  ];
  const TOOL_LOOP = ["Model decides", "Tool call", "ToolRegistry", "Tool execution",
    "Result", "Model observes", "Continue / complete"];

  /** Loop node highlighted from the latest real tool/agent event. */
  function activeLoopStep(events) {
    for (let i = arr(events).length - 1; i >= 0; i--) {
      const k = String(events[i].kind || "");
      if (k === "agent.tool_loop.started" || k === "agent.tool_loop.step") return 0;
      if (k === "agent.tool_call") return 1;
      if (k === "tool.started") return 3;
      if (k === "tool.completed" || k === "tool.failed" || k === "agent.tool_result") return 4;
      if (k === "agent.tool_loop.finished" || k === "agent.tool_loop.failed") return 6;
    }
    return -1;
  }

  /** Nodes with live `lines` (label/value/status) drawn from real data. */
  function buildNodes(d) {
    const cards = d.providerCards || [];
    const tg = d.tools || { groups: [], total: 0 };
    const gw = isObj(d.gateway) ? d.gateway : null;
    const last = isObj(d.routerStats) && isObj(d.routerStats.last_route) ? d.routerStats.last_route : {};
    const lines = {
      interfaces: [
        { k: "Web UI", v: "Active", s: "online" },
        { k: "API / FastAPI", v: "Active", s: "online" },
        { k: "CLI / Terminal", v: isObj(d.runtime) ? (d.runtime.available ? "Active" : "Runtime unavailable") : "Unavailable", s: isObj(d.runtime) && d.runtime.available ? "online" : "unknown" },
        { k: "Mobile", v: "Planned", s: "planned" },
      ],
      gateway: [
        { k: "Role", v: "Orchestration + verification — NOT a provider", s: "info" },
        { k: "State", v: gw ? gw.state : "Unavailable", s: gw ? (gw.state === "healthy" ? "online" : "degraded") : "unknown" },
        { k: "Connections", v: gw && isObj(gw.connections) ? Object.keys(gw.connections).length : "Unavailable", s: "info" },
        { k: "Flow", v: "Understand → assign → execute → verify → fix/redo", s: "info" },
      ],
      router: [
        { k: "Role", v: "Routing brain — NOT a provider", s: "info" },
        { k: "Last provider", v: last.provider || "None yet", s: "info" },
        { k: "Last model", v: last.model || "None yet", s: "info" },
        { k: "Preference", v: isObj(d.providersRaw) && isObj(d.providersRaw.router) ? d.providersRaw.router.preference || "Unavailable" : "Unavailable", s: "info" },
      ],
      providers: cards.length ? cards.map((p) => ({ k: p.name, v: p.modelCount + " models", s: p.status }))
        : [{ k: "No providers configured", v: "", s: "unknown" }],
      agents: arr(d.agents).length ? d.agents.map((a) => ({ k: a.name, v: a.description || "", s: "info" }))
        : [{ k: "Agent registry", v: "Unavailable (/api/system-map did not return an agent registry)", s: "unknown" }],
      tools: tg.groups.length ? tg.groups.map((g) => ({ k: g.name, v: g.tools.length + " tools", s: "online" }))
        : [{ k: "ToolRegistry", v: "Unavailable", s: "unknown" }],
      external: [{ k: "Provider HTTP APIs", v: "Via providers", s: "info" }, { k: "Blockchain RPC", v: "Via Web3 tools", s: "info" }],
      memory: d.experiences ? [
        { k: "Memories (recent)", v: d.memoryCount == null ? "Unavailable" : d.memoryCount, s: "info" },
        { k: "Experience records", v: d.experiences.total, s: "info" },
        { k: "Success rate", v: d.experiences.success_rate, s: "info" },
      ] : [{ k: "Memory", v: "Unavailable", s: "unknown" }],
      workflows: [
        { k: "Workflows", v: d.workflows == null ? "Unavailable" : d.workflows.length, s: "info" },
        { k: "Schedules", v: d.schedules == null ? "Unavailable" : d.schedules.length, s: "info" },
        { k: "Tasks", v: d.tasks == null ? "Unavailable" : d.tasks.length, s: "info" },
      ],
      web3: isObj(d.web3) ? [
        { k: "Confirmation mode", v: d.web3.mode, s: "info" },
        { k: "Emergency stop", v: d.web3.stopped ? "Stopped" : "Not stopped", s: d.web3.stopped ? "degraded" : "online" },
        { k: "Chains allowed", v: d.web3.chains == null ? "Unavailable" : d.web3.chains, s: "info" },
      ] : [{ k: "Web3", v: "Unavailable", s: "unknown" }],
      workspace: [
        { k: "Shared terminal", v: isObj(d.runtime) ? (d.runtime.available ? "Active (" + d.runtime.state + ")" : "Unavailable") : "Unavailable", s: isObj(d.runtime) && d.runtime.available ? "online" : "unknown" },
        { k: "Files", v: "Runtime file API", s: "info" },
        { k: "Browser", v: "Playwright (optional)", s: "info" },
      ],
      security: d.security ? d.security : [{ k: "Status", v: "Unavailable (/api/system-map did not return security status)", s: "unknown" }],
      monitoring: [
        { k: "EventBus (SSE)", v: d.sseState || "Unavailable", s: d.sseState === "live" ? "online" : "unknown" },
        { k: "Events loaded", v: d.eventCount == null ? "Unavailable" : d.eventCount, s: "info" },
      ],
    };
    return NODE_DEFS.map((n) => ({ ...n, lines: lines[n.id] || [] }));
  }

  /** Node ids directly connected to `id` (for connection highlighting). */
  function neighbours(id) {
    const out = new Set([id]);
    EDGE_DEFS.forEach(([a, b]) => { if (a === id) out.add(b); if (b === id) out.add(a); });
    return out;
  }

  /** Search across node titles + line text; returns matching node ids. */
  function searchNodes(nodes, q) {
    const s = String(q || "").trim().toLowerCase();
    if (!s) return null;
    return new Set(nodes.filter((n) =>
      n.title.toLowerCase().includes(s) ||
      n.lines.some((l) => (String(l.k) + " " + String(l.v)).toLowerCase().includes(s))).map((n) => n.id));
  }

  /* ------------------------------ KPI cards ------------------------------- */
  function fmt(v) { return v == null ? "Unavailable" : String(v); }
  function kpis(d, ops) {
    const cards = d.providerCards || [];
    const sum = providerSummary(cards);
    const running = ops.filter((o) => o.status === "running");
    const activeAgents = running.filter((o) => o.type === "Agent");
    const activeWf = running.filter((o) => o.type === "Workflow");
    const health = subsystemHealth(d);
    const bad = health.filter((h) => h.status !== "healthy").length;
    return [
      { id: "system", label: "System Status", value: d.health ? (d.health.ok ? "Online" : "Degraded") : "Unavailable",
        sub: health.length - bad + "/" + health.length + " subsystems healthy", tone: d.health && d.health.ok ? "good" : "warn" },
      { id: "ops", label: "Active Operations", value: fmt(d.eventsOk ? running.length : null), sub: "derived from live events", tone: "blue" },
      { id: "agents", label: "Active Agents", value: fmt(d.eventsOk ? activeAgents.length : null), sub: arr(d.agents).length ? arr(d.agents).length + " registered" : "registry unavailable", tone: "green" },
      { id: "workflows", label: "Active Workflows", value: fmt(d.eventsOk ? activeWf.length : null), sub: d.workflows ? d.workflows.length + " defined" : "Unavailable", tone: "purple" },
      { id: "providers", label: "Provider Health", value: cards.length ? sum.online + "/" + cards.length : "None configured", sub: cards.length ? "online" : "add a provider key", tone: cards.length && sum.online === cards.length ? "good" : "warn" },
      { id: "tools", label: "Tool Availability", value: d.tools ? d.tools.total : "Unavailable", sub: d.tools ? d.tools.groups.length + " groups" : "", tone: "cyan" },
      { id: "memory", label: "Memory Status", value: d.experiences ? d.experiences.total : "Unavailable", sub: (d.experiences ? "experience records" : "") + (d.memoryCount == null ? "" : " · " + d.memoryCount + " recent memories"), tone: "magenta" },
      { id: "sched", label: "Scheduled Tasks", value: d.schedules == null ? "Unavailable" : d.schedules.length, sub: "from scheduler", tone: "amber" },
    ];
  }

  const api = {
    providerStatus, providerCards, providerSummary, toolGroups, toolGroupOf,
    sourceOfKind, isTerminalEvent, isErrorEvent, eventRow, operationsFromEvents, filterEvents,
    subsystemHealth, NODE_DEFS, EDGE_DEFS, TOOL_LOOP, activeLoopStep, buildNodes,
    neighbours, searchNodes, kpis,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.SystemMapModel = api;
})(typeof window !== "undefined" ? window : globalThis);
