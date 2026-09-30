/* Astra AI Agent — 🛡️ Security Center (dedicated page).
 *
 * REAL DATA ONLY. The one data source is GET /api/security/status, which the
 * backend derives from live objects (config, rate limiter, ToolRegistry,
 * provider router, Web3 policy, Agent Runtime, emergency latch). A value the
 * backend cannot report arrives as null and is rendered "Not reported by API".
 * There is no security score (Astra has no deterministic scoring model) and
 * no events table. Nothing here is random, hard-coded or simulated.
 *
 *   - Emergency Shutdown  -> POST /api/security/emergency-shutdown (confirmed)
 *                            and /emergency-release. The badge shows the
 *                            backend latch, never a client-side guess.
 *   - Total Data Backup   -> POST /api/security/backup/create   (zip download)
 *   - Import Astra Data   -> POST /api/security/backup/inspect  (validate +
 *                            preview) then /restore (explicit confirmation).
 *
 * API keys, tokens and wallet keys never reach this file: the status payload
 * is secret-free and backups exclude secrets by construction. Everything
 * rendered goes through esc().
 */
(function () {
  "use strict";

  const NA = "Not reported by API";
  const INTERVALS = [[15000, "Every 15s"], [30000, "Every 30s"], [60000, "Every 1m"], [300000, "Every 5m"]];
  const SC = {
    data: null, error: null, loading: false, mounted: false, active: false,
    auto: true, intervalMs: 30000, checkedAt: null, timer: 0, clock: 0,
    shutdown: { busy: false, result: null, error: null },
    backup: { busy: false, created: null, error: null },
    imp: { busy: false, file: null, preview: null, strategy: "keep_existing", skip: {}, result: null, error: null, drag: false },
    dialog: null,
    open: {},                // expanded control rows
  };

  const E = (s) => (typeof esc === "function" ? esc(s) : String(s == null ? "" : s));
  const byId = (id) => document.getElementById(id);
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const show = (v) => (v == null || v === "" ? NA : String(v));

  /* ------------------------------- icons ---------------------------------- */
  const P = {
    shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z", check: "M5 12l4 4 10-10",
    lock: "M6 11h12v9H6z M8 11V8a4 4 0 018 0v3", key: "M15 7a4 4 0 11-3 6.6L5 21H3v-3l7.5-7.5A4 4 0 0115 7z",
    cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9z M12 12l8-4.5 M12 12v9 M12 12L4 7.5",
    link: "M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1 M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1",
    gear: "M12 8a4 4 0 100 8 4 4 0 000-8z M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2",
    net: "M18 8a3 3 0 100-6 3 3 0 000 6z M6 15a3 3 0 100-6 3 3 0 000 6z M18 22a3 3 0 100-6 3 3 0 000 6z M8.6 13.5l6.8 4 M15.4 6.5l-6.8 4",
    power: "M12 3v9 M6.3 6.3a8 8 0 1011.4 0",
    db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
    down: "M12 3v12 M7 11l5 5 5-5 M5 21h14", up: "M12 21V9 M7 13l5-5 5 5 M5 3h14",
    refresh: "M20 11a8 8 0 10-2.3 6.3 M20 4v7h-7", alert: "M12 3l10 18H2z M12 10v5 M12 18v.5",
    chev: "M9 6l6 6-6 6", list: "M4 6h16 M4 12h16 M4 18h10", monitor: "M3 5h18v11H3z M8 21h8 M12 16v5",
    file: "M6 3h9l4 4v14H6z M14 3v5h5", x: "M6 6l12 12 M18 6L6 18", scan: "M4 8V5a1 1 0 011-1h3 M16 4h3a1 1 0 011 1v3 M20 16v3a1 1 0 01-1 1h-3 M8 20H5a1 1 0 01-1-1v-3 M4 12h16",
  };
  const ic = (n, cls) => `<svg class="sc-i${cls ? " " + cls : ""}" viewBox="0 0 24 24" aria-hidden="true"><path d="${P[n] || P.list}"/></svg>`;
  const CTRL_ICON = { auth: "lock", rate_limit: "list", cors: "net", headers: "shield", csp: "file", body_limit: "file", ssrf: "net", request_id: "list" };

  /* ------------------------------ formatting ------------------------------ */
  const LV = { ok: "ok", warn: "warn", danger: "danger", info: "info", na: "na" };
  const lv = (l) => LV[l] || "info";
  const chip = (level, text) => `<span class="sc-chip lv-${lv(level)}"><i></i>${E(text)}</span>`;
  const bytes = (n) => !isNum(n) ? NA : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
  function ago(ms) {
    if (!isNum(ms)) return NA;
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    return s < 5 ? "just now" : s < 60 ? s + "s ago" : Math.round(s / 60) + "m ago";
  }
  const panel = (icon, title, sub, body, extra) =>
    `<section class="sc-panel${extra ? " " + extra : ""}"><header class="sc-ph"><span class="sc-tile">${ic(icon)}</span><div><h3>${E(title)}</h3><p>${E(sub)}</p></div></header>${body}</section>`;
  const kv = (k, v, level) => `<div class="sc-row"><span class="sc-k">${E(k)}</span>${level ? chip(level, show(v)) : `<b class="sc-n">${E(show(v))}</b>`}</div>`;
  const unavailable = `<div class="sc-empty">${NA}</div>`;

  /* -------------------------------- header -------------------------------- */
  function renderHead() {
    const d = SC.data, p = d && d.posture;
    const level = !p ? "na" : p.state === "protected" ? "ok" : p.state === "shutdown" ? "danger" : p.state === "attention" ? "warn" : "danger";
    const opts = INTERVALS.map(([ms, l]) => `<option value="${ms}"${ms === SC.intervalMs ? " selected" : ""}>${l}</option>`).join("");
    return `<div class="sc-title"><span class="sc-tile lg">${ic("shield")}</span><div><h2>ASTRA Security Center</h2>` +
      `<p>Protecting APIs, credentials, tools, agents, Web3 operations, and runtime execution</p></div></div>` +
      `<div class="sc-ctl">` +
      `<div class="sc-status lv-${level}"><span class="sc-tile sm">${ic(level === "ok" ? "check" : "alert")}</span><div><b>${E(p ? p.label : "Unavailable")}</b><small>${E(p ? p.summary : (SC.error || NA))}</small></div></div>` +
      `<div class="sc-last"><small>Last checked</small><b id="sc-ago">${E(ago(SC.checkedAt))}</b></div>` +
      `<button class="sc-btn primary" data-act="scan"${SC.loading ? " disabled" : ""}>${ic("scan")}Scan Now</button>` +
      `<label class="sc-auto">Auto refresh <button class="sc-sw" data-act="auto" role="switch" aria-checked="${SC.auto}" aria-label="Auto refresh"><i></i></button></label>` +
      `<select class="sc-sel" id="sc-interval" aria-label="Refresh interval">${opts}</select>` +
      `<button class="sc-btn" data-act="refresh"${SC.loading ? " disabled" : ""}>${ic("refresh")}Refresh</button></div>`;
  }

  /* ------------------------------ summary row ----------------------------- */
  function renderSummary() {
    const d = SC.data;
    if (!d) return `<div class="sc-state">${SC.loading ? "Loading security status…" : E(SC.error || "Security status unavailable")}</div>`;
    const p = d.posture, c = p.counts, a = d.api, cr = d.credentials, t = d.tools, w = d.web3;
    const pl = p.state === "protected" ? "ok" : p.state === "attention" ? "warn" : "danger";
    const posture = `<article class="sc-card"><span class="sc-tile lg lv-${pl}">${ic("shield")}</span><div class="sc-cb"><small>Security Posture</small><b class="big lv-${pl}">${E(p.label)}</b><p>${E(p.summary)}</p>` +
      `<div class="sc-counts"><span class="lv-danger"><b>${c.critical}</b>Critical</span><span class="lv-warn"><b>${c.high}</b>High</span><span class="lv-warn2"><b>${c.medium}</b>Medium</span><span class="lv-ok"><b>${c.passed}</b>Passed</span></div>` +
      (isNum(p.score) ? `<div class="sc-score">${p.score}<small>Security Score</small></div>` : "") + `</div></article>`;
    const apiOk = [a.authentication, a.rate_limiting, a.security_headers].every((x) => x.level === "ok");
    const api_ = `<article class="sc-card"><span class="sc-tile lg">${ic("lock")}</span><div class="sc-cb"><small>API Security</small><b class="big lv-${apiOk ? "ok" : "warn"}">${apiOk ? "Protected" : "Attention"}</b>` +
      `<p>Auth: ${E(a.authentication.status)} · Rate limit: ${E(a.rate_limiting.status)} · Headers: ${E(a.security_headers.status)}</p>${chip(apiOk ? "ok" : "warn", apiOk ? "Healthy" : "Review controls")}</div></article>`;
    const haveCr = isNum(cr.keys);
    const creds = `<article class="sc-card"><span class="sc-tile lg">${ic("key")}</span><div class="sc-cb"><small>Credentials</small>` +
      (haveCr ? `<b class="big">${cr.healthy} / ${cr.keys}</b><p>${cr.keys ? `Healthy credentials (across ${cr.providers} provider${cr.providers === 1 ? "" : "s"})` : "No provider credentials configured"}</p>${cr.keys ? chip(cr.failed ? "warn" : "ok", cr.failed ? cr.failed + " failing" : "Healthy") : chip("na", "None configured")}`
        : `<b class="big na">${NA}</b>`) + `</div></article>`;
    const tools = `<article class="sc-card"><span class="sc-tile lg lv-purple">${ic("cube")}</span><div class="sc-cb"><small>Tool Security</small>` +
      (t.available ? `<b class="big">${t.total} tools</b><p>${t.high} high · ${t.medium} medium · ${t.low} low risk</p>${chip(t.granted ? "ok" : "na", t.granted ? t.granted.length + " permission levels granted" : "Permissions " + NA)}` : `<b class="big na">${NA}</b>`) + `</div></article>`;
    const web3 = `<article class="sc-card"><span class="sc-tile lg">${ic("link")}</span><div class="sc-cb"><small>Web3 Policy</small>` +
      (w.available ? `<b class="big lv-${w.mode === "CONFIRM" ? "ok" : "warn"}">${E(w.mode)}</b><p>Confirmation mode · ${w.limits_configured ? "limits configured" : "no limits set"}</p>${chip(w.stopped ? "danger" : "ok", w.stopped ? "Emergency stop engaged" : "Transactions gated")}` : `<b class="big na">${NA}</b>`) + `</div></article>`;
    return posture + api_ + creds + tools + web3;
  }

  /* ------------------------------- main grid ------------------------------ */
  function renderControls(d) {
    const rows = (d.controls || []).map((c) => {
      const open = SC.open[c.id];
      return `<button class="sc-row ctl" data-toggle="${E(c.id)}" aria-expanded="${!!open}"><span class="sc-rowi">${ic(CTRL_ICON[c.id])}</span><span class="sc-k">${E(c.name)}</span>${chip(c.level, c.status)}<span class="sc-ch${open ? " open" : ""}">${ic("chev")}</span></button>` +
        (open ? `<div class="sc-detail">${E(c.detail || NA)}</div>` : "");
    }).join("");
    return panel("gear", "Security Controls", "Core security controls and hardening measures", rows || unavailable);
  }
  function renderProviders(d) {
    let body;
    if (!d.providers_available) body = unavailable;
    else if (!(d.providers || []).length) body = `<div class="sc-empty">No providers configured</div>`;
    else body = d.providers.map((p) => `<div class="sc-row"><span class="sc-rowi">${ic("net")}</span><span class="sc-k">${E(p.name)}${p.kind === "gateway" ? ' <em class="sc-tag">Gateway</em>' : ""}</span>` +
      `${isNum(p.keys) ? `<small class="sc-dim">${p.healthy_keys}/${p.keys} key${p.keys === 1 ? "" : "s"}</small>` : ""}${chip(p.level, p.state)}</div>`).join("");
    return panel("net", "Provider Security", "Health of configured AI providers and gateway connections", body);
  }
  function renderTools(d) {
    const t = d.tools;
    if (!t || !t.available) return panel("cube", "Tool & Execution Security", "Tool registry analysis and execution permission control", unavailable);
    const body = kv("Total Tools", t.total) + kv("High Risk", t.high) + kv("Medium Risk", t.medium) + kv("Low Risk", t.low) +
      kv("Confirmation Required", t.confirmation_required) + kv("Agent Forbidden", t.agent_forbidden) +
      kv("Tool Errors (" + t.counted + ")", t.errors) + kv("Tool Calls (" + t.counted + ")", t.calls);
    return panel("cube", "Tool & Execution Security", "Tool registry analysis and execution permission control", body);
  }
  function renderRow2(d) {
    const rows = (a) => (a && a.length ? a.map((r) => kv(r.k, r.v, r.v == null ? "na" : r.level)).join("") : unavailable);
    return panel("link", "Web3 Security Policy", "Transaction safety and blockchain guardrails", rows(d.web3_rows)) +
      panel("monitor", "Runtime Security", "Runtime environment protections", rows(d.runtime_rows));
  }

  /* ---------------------------- emergency shutdown ------------------------ */
  function renderEmergency() {
    const em = SC.data && SC.data.emergency, on = !!(em && em.active), sd = SC.shutdown;
    const badge = !em ? chip("na", NA) : on ? chip("danger", "Shutdown Active") : chip("ok", "Active");
    let result = "";
    const last = sd.result || (on && em.last_result);
    if (last && last.subsystems) {
      result = `<ul class="sc-res">${last.subsystems.map((s) => `<li class="lv-${s.status === "failed" ? "danger" : s.status === "absent" ? "na" : "ok"}"><b>${E(s.subsystem)}</b> — ${E(s.status)}: ${E(s.detail)}</li>`).join("")}</ul>`;
    }
    return `<section class="sc-panel sc-danger"><header class="sc-ph"><span class="sc-tile danger">${ic("power")}</span><div><h3>Emergency Shutdown</h3><p>Immediately stop all agents, tools, workflows and runtime execution</p></div><div class="sc-badge">${badge}</div></header>` +
      `<div class="sc-warnbox"><span class="sc-warnic">${ic("alert")}</span><p>${on ? "Agent execution is disabled. The Astra server is still running — release the shutdown to let agents run again." : "This will stop all running agents, tasks, tools, browser sessions and runtime processes. You can start the system again after shutdown."}</p></div>` +
      (sd.error ? `<div class="sc-err" role="alert">${E(sd.error)}</div>` : "") + result +
      `<button class="sc-btn ${on ? "" : "danger"} wide" data-act="${on ? "release" : "shutdown"}"${!em || sd.busy ? " disabled" : ""}>${ic("power")}${on ? "Release Shutdown" : "Shut Down All Agents"}</button></section>`;
  }

  /* ------------------------------ backup / import ------------------------- */
  const CATS = (list) => (list || []).map((c) => `<li>${E(c.label)}${isNum(c.items) ? ` <b>${c.items}</b>` : ""}${c.restorable === false ? ' <em class="sc-tag">reference</em>' : ""}</li>`).join("");
  function renderBackup() {
    const b = SC.backup, i = SC.imp;
    let made = "";
    if (b.created) {
      const m = b.created.manifest || {};
      made = `<div class="sc-meta"><b>${E(b.created.filename)}</b>` + kv("Backup version", m.format_version) + kv("Created", m.created_at) + kv("Astra version", m.astra_version) +
        kv("Data size", bytes(m.data_size_bytes)) + kv("Secrets included", m.secrets_included === false ? "No — never exported" : null, m.secrets_included === false ? "ok" : "na") +
        `<ul class="sc-cats">${CATS(Object.entries(m.categories || {}).map(([k, v]) => ({ label: v.label, items: v.items, restorable: v.restorable })))}</ul></div>`;
    }
    let imp = "";
    if (i.preview) {
      const pv = i.preview, m = pv.backup;
      const cats = pv.categories.map((c) => `<label class="sc-cat"><input type="checkbox" data-cat="${E(c.id)}"${c.restorable ? "" : " disabled"}${c.restorable && !i.skip[c.id] ? " checked" : ""}> ${E(c.label)}` +
        `${isNum(c.items) ? ` <b>${c.items}</b>` : ""}${c.conflicts ? ` <em class="sc-tag warn">${c.conflicts} already exist</em>` : ""}${c.restorable ? "" : ' <em class="sc-tag">reference only</em>'}</label>`).join("");
      const strat = [["keep_existing", "Keep existing"], ["merge", "Merge"], ["replace", "Replace matches"]]
        .map(([v, l]) => `<option value="${v}"${i.strategy === v ? " selected" : ""}>${l}</option>`).join("");
      imp = `<div class="sc-meta"><b>Backup found</b>` + kv("Version", m.format_version) + kv("Created", m.created_at) + kv("Astra version", m.astra_version) + kv("Size", bytes(m.data_size_bytes)) +
        (pv.compatibility.notes || []).map((n) => `<div class="sc-note">${E(n)}</div>`).join("") + `<div class="sc-cats">${cats}</div>` +
        `<label class="sc-strat">If an item already exists <select class="sc-sel" id="sc-strategy" aria-label="Conflict strategy">${strat}</select></label>` +
        `<button class="sc-btn primary wide" data-act="import"${i.busy ? " disabled" : ""}>${ic("up")}Import &amp; Restore</button></div>`;
    }
    let res = "";
    if (i.result) {
      res = `<div class="sc-meta ok"><b>Restore complete${Object.values(i.result.verified || {}).every(Boolean) ? " — verified" : ""}</b><ul class="sc-res">` +
        Object.entries(i.result.results || {}).map(([k, r]) => `<li><b>${E(k)}</b> — ${r.added} added, ${r.updated} updated, ${r.skipped} skipped${r.renamed ? ", " + r.renamed + " renamed" : ""}${r.note ? " · " + E(r.note) : ""}</li>`).join("") +
        `</ul><div class="sc-note">Safety snapshot of previous data: ${E(i.result.safety_snapshot)}</div>${(i.result.notes || []).map((n) => `<div class="sc-note">${E(n)}</div>`).join("")}</div>`;
    }
    return `<section class="sc-panel sc-backup"><header class="sc-ph"><span class="sc-tile">${ic("db")}</span><div><h3>Total Data Backup &amp; Import</h3><p>Backup and restore all Astra data (agents, tools, settings, workflows, preferences, etc.)</p></div></header>` +
      `<div class="sc-bgrid"><div class="sc-half"><span class="sc-tile lg">${ic("down")}</span><div><h4>Create Backup</h4><p>Create a complete backup of your Astra data to restore it on this PC or another device. Secrets are never included.</p>` +
      `${b.error ? `<div class="sc-err" role="alert">${E(b.error)}</div>` : ""}<button class="sc-btn primary wide" data-act="backup"${b.busy ? " disabled" : ""}>${ic("down")}${b.busy ? "Creating…" : "Create Backup"}</button>${made}</div></div>` +
      `<div class="sc-half${i.drag ? " drag" : ""}" id="sc-drop"><span class="sc-tile lg purple">${ic("up")}</span><div><h4>Import Astra Data</h4><p>Import a previously created Astra backup file (.zip) — or drop it here.</p>` +
      `${i.error ? `<div class="sc-err" role="alert">${E(i.error)}</div>` : ""}<input type="file" id="sc-file" accept=".zip,application/zip" hidden>` +
      `<button class="sc-btn violet wide" data-act="choose"${i.busy ? " disabled" : ""}>${ic("file")}${i.busy ? "Working…" : "Choose File and Import"}</button>${imp}${res}</div></div></div></section>`;
  }

  /* -------------------------------- dialog -------------------------------- */
  function renderDialog() {
    const d = SC.dialog;
    if (!d) return "";
    return `<div class="sc-scrim" data-act="dialog-cancel"></div><div class="sc-dialog${d.danger ? " danger" : ""}" role="dialog" aria-modal="true" aria-labelledby="sc-dt"><h3 id="sc-dt">${E(d.title)}</h3><p>${E(d.body)}</p>` +
      `<div class="sc-dact"><button class="sc-btn" data-act="dialog-cancel">Cancel</button><button class="sc-btn ${d.danger ? "danger" : "primary"}" data-act="dialog-ok">${E(d.ok)}</button></div></div>`;
  }

  /* -------------------------------- painting ------------------------------ */
  const PAGE = `<div class="sc-root"><div class="sc-head" id="sc-head"></div><div class="sc-summary" id="sc-summary"></div><div class="sc-grid" id="sc-grid"></div>` +
    `<div class="sc-row2" id="sc-row2"></div><div class="sc-bottom"><div id="sc-emergency"></div><div id="sc-backup"></div></div><div id="sc-dialog"></div></div>`;
  const set = (id, html) => { const el = byId(id); if (el) el.innerHTML = html; };
  function paintData() {
    set("sc-head", renderHead()); set("sc-summary", renderSummary());
    const d = SC.data;
    set("sc-grid", d ? renderControls(d) + renderProviders(d) + renderTools(d) : "");
    set("sc-row2", d ? renderRow2(d) : "");
    set("sc-emergency", renderEmergency());
  }
  const paintBackup = () => set("sc-backup", renderBackup());
  const paintDialog = () => set("sc-dialog", renderDialog());
  function paint() { paintData(); paintBackup(); paintDialog(); }

  /* --------------------------------- data --------------------------------- */
  async function load() {
    if (SC.loading) return;
    SC.loading = true; set("sc-head", renderHead());
    try {
      const r = await api("/api/security/status");
      if (r && r.ok) { SC.data = r.data; SC.error = null; SC.checkedAt = Date.now(); }
      else SC.error = (r && (r.error || r.message)) || "security status unavailable";
    } catch (e) { SC.error = String((e && e.message) || e); }
    SC.loading = false; paintData();
  }

  /* ---------------------------- emergency actions ------------------------- */
  async function post(path, body) { return api(path, { method: "POST", body }); }
  async function doShutdown() {
    SC.shutdown = { busy: true, result: null, error: null }; paintData();
    const r = await post("/api/security/emergency-shutdown", { confirm: true });
    if (r && r.ok) SC.shutdown = { busy: false, result: r.data, error: null };
    else SC.shutdown = { busy: false, result: null, error: (r && r.error) || "shutdown request failed" };
    await load(); paintData();
  }
  async function doRelease() {
    SC.shutdown = { busy: true, result: null, error: null }; paintData();
    const r = await post("/api/security/emergency-release", { confirm: true });
    SC.shutdown = { busy: false, result: null, error: r && r.ok ? null : ((r && r.error) || "release request failed") };
    await load(); paintData();
  }

  /* ------------------------------- backup actions ------------------------- */
  async function createBackup() {
    const b = SC.backup; if (b.busy) return;
    SC.backup = { busy: true, created: null, error: null }; paintBackup();
    try {
      const res = await fetch("/api/security/backup/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (!res.ok) {
        let msg = "backup failed"; try { msg = (await res.json()).error || msg; } catch (_) { /* keep */ }
        SC.backup = { busy: false, created: null, error: msg }; return paintBackup();
      }
      let meta = {}; try { meta = JSON.parse(res.headers.get("X-Astra-Backup") || "{}"); } catch (_) { /* keep */ }
      const blob = await res.blob();
      if (typeof URL !== "undefined" && URL.createObjectURL && document.createElement) {
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = meta.filename || "astra_backup.zip";
        if (document.body && document.body.appendChild) document.body.appendChild(a);
        a.click(); if (a.remove) a.remove(); const rv = setTimeout(() => URL.revokeObjectURL(a.href), 4000); if (rv && rv.unref) rv.unref();
      }
      SC.backup = { busy: false, created: { filename: meta.filename || "astra_backup.zip", manifest: meta.manifest || {} }, error: null };
    } catch (e) { SC.backup = { busy: false, created: null, error: "network: " + ((e && e.message) || e) }; }
    paintBackup();
  }
  function upload(path, extra) {
    const fd = new FormData(); fd.append("file", SC.imp.file, SC.imp.file.name || "backup.zip");
    Object.entries(extra || {}).forEach(([k, v]) => fd.append(k, v));
    return fetch(path, { method: "POST", body: fd }).then(async (res) => { try { return await res.json(); } catch (_) { return { ok: false, error: "unexpected response" }; } });
  }
  async function pickFile(file) {
    if (!file) return;
    SC.imp = { busy: true, file, preview: null, strategy: "keep_existing", skip: {}, result: null, error: null, drag: false }; paintBackup();
    try {
      const r = await upload("/api/security/backup/inspect");
      if (r && r.ok) SC.imp = Object.assign(SC.imp, { busy: false, preview: r.data });
      else SC.imp = Object.assign(SC.imp, { busy: false, error: (r && r.error) || "could not read backup" });
    } catch (e) { SC.imp = Object.assign(SC.imp, { busy: false, error: "network: " + ((e && e.message) || e) }); }
    paintBackup();
  }
  async function doImport() {
    const i = SC.imp; i.busy = true; i.error = null; paintBackup();
    const cats = (i.preview.categories || []).filter((c) => c.restorable && !i.skip[c.id]).map((c) => c.id);
    try {
      const r = await upload("/api/security/backup/restore", { confirm: "true", strategy: i.strategy, categories: cats.join(",") });
      if (r && r.ok) { SC.imp = Object.assign(SC.imp, { busy: false, result: r.data, preview: null }); }
      else Object.assign(SC.imp, { busy: false, error: (r && r.error) || "restore failed" });
    } catch (e) { Object.assign(SC.imp, { busy: false, error: "network: " + ((e && e.message) || e) }); }
    paintBackup();
  }

  /* -------------------------------- dialogs ------------------------------- */
  function ask(kind) {
    const T = {
      shutdown: { title: "Emergency Shutdown", body: "This will stop all running agents, tasks, workflows and runtime operations.", ok: "Shut Down All Agents", danger: true },
      release: { title: "Release Emergency Shutdown", body: "Agents, workflows and tools will be allowed to run again. Stopped sessions are not restarted.", ok: "Release Shutdown", danger: false },
      import: { title: "Import & Restore", body: "The selected data will be added to this Astra using your conflict setting. A safety snapshot of your current data is saved first.", ok: "Import & Restore", danger: false },
    };
    SC.dialog = Object.assign({ kind }, T[kind]); paintDialog();
  }
  async function confirmDialog() {
    const k = SC.dialog && SC.dialog.kind; SC.dialog = null; paintDialog();
    if (k === "shutdown") await doShutdown(); else if (k === "release") await doRelease(); else if (k === "import") await doImport();
  }

  /* ------------------------------ auto refresh ---------------------------- */
  function schedule() {
    clearInterval(SC.timer); clearInterval(SC.clock); SC.timer = SC.clock = 0;
    if (!SC.active) return;
    SC.clock = setInterval(() => { const el = byId("sc-ago"); if (el) el.textContent = ago(SC.checkedAt); }, 10000);
    if (SC.auto) SC.timer = setInterval(() => { if (typeof document !== "undefined" && document.hidden) return; load(); }, SC.intervalMs);
    [SC.clock, SC.timer].forEach((t) => { if (t && t.unref) t.unref(); });
  }

  /* --------------------------------- events ------------------------------- */
  function bind(host) {
    host.addEventListener("click", (e) => {
      const t = e.target && e.target.closest ? e.target : null; if (!t) return;
      const tog = t.closest("[data-toggle]"); if (tog) { const id = tog.dataset.toggle; SC.open[id] = !SC.open[id]; return paintData(); }
      const b = t.closest("[data-act]"); if (!b) return;
      const act = b.dataset.act;
      if (act === "scan" || act === "refresh") return load();
      if (act === "auto") { SC.auto = !SC.auto; schedule(); return paintData(); }
      if (act === "shutdown" || act === "release") return ask(act);
      if (act === "backup") return createBackup();
      if (act === "choose") { const f = byId("sc-file"); if (f && f.click) f.click(); return; }
      if (act === "import") return ask("import");
      if (act === "dialog-cancel") { SC.dialog = null; return paintDialog(); }
      if (act === "dialog-ok") return confirmDialog();
    });
    host.addEventListener("change", (e) => {
      const t = e.target || {};
      if (t.id === "sc-interval") { SC.intervalMs = parseInt(t.value, 10) || 30000; schedule(); }
      else if (t.id === "sc-strategy") SC.imp.strategy = t.value;
      else if (t.id === "sc-file") { const f = t.files && t.files[0]; if (f) pickFile(f); }
      else if (t.dataset && t.dataset.cat) SC.imp.skip[t.dataset.cat] = !t.checked;
    });
    host.addEventListener("dragover", (e) => { if (e.target && e.target.closest && e.target.closest("#sc-drop")) { if (e.preventDefault) e.preventDefault(); if (!SC.imp.drag) { SC.imp.drag = true; byId("sc-drop") && byId("sc-drop").classList.add("drag"); } } });
    host.addEventListener("dragleave", () => { SC.imp.drag = false; byId("sc-drop") && byId("sc-drop").classList.remove("drag"); });
    host.addEventListener("drop", (e) => {
      if (!(e.target && e.target.closest && e.target.closest("#sc-drop"))) return;
      if (e.preventDefault) e.preventDefault(); SC.imp.drag = false;
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]; if (f) pickFile(f);
    });
  }
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && SC.dialog) { SC.dialog = null; paintDialog(); } });
  }

  /* ------------------------------ tab lifecycle --------------------------- */
  function mount() {
    const host = byId("tab-security-center"); if (!host) return;
    if (!SC.mounted) { host.innerHTML = PAGE; SC.mounted = true; bind(host); paint(); }
  }
  async function open() { SC.active = true; mount(); schedule(); await load(); }
  function onTab(name) { const was = SC.active; SC.active = name === "security-center"; if (was !== SC.active) schedule(); }

  if (window.Astra && window.Astra.loaders) window.Astra.loaders["security-center"] = open;
  else if (typeof loaders !== "undefined") loaders["security-center"] = open;
  // Stop polling the moment the operator leaves the page (wraps, never replaces, showTab).
  const prevShow = window.showTab;
  if (typeof prevShow === "function") window.showTab = function (name) { onTab(name); return prevShow.apply(this, arguments); };

  window.SecurityCenter = { state: SC, open, mount, load, paint, onTab, ask, confirmDialog, createBackup, pickFile, doImport, NA };
})();
