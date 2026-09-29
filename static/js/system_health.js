/* ASTRA AI Agent OS — System Health (the OS health dashboard).
 *
 * Owns the PRESENTATION of the `command-center` tabview so the /command-center
 * route and every existing nav entry keep working; the visible page identity
 * is "ASTRA System Health". astra_os.js keeps owning navigation, the ONE
 * aggregate refresh and the ONE shared SSE feed — this module never opens its
 * own EventSource or its own timer.
 *
 * DATA: everything comes from the existing endpoints. Nothing is invented:
 * a metric the backend does not report renders as an em dash / "Not
 * reported", and no value is estimated. Secrets are never read — provider
 * credentials reach the browser only as the backend's own secret-free
 * `keys[]` metadata (a non-secret key_id plus a human label).
 */
"use strict";
(function () {
  const M = window.SystemMapModel;
  const OS = window.AstraOS || (window.AstraOS = { data: {}, events: [] });

  const SH = {
    mounted: false,
    data: null,          // last OS.data handed in by astra_os.js
    events: [],
    models: { payload: null, ok: false, error: null, loading: false },
    tab: "overview",
    q: "",
    fProvider: "",
    fStatus: "",
    fKey: "",
  };

  /* --------------------------------- helpers -------------------------------- */
  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const arr = (v) => (Array.isArray(v) ? v : []);
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const DASH = "\u2014";
  const byId = (id) => (document.getElementById ? document.getElementById(id) : null);
  const closest = (e, sel) => (e && e.target && e.target.closest ? e.target.closest(sel) : null);
  const E = (v) => (typeof esc === "function" ? esc(v) : String(v == null ? "" : v));

  const fmtPct = (v, digits) => (v == null ? DASH : (digits == null ? v : Number(v).toFixed(digits)) + "%");
  const fmtMs = (v) => (num(v) == null ? DASH : Math.round(v) + " ms");
  const fmtInt = (v) => (v == null ? DASH : String(v));

  function fmtUptime(seconds) {
    const s = num(seconds);
    if (s == null || s < 0) return DASH;
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m";
    return Math.round(s) + "s";
  }

  /** "2026-09-29 21:17:12" | ISO | epoch seconds -> "21:17:12" (raw if unknown). */
  function fmtClock(v) {
    if (v == null || v === "") return DASH;
    if (typeof v === "number") return new Date(v * 1000).toLocaleTimeString([], { hour12: false });
    const raw = String(v).trim();
    const t = Date.parse(raw.includes("T") ? raw : raw.replace(" ", "T"));
    if (isNaN(t)) return raw;
    return new Date(t).toLocaleTimeString([], { hour12: false });
  }

  /* ---- presentation-only maps (nothing here invents data) ---- */
  const SERVICE_ICON = [
    [/^api$|database|sqlite/, "ic-service"], [/gateway/, "ic-gateway"], [/router/, "ic-gateway"],
    [/tool/, "ic-tool"], [/memory|experience/, "ic-memory"], [/workflow|scheduler/, "ic-sched"],
    [/event|bus|stream/, "ic-events"], [/web3|chain|wallet/, "ic-web3"], [/terminal|runtime/, "ic-agent"],
    [/provider/, "ic-provider"], [/model/, "ic-model"],
  ];
  const serviceIcon = (name) => {
    const low = String(name || "").toLowerCase();
    for (const [re, cls] of SERVICE_ICON) if (re.test(low)) return cls;
    return "ic-service";
  };

  /** Raw capability token -> label. Unknown tokens still render with the
   *  neutral badge: the catalogue is the backend's, not ours. */
  const CAP_LABEL = {
    chat: "Text", text: "Text", completion: "Text", code: "Code",
    vision: "Vision", tools: "Tools", function_calling: "Tools",
    json: "JSON", structured_output: "JSON", reasoning: "Reasoning",
    streaming: "Streaming", image: "Image", image_in: "Image In", image_out: "Image Out",
    image_edit: "Edit", edit: "Edit", inpaint: "Inpaint", audio: "Audio", embed: "Embeddings",
  };
  const CAP_CLASS = {
    Text: "sh-b-text", Code: "sh-b-tools", Vision: "sh-b-vision", Tools: "sh-b-tools",
    JSON: "sh-b-json", Reasoning: "sh-b-reasoning", Streaming: "sh-b-streaming",
    Image: "sh-b-image", "Image In": "sh-b-image", "Image Out": "sh-b-image",
    Edit: "sh-b-image", Inpaint: "sh-b-image", Audio: "sh-b-streaming", Embeddings: "sh-b-json",
  };
  function capsOf(model) {
    const raw = arr(model && model.capabilities).map(String);
    const out = [];
    const push = (tok) => {
      const l = CAP_LABEL[tok] || (String(tok).charAt(0).toUpperCase() + String(tok).slice(1));
      if (out.indexOf(l) < 0) out.push(l);
    };
    raw.forEach(push);
    if (model && model.supports_vision) push("vision");
    if (model && model.supports_tools) push("tools");
    if (model && model.supports_json) push("json");
    if (model && model.supports_streaming) push("streaming");
    return out;
  }

  /* ------------------------------ normalisation ----------------------------- */
  /** /api/health + gateway + router + tools + runtime -> Core Services rows. */
  function services(data) {
    if (!M || typeof M.subsystemHealth !== "function") return [];
    return arr(M.subsystemHealth(data || {})).map((s) => ({
      name: s.name,
      status: s.status === "healthy" ? "online" : s.status,
      detail: s.detail == null ? "" : String(s.detail),
      uptime: null,      // /api/health reports no per-subsystem uptime
      response: null,    // ... nor per-subsystem response time
    }));
  }

  /** /api/providers -> provider rows (status / success / latency / models / keys). */
  function providers(data) {
    return arr(data && data.providerCards).map((p) => ({
      name: p.name,
      status: p.status || "unknown",
      state: p.state == null ? null : String(p.state),
      modelCount: p.modelCount == null ? 0 : p.modelCount,
      successRate: num(p.successRate),
      latencyMs: num(p.latencyMs),
      keyCount: num(p.keyCount),
      calls: num(p.calls),
      errors: num(p.errors),
    }));
  }

  function rawProviders(data) {
    const d = data && data.providersRaw;
    return isObj(d) && isObj(d.providers) ? d.providers : {};
  }

  /** Secret-free key counters from the raw /api/providers payload. */
  function keyStats(data) {
    const raw = rawProviders(data);
    let total = 0, healthy = 0, seen = 0;
    Object.keys(raw).forEach((n) => {
      const keys = arr(raw[n] && raw[n].keys);
      if (!keys.length) return;
      seen += 1;
      keys.forEach((k) => { total += 1; if (k && k.healthy) healthy += 1; });
    });
    return { total: total, healthy: healthy, providersWithKeys: seen };
  }

  /** First key chip available for a provider (the backend's own safe label). */
  function firstKeyTag(data, provider) {
    const keys = arr(rawProviders(data)[provider] && rawProviders(data)[provider].keys);
    if (!keys.length) return null;
    const k = keys[0] || {};
    return { tag: String(k.label || k.key_id || ""), id: k.key_id ? String(k.key_id) : "", healthy: !!k.healthy };
  }

  /**
   * /api/models joined with /api/providers (per-model key health).
   * Every column is either reported by the backend or an em dash.
   */
  function models(data) {
    const payload = SH.models.payload;
    const list = arr(isObj(payload) ? payload.models : null);
    const cards = {};
    providers(data).forEach((p) => { cards[p.name] = p; });
    const raw = rawProviders(data);
    return list.map((m) => {
      const provider = String((m && m.provider) || "?");
      const modelId = String((m && (m.model || m.model_id)) || "?");
      const card = cards[provider] || null;
      const pRaw = isObj(raw[provider]) ? raw[provider] : {};
      const km = isObj(pRaw.key_results) ? pRaw.key_results : {};
      const perModel = isObj(km[modelId]) ? km[modelId] : {};
      const keyIds = Object.keys(perModel);
      const first = keyIds.length ? perModel[keyIds[0]] : null;
      const ktag = first
        ? { tag: String(first.key_label || keyIds[0]), id: keyIds[0], healthy: !!first.ok }
        : firstKeyTag(data, provider);
      let status = "not_reported";
      if (m && m.disabled) status = "unavailable";
      else if (first) status = first.ok ? "healthy" : "failed";
      else if (card) status = card.status;
      return {
        provider: provider,
        model: modelId,
        keyTag: ktag ? ktag.tag : null,
        keyId: ktag ? ktag.id : null,
        keyOk: ktag ? ktag.healthy : null,
        status: status,
        latencyMs: first ? num(first.latency_ms) : null,
        // /api/providers reports ONE success rate per provider — the row does
        // not pretend it is per-model (the cell carries a title saying so).
        successRate: card ? card.successRate : null,
        lastCheck: first ? first.tested_at : null,
        capabilities: capsOf(m),
      };
    });
  }

  /** Host telemetry the backend does not expose -> honest empty state. */
  function resourceRows() {
    return [
      { key: "cpu", label: "CPU" }, { key: "ram", label: "Memory" },
      { key: "disk", label: "Disk" }, { key: "net", label: "Network" },
    ];
  }

  /** The six headline cards — every value is one the backend reports. */
  function sixKpis(data) {
    const d = data || {};
    const health = d.health;
    const subs = M && M.subsystemHealth ? arr(M.subsystemHealth(d)) : [];
    const bad = subs.filter((s) => s.status !== "healthy").length;
    const cards = providers(d);
    const withLat = cards.filter((c) => c.latencyMs != null);
    const avgLat = withLat.length ? withLat.reduce((a, c) => a + c.latencyMs, 0) / withLat.length : null;
    const req = isObj(d.metrics) && isObj(d.metrics.requests) ? d.metrics.requests : null;
    const reqCount = req ? num(req.count) : null;
    const reqErr = req ? num(req.errors) : null;
    const errRate = reqCount && reqErr != null && reqCount > 0
      ? Math.round((reqErr / reqCount) * 10000) / 100 : null;
    const agentsReg = arr(d.agents).length;
    const ops = d.eventsOk && M && M.operationsFromEvents ? arr(M.operationsFromEvents(SH.events)) : null;
    const runningAgents = ops ? ops.filter((o) => o.type === "Agent" && o.status === "running").length : null;
    const tasksKnown = Array.isArray(d.tasks);
    const tasks = arr(d.tasks);
    const runningTasks = tasksKnown ? tasks.filter((t) => t && t.status === "running").length : null;
    const queuedTasks = tasksKnown
      ? tasks.filter((t) => t && (t.status === "pending" || t.status === "ready")).length : null;
    const uptimeS = isObj(d.metrics) ? num(d.metrics.uptime_s) : null;
    const shownAgents = agentsReg ? (runningAgents == null ? agentsReg : runningAgents) : null;

    return [
      { id: "system", label: "System Status", icon: bad ? "ic-error" : "ic-ok",
        tone: health ? (health.ok ? "sh-ki-green" : "sh-ki-amber") : "sh-ki-cyan",
        value: health ? (health.ok ? "Healthy" : "Degraded") : "Not reported",
        valueClass: health ? (health.ok ? "sh-good" : "sh-warn") : "sh-mut",
        sub: subs.length ? (subs.length - bad) + "/" + subs.length + " subsystems healthy"
          : "subsystem health unavailable" },
      { id: "uptime", label: "Server Uptime", icon: "ic-uptime", tone: "sh-ki-cyan",
        value: uptimeS == null ? DASH : fmtUptime(uptimeS), valueClass: "",
        sub: uptimeS == null ? "not reported" : "since last start" },
      { id: "latency", label: "Avg. Response", icon: "ic-latency", tone: "sh-ki-amber",
        value: avgLat == null ? DASH : Math.round(avgLat) + " ms", valueClass: "",
        sub: withLat.length ? "across " + withLat.length + " provider(s)" : "no provider latency reported" },
      { id: "agents", label: "Active Agents", icon: "ic-agent", tone: "sh-ki-blue",
        value: agentsReg ? (runningAgents == null ? String(agentsReg) : runningAgents + " / " + agentsReg) : DASH,
        valueClass: "", sub: agentsReg ? agentsReg + " registered" : "agent registry unavailable",
        bar: agentsReg ? Math.round((shownAgents / agentsReg) * 100) : null },
      { id: "tasks", label: "Running Tasks", icon: "ic-tasks", tone: "sh-ki-violet",
        value: runningTasks == null ? DASH : String(runningTasks), valueClass: "",
        sub: queuedTasks == null ? "not reported" : queuedTasks + " queued" },
      { id: "errors", label: "Error Rate", icon: "ic-error", tone: "sh-ki-red",
        value: errRate == null ? DASH : errRate + "%",
        valueClass: errRate == null ? "" : (errRate > 0 ? "sh-down" : "sh-up"),
        sub: reqCount == null ? "not reported" : (reqErr || 0) + " of " + reqCount + " requests" },
    ];
  }

  /** The six health-summary cards. */
  function sixSummary(data) {
    const d = data || {};
    const cards = providers(d);
    const online = cards.filter((p) => p.status === "online").length;
    const degraded = cards.filter((p) => p.status === "degraded" || p.status === "rate_limited").length;
    const failed = cards.filter((p) => p.status === "offline").length;
    const ks = keyStats(d);
    const mrows = models(d);
    const mTotal = mrows.length;
    const mOk = mrows.filter((m) => m.status === "healthy").length;
    const rated = cards.filter((c) => c.successRate != null);
    const success = rated.length ? rated.reduce((a, c) => a + c.successRate, 0) / rated.length : null;
    return [
      { id: "providers", label: "Providers", icon: "ic-provider", tone: "sh-ki-blue",
        value: cards.length ? online + " / " + cards.length : DASH,
        sub: !cards.length ? "none configured"
          : (online === cards.length ? "Healthy" : degraded + " degraded"),
        subClass: !cards.length ? "sh-mut" : (online === cards.length ? "sh-good" : "sh-warn") },
      { id: "models", label: "Models", icon: "ic-model", tone: "sh-ki-violet",
        value: mTotal ? mOk + " / " + mTotal : DASH,
        sub: mTotal ? (Math.round((mOk / mTotal) * 1000) / 10) + "% Healthy" : "model registry empty",
        subClass: mTotal ? "sh-good" : "sh-mut" },
      { id: "keys", label: "API Keys", icon: "ic-service", tone: "sh-ki-green",
        value: ks.total ? ks.healthy + " / " + ks.total : DASH,
        sub: ks.total ? (Math.round((ks.healthy / ks.total) * 1000) / 10) + "% Healthy"
          : "no credentials reported",
        subClass: ks.total ? "sh-good" : "sh-mut" },
      { id: "degraded", label: "Degraded", icon: "ic-error", tone: "sh-ki-amber",
        value: String(degraded), valueClass: degraded ? "sh-warn" : "",
        sub: degraded ? "Need attention" : "None" },
      { id: "failed", label: "Failed", icon: "ic-fail", tone: "sh-ki-red",
        value: String(failed), valueClass: failed ? "sh-bad" : "",
        sub: failed ? "Need attention" : "None" },
      { id: "success", label: "Provider Success Rate", icon: "ic-latency", tone: "sh-ki-green",
        value: success == null ? DASH : (Math.round(success * 10) / 10) + "%",
        valueClass: success == null ? "sh-mut" : "sh-good",
        sub: rated.length ? "across " + rated.length + " provider(s)" : "not reported" },
    ];
  }

  /** Filter + search over the loaded model rows (never over a second list). */
  function visibleModels(rows, f) {
    const q = String((f && f.q) || "").trim().toLowerCase();
    return arr(rows).filter((r) => {
      if (f && f.fProvider && r.provider !== f.fProvider) return false;
      if (f && f.fStatus && r.status !== f.fStatus) return false;
      if (f && f.fKey && (r.keyTag || "") !== f.fKey) return false;
      if (!q) return true;
      return [r.provider, r.model, r.keyTag, r.status].concat(r.capabilities)
        .some((v) => String(v == null ? "" : v).toLowerCase().includes(q));
    });
  }

  const STATUS_LABEL = {
    healthy: "Healthy", online: "Online", degraded: "Degraded", rate_limited: "Rate Limited",
    offline: "Failed", failed: "Failed", unavailable: "Unavailable", unknown: "Unknown",
    not_reported: "Not reported", disabled: "Disabled",
  };
  const statusLabel = (s) => STATUS_LABEL[s] || (s ? String(s).replace(/_/g, " ") : "Not reported");

  /* --------------------------------- markup --------------------------------- */
  const ico = (cls, extra) => `<span class="sh-ic ${cls}${extra ? " " + extra : ""}"></span>`;
  const GO = '<span class="sh-ico ic-go"></span>';
  const DOTS = '<span class="sh-ico ic-dots"></span>';
  const RELOAD = '<span class="sh-ico ic-reload"></span>';
  const statusCell = (s) => `<span class="sh-st ${E(s)}"><span class="sh-dot"></span>${E(statusLabel(s))}</span>`;

  const PAGE = `
    <div class="sh" id="sh-root">
      <div class="sh-head">
        <div class="sh-mark" aria-hidden="true"></div>
        <div class="sh-ht">
          <h1 class="sh-title">ASTRA System Health</h1>
          <p class="sh-sub">Real-time status of all services, providers, models, and infrastructure</p>
        </div>
        <div class="sh-tabs" role="tablist" id="sh-tabs" aria-label="System Health sections"></div>
        <div class="sh-ctrl">
          <button class="sh-auto" id="sh-auto" type="button" aria-pressed="false">
            <span class="sh-sw" aria-hidden="true"></span>Auto refresh
          </button>
          <span class="sh-select">
            <label for="sh-interval" style="color:#8b96b4">Every</label>
            <select id="sh-interval" aria-label="Refresh interval"
                    style="background:none;border:0;color:#d5def2;font:inherit;font-size:12px;padding:0">
              <option value="15000">15s</option>
              <option value="30000" selected>30s</option>
              <option value="60000">60s</option>
              <option value="300000">5m</option>
            </select>
          </span>
          <button class="sh-btn" id="sh-refresh" type="button">${RELOAD}Refresh</button>
        </div>
      </div>

      <div class="sh-kpis" id="sh-kpis"></div>
      <div class="sh-summary" id="sh-summary"></div>

      <div class="sh-grid3">
        <section class="sh-panel" id="sh-services">
          <div class="sh-ph">
            ${ico("ic-service")}
            <div class="sh-pht"><h3>Core Services</h3><p id="sh-services-sub">Subsystem status</p></div>
            <div class="sh-phr">
              <button class="sh-link" type="button" data-sh-nav="system-map">View All ${GO}</button>
            </div>
          </div>
          <div class="sh-scroll">
            <table class="sh-tbl"><thead><tr>
              <th>Service</th><th>Status</th><th>Uptime</th><th>Response</th><th>Details</th><th></th>
            </tr></thead><tbody id="sh-services-body"></tbody></table>
          </div>
        </section>

        <section class="sh-panel" id="sh-providers">
          <div class="sh-ph">
            ${ico("ic-provider")}
            <div class="sh-pht"><h3>AI Provider Health</h3><p>Status of configured AI providers</p></div>
            <div class="sh-phr">
              <button class="sh-link" type="button" data-sh-nav="providers">View All ${GO}</button>
            </div>
          </div>
          <div class="sh-scroll">
            <table class="sh-tbl sh-tbl-prov"><thead><tr>
              <th>Provider</th><th>Status</th><th>Models</th><th>Success</th><th>Avg Latency</th>
            </tr></thead><tbody id="sh-providers-body"></tbody></table>
          </div>
        </section>

        <section class="sh-panel" id="sh-resources">
          <div class="sh-ph">
            ${ico("ic-cpu")}
            <div class="sh-pht"><h3>System Resources</h3><p>Host metrics not exposed by the API</p></div>
          </div>
          <div class="sh-res" id="sh-resources-body"></div>
        </section>
      </div>

      <section class="sh-panel sh-models" id="sh-models">
        <div class="sh-ph">
          ${ico("ic-model")}
          <div class="sh-pht"><h3>AI Model Health</h3><p>Status of all AI models with key information</p></div>
          <div class="sh-phr">
            <button class="sh-btn" id="sh-mrefresh" type="button">${RELOAD}Refresh</button>
          </div>
        </div>
        <div class="sh-filters">
          <select class="sh-select" id="sh-fprov" aria-label="Filter by provider"></select>
          <select class="sh-select" id="sh-fstat" aria-label="Filter by status"></select>
          <select class="sh-select" id="sh-fkey" aria-label="Filter by key"></select>
          <span class="grow"></span>
          <span class="sh-qwrap">
            <span class="sh-mag" aria-hidden="true"></span>
            <input class="sh-q" id="sh-q" type="search" autocomplete="off"
                   placeholder="Search models..." aria-label="Search models">
          </span>
        </div>
        <div class="sh-scroll">
          <table class="sh-tbl"><thead><tr>
            <th>Provider</th><th>Model</th><th>Key Tag</th><th>Status</th>
            <th class="r">Latency</th><th class="r">Success</th><th>Last Check</th>
            <th>Capabilities</th><th class="r">Actions</th>
          </tr></thead><tbody id="sh-models-body"></tbody></table>
        </div>
      </section>
    </div>`;

  /* ---------------------------------- paint --------------------------------- */
  const skeleton = (n) => Array.from({ length: n },
    () => '<tr class="sh-skel"><td colspan="9"><i></i></td></tr>').join("");

  function paintKpis() {
    const el = byId("sh-kpis"); if (!el) return;
    el.innerHTML = sixKpis(SH.data).map((k) => `
      <div class="sh-panel sh-kpi">
        ${ico(k.icon, k.tone)}
        <div>
          <div class="sh-kl">${E(k.label)}</div>
          <div class="sh-kv ${E(k.valueClass || "")}">${E(k.value)}</div>
          ${k.bar == null ? "" : `<div class="sh-kbar"><i style="width:${Math.max(0, Math.min(100, k.bar))}%"></i></div>`}
          <div class="sh-ks">${E(k.sub)}</div>
        </div>
      </div>`).join("");
  }

  function paintSummary() {
    const el = byId("sh-summary"); if (!el) return;
    el.innerHTML = sixSummary(SH.data).map((c) => `
      <div class="sh-panel sh-sum">
        ${ico(c.icon, c.tone)}
        <div>
          <div class="sh-sl">${E(c.label)}</div>
          <div class="sh-sv ${E(c.valueClass || "")}">${E(c.value)}</div>
          <div class="sh-ss ${E(c.subClass || "")}">${E(c.sub)}</div>
        </div>
        ${c.id === "success" ? '<span class="sh-barcap">No history</span>' : ""}
      </div>`).join("");
  }

  function paintServices() {
    const body = byId("sh-services-body"); if (!body) return;
    const rows = services(SH.data);
    const sub = byId("sh-services-sub");
    const bad = rows.filter((r) => r.status !== "online").length;
    if (sub) {
      sub.textContent = !rows.length ? "No subsystem health reported"
        : bad === 0 ? "All reported subsystems are healthy"
          : bad + " of " + rows.length + " subsystems need attention";
    }
    body.innerHTML = rows.length ? rows.map((r) => `
      <tr>
        <td><span class="sh-name">${ico(serviceIcon(r.name), "sm")}<span>${E(r.name)}</span></span></td>
        <td>${statusCell(r.status)}</td>
        <td class="sh-num">${E(r.uptime == null ? DASH : r.uptime)}</td>
        <td class="sh-num">${E(r.response == null ? DASH : r.response)}</td>
        <td class="sh-mut">${E(r.detail || DASH)}</td>
        <td><button class="sh-go" type="button" data-sh-node="${E(r.name)}"
              aria-label="Inspect ${E(r.name)}">${GO}</button></td>
      </tr>`).join("")
      : '<tr><td colspan="6"><div class="sh-state">No subsystem health reported.</div></td></tr>';
  }

  function paintProviders() {
    const body = byId("sh-providers-body"); if (!body) return;
    const rows = providers(SH.data);
    body.innerHTML = rows.length ? rows.map((p) => `
      <tr>
        <td><span class="sh-name">${ico("ic-provider", "sm")}<span>${E(p.name)}</span></span></td>
        <td>${statusCell(p.status)}</td>
        <td class="sh-num">${E(fmtInt(p.modelCount))}</td>
        <td class="sh-num">${E(fmtPct(p.successRate, 1))}</td>
        <td class="sh-num">${E(fmtMs(p.latencyMs))}</td>
      </tr>`).join("")
      : '<tr><td colspan="6"><div class="sh-state">No providers configured.</div></td></tr>';
  }

  const RES_ICON = { cpu: "ic-cpu", ram: "ic-ram", disk: "ic-disk", net: "ic-net" };

  function paintResources() {
    const el = byId("sh-resources-body"); if (!el) return;
    el.innerHTML = resourceRows().map((r) => `
      <div class="sh-resrow">
        ${ico(RES_ICON[r.key], "sm")}
        <div><div class="sh-rl">${E(r.label)}</div><div class="sh-rv sh-mut">${DASH}</div></div>
        <svg class="sh-spark" viewBox="0 0 76 26" preserveAspectRatio="none" role="img"
             aria-label="${E(r.label)}: not reported"><path class="ln" d="M0,13 L76,13"></path></svg>
      </div>`).join("") + '<div class="sh-state" style="padding:10px">Not reported by the API.</div>';
  }

  function optionList(values, allLabel, selected) {
    return `<option value=""${selected ? "" : " selected"}>${E(allLabel)}</option>` +
      values.map((v) => `<option value="${E(v)}"${v === selected ? " selected" : ""}>${E(v)}</option>`).join("");
  }

  function paintFilters(rows) {
    const uniq = (key) => [...new Set(rows.map(key).filter((v) => v != null && v !== ""))].sort();
    const fp = byId("sh-fprov"), fs = byId("sh-fstat"), fk = byId("sh-fkey");
    if (fp) fp.innerHTML = optionList(uniq((r) => r.provider), "All Providers", SH.fProvider);
    if (fs) fs.innerHTML = optionList(uniq((r) => r.status), "All Status", SH.fStatus);
    if (fk) fk.innerHTML = optionList(uniq((r) => r.keyTag), "All Keys", SH.fKey);
    if (fs && fs.options) Array.from(fs.options).forEach((o) => { if (o.value) o.textContent = statusLabel(o.value); });
  }

  function paintModels() {
    const body = byId("sh-models-body"); if (!body) return;
    if (SH.models.loading && !SH.models.payload && !SH.models.error) { body.innerHTML = skeleton(6); return; }
    const all = models(SH.data);
    paintFilters(all);
    const rows = visibleModels(all, SH);
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="9"><div class="sh-state">${
        all.length ? "No models match this filter."
          : (SH.models.error ? "Model registry unavailable." : "No models reported by the model registry.")
      }</div></td></tr>`;
      return;
    }
    body.innerHTML = rows.map((r) => `
      <tr>
        <td><span class="sh-name">${ico("ic-provider", "sm")}<span>${E(r.provider)}</span></span></td>
        <td class="sh-mono">${E(r.model)}</td>
        <td>${r.keyTag
          ? `<span class="sh-key${r.keyOk === false ? " warn" : ""}"${r.keyId ? ` title="${E(r.keyId)}"` : ""}>${E(r.keyTag)}</span>`
          : `<span class="sh-mut">${DASH}</span>`}</td>
        <td>${statusCell(r.status)}</td>
        <td class="sh-num r">${E(fmtMs(r.latencyMs))}</td>
        <td class="sh-num r"${r.successRate == null ? "" : ' title="provider success rate"'}>${E(fmtPct(r.successRate, 1))}</td>
        <td class="sh-num sh-mut">${E(fmtClock(r.lastCheck))}</td>
        <td>${r.capabilities.length
          ? r.capabilities.map((c) => `<span class="sh-badge ${E(CAP_CLASS[c] || "")}">${E(c)}</span>`).join("")
          : `<span class="sh-mut">${DASH}</span>`}</td>
        <td class="r"><button class="sh-kebab" type="button"
              data-sh-row="${E(r.provider)}|${E(r.model)}" aria-label="Model actions">${DOTS}</button></td>
      </tr>`).join("");
  }

  const TABS = [["overview", "Overview"], ["services", "Services"], ["providers", "Providers"],
    ["models", "Models"], ["resources", "Resources"], ["logs", "Logs"]];

  function paintTabs() {
    const el = byId("sh-tabs"); if (!el) return;
    el.innerHTML = TABS.map(([id, label]) =>
      `<button class="sh-tab${SH.tab === id ? " on" : ""}" type="button" role="tab"`
      + ` aria-selected="${SH.tab === id}" data-sh-tab="${id}">${E(label)}</button>`).join("");
  }

  function paint() {
    paintTabs(); paintKpis(); paintSummary(); paintServices();
    paintProviders(); paintResources(); paintModels();
  }

  /** astra_os.js hands the refreshed aggregate in on every poll / SSE batch. */
  function render(data, events) {
    if (data) SH.data = data;
    if (events) SH.events = events;
    if (!SH.mounted) mount();
    paint();
  }

  /* ------------------------------- mount / bind ------------------------------ */
  function mount() {
    const host = byId("tab-command-center");
    if (!host) return;
    if (SH.mounted) return;
    host.innerHTML = PAGE;
    SH.mounted = true;
    bind();
  }

  function bind() {
    const root = byId("tab-command-center");
    if (!root || !root.addEventListener) return;
    root.addEventListener("click", (e) => {
      const tab = closest(e, "[data-sh-tab]");
      if (tab) { selectTab(tab.dataset.shTab); return; }
      const nav = closest(e, "[data-sh-nav]");
      if (nav) { if (typeof showTab === "function") showTab(nav.dataset.shNav); return; }
      const node = closest(e, "[data-sh-node]");
      if (node) { openService(node.dataset.shNode); return; }
      const row = closest(e, "[data-sh-row]");
      if (row) { openModel(row.dataset.shRow); return; }
      if (closest(e, "#sh-refresh") || closest(e, "#sh-mrefresh")) { refresh(true); return; }
      if (closest(e, "#sh-auto")) { toggleAuto(); return; }
      const x = closest(e, "#os-drawer .x");
      if (x) { const d = byId("os-drawer"); if (d) d.classList.remove("open"); }
    });
    const q = byId("sh-q");
    if (q && q.addEventListener) q.addEventListener("input", () => { SH.q = q.value; paintModels(); });
    const ia = byId("sh-interval");
    if (ia && ia.addEventListener) {
      ia.addEventListener("change", () => {
        const ms = parseInt(ia.value, 10);
        if (OS.hooks && typeof OS.hooks.setInterval === "function") OS.hooks.setInterval(ms);
      });
    }
    const map = [["sh-fprov", "fProvider"], ["sh-fstat", "fStatus"], ["sh-fkey", "fKey"]];
    map.forEach(([id, key]) => {
      const el = byId(id);
      if (!el || !el.addEventListener) return;
      el.addEventListener("change", () => { SH[key] = el.value; paintModels(); });
    });
  }

  /* ------------------------------ interactions ------------------------------ */
  const SECTION_OF = { services: "sh-services", providers: "sh-providers", resources: "sh-resources", models: "sh-models" };

  /** Tabs scroll to the real panel they name; "Logs" opens the existing
   *  Activity Center page rather than duplicating an event list here. */
  function selectTab(id) {
    if (id === "logs") { if (typeof showTab === "function") showTab("logs"); return; }
    SH.tab = id;
    paintTabs();
    if (id === "overview") {
      const host = byId("sh-root");
      if (host && host.scrollIntoView) host.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const target = byId(SECTION_OF[id]);
    if (target && target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "start" });
    if (target && target.classList) {
      target.classList.remove("sh-flash"); void target.offsetWidth; target.classList.add("sh-flash");
    }
  }

  function toggleAuto() {
    const on = !(OS.poll && OS.poll.enabled);
    if (OS.hooks && typeof OS.hooks.setAuto === "function") OS.hooks.setAuto(on);
    syncAuto();
  }

  /** Reflect the ONE shared poll's real state into the header controls. */
  function syncAuto() {
    const btn = byId("sh-auto");
    if (btn && btn.setAttribute) btn.setAttribute("aria-pressed", (OS.poll && OS.poll.enabled) ? "true" : "false");
    const sel = byId("sh-interval");
    if (sel && OS.poll && OS.poll.intervalMs != null) sel.value = String(OS.poll.intervalMs);
  }

  function flash(btn) {
    if (!btn) return;
    btn.disabled = true;
    const g = btn.querySelector ? btn.querySelector(".sh-ico") : null;
    if (g && g.classList) g.classList.add("spin");
    setTimeout(() => {
      btn.disabled = false;
      if (g && g.classList) g.classList.remove("spin");
    }, 700);
  }

  async function loadModels() {
    SH.models.loading = true;
    try {
      const r = typeof api === "function" ? await api("/api/models") : null;
      if (r && r.ok) { SH.models.payload = r.data || null; SH.models.ok = true; SH.models.error = null; }
      else { SH.models.error = (r && (r.error || r.message)) || "model registry unavailable"; }
    } catch (e) {
      SH.models.error = String((e && e.message) || e);
    } finally {
      SH.models.loading = false;
    }
  }

  /** Refresh button: re-read the registry, then the shared aggregate. */
  async function refresh(user) {
    if (user) flash(byId("sh-refresh") || byId("sh-mrefresh"));
    await loadModels();
    if (OS.hooks && typeof OS.hooks.refresh === "function") {
      try { SH.data = await OS.hooks.refresh(); } catch (e) { /* keep last good data */ }
    }
    paint();
  }

  function drawer(title, html) {
    const d = byId("os-drawer");
    if (!d) return;
    d.innerHTML = `<button class="x" aria-label="Close">\u00d7</button><h3>${E(title)}</h3>${html}`;
    if (d.classList) d.classList.add("open");
  }

  function openService(name) {
    const s = services(SH.data).find((r) => r.name === name);
    if (!s) return;
    const kv = (k, v) => `<div class="kv"><span>${E(k)}</span><span>${E(v == null || v === "" ? DASH : v)}</span></div>`;
    drawer(s.name, kv("Status", statusLabel(s.status)) + kv("Detail", s.detail) +
      kv("Uptime", "not reported") + kv("Response", "not reported") +
      '<div class="small muted" style="margin-top:8px">/api/health reports status per subsystem, not per-service uptime or latency.</div>');
  }

  function openModel(key) {
    const parts = String(key || "").split("|");
    const row = models(SH.data).find((r) => r.provider === parts[0] && r.model === parts[1]);
    if (!row) return;
    const kv = (k, v) => `<div class="kv"><span>${E(k)}</span><span>${E(v == null || v === "" ? DASH : v)}</span></div>`;
    drawer(row.provider + " \u00b7 " + row.model,
      kv("Status", statusLabel(row.status)) +
      kv("Key tag", row.keyTag) +
      kv("Latency", row.latencyMs == null ? null : Math.round(row.latencyMs) + " ms") +
      kv("Success rate", row.successRate == null ? null : row.successRate + "% (provider)") +
      kv("Last check", row.lastCheck == null ? null : fmtClock(row.lastCheck)) +
      kv("Capabilities", row.capabilities.length ? row.capabilities.join(", ") : null) +
      '<div class="small muted" style="margin-top:8px">API keys are never shown \u2014 only the backend\'s own non-secret key label.</div>');
  }

  /* ------------------------------ tab lifecycle ----------------------------- */
  /** Called by astra_os.js's loader after the aggregate refresh. */
  async function open() {
    mount();
    syncAuto();
    if (!SH.mounted) return;
    if (!SH.data) { const b = byId("sh-models-body"); if (b) b.innerHTML = skeleton(6); }
    paint();
    await loadModels();
    paint();
  }

  window.SystemHealth = {
    state: SH,
    mount: mount, open: open, render: render, paint: paint, refresh: refresh,
    loadModels: loadModels, selectTab: selectTab, toggleAuto: toggleAuto, syncAuto: syncAuto,
    norm: {
      services: services, providers: providers, keyStats: keyStats, models: models,
      sixKpis: sixKpis, sixSummary: sixSummary, visibleModels: visibleModels, capsOf: capsOf,
      statusLabel: statusLabel, fmtUptime: fmtUptime, fmtClock: fmtClock, serviceIcon: serviceIcon,
    },
  };
})();