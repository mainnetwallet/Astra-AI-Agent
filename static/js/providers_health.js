/* Providers Health Test — page chrome (static/js/providers_health.js).
 *
 * ONE unified operations surface over the two systems that already exist:
 * direct AI providers (AstraRouter provider health) and the Astra AI Gateway
 * (its own connection/model health). This module owns only the presentation
 * shell — header, overall health, summary cards, filters/search and the Live
 * Test Activity rail.
 *
 * It NEVER fetches anything: every number is derived from the state astra.js
 * published after GET /api/providers (which already carries both the provider
 * table and the Gateway block) — see window.AstraProviders. Every action
 * forwards to the EXISTING control in astra.js (the Test All button, the row
 * Test buttons, loaders.providers), so there is one test engine, one set of
 * endpoints and one running-test/persistence implementation.
 *
 * Live Test Activity is fed by the test lifecycle astra.js emits on the
 * document event bus while a manual test runs; the shared SSE feed is not
 * re-subscribed here.
 */
"use strict";
(function () {
  const MAX_ACTIVITY = 60;
  const ACTIVITY = [];
  let ACTIVITY_SEQ = 0;
  const STATE = { view: "all", status: "all", search: "", key: "" };

  const api = () => (window.AstraProviders = window.AstraProviders || {});

  /* ------------------------------- helpers -------------------------------- */
  const pct = (n, d) => (d > 0 ? Math.round((n / d) * 100) : null);
  const cardNum = (v) => (v === null || v === undefined ? "--" : String(v));

  function tsOf(v) {
    if (v === null || v === undefined || v === "") return null;
    if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
    const d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  function newest(list) {
    let best = null;
    (list || []).forEach((v) => { const d = tsOf(v); if (d && (!best || d > best)) best = d; });
    return best;
  }
  function clock(d) {
    return d ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "--";
  }
  function shortDate(d) {
    return d ? d.toLocaleDateString([], { day: "2-digit", month: "short", year: "numeric" }) : "";
  }

  /* Per-model health merged across BOTH systems, from saved results only.
   * A model counts as healthy when at least one saved probe succeeded, failed
   * when it was probed and none succeeded, unknown when it was never probed. */
  function modelStates() {
    const out = new Map();
    const merge = (m, state) => {
      const prev = out.get(m);
      if (prev === "healthy") return;
      if (state === "healthy") { out.set(m, "healthy"); return; }
      if (prev === undefined || prev === "unknown") out.set(m, state);
    };
    const P = api();
    Object.values(P.providers || {}).forEach((p) => {
      (p.models || []).forEach((m) => {
        const results = Object.values((p.key_results || {})[m] || {}).filter(Boolean);
        merge(m, results.length ? (results.some((r) => r.ok) ? "healthy" : "failed") : "unknown");
      });
    });
    Object.entries(P.gateway || {}).forEach(([, c]) => {
      (c.models || []).forEach((m) => {
        const h = (c.model_health || {})[m];
        const tested = h && ((h.success_count || 0) + (h.failure_count || 0)) > 0;
        merge(m, !tested ? "unknown"
          : (h.last_success && (!h.last_failure || h.last_success > h.last_failure)) ? "healthy" : "failed");
      });
    });
    return out;
  }

  function summary() {
    const P = api();
    const provs = Object.values(P.providers || {});
    const conns = Object.entries(P.gateway || {});
    const models = modelStates();
    let healthy = 0, failed = 0;
    models.forEach((s) => { if (s === "healthy") healthy += 1; else if (s === "failed") failed += 1; });
    const keys = provs.reduce((n, p) => n + (p.keys || []).length, 0) +
                 conns.reduce((n, [, c]) => n + (c.keys || []).length, 0);
    const stamps = [];
    provs.forEach((p) => Object.values(p.key_results || {}).forEach((byKey) =>
      Object.values(byKey || {}).forEach((r) => { if (r) stamps.push(r.tested_at); })));
    conns.forEach(([, c]) => Object.values(c.model_health || {}).forEach((h) => {
      if (h) stamps.push(h.last_success, h.last_failure);
    }));
    return {
      providers: provs.length,
      connections: conns.length,
      models: models.size,
      keys,
      healthy,
      failed,
      lastTest: newest(stamps),
      total: models.size,
      tested: healthy + failed,
    };
  }

  /* --------------------------- metric cards / ring ------------------------- */
  function renderMetrics() {
    const host = $("#ph-metrics");
    if (!host) return;
    const s = summary();
    const p = pct(s.healthy, s.total);
    const cards = [
      { k: "AI Providers", v: cardNum(s.providers), s: "providers configured", tone: "purple" },
      { k: "Gateway Connections", v: cardNum(s.connections), s: "connections configured", tone: "blue" },
      { k: "Total Models", v: cardNum(s.models), s: "models available", tone: "purple" },
      { k: "Total Keys", v: cardNum(s.keys), s: "API keys configured", tone: "amber" },
      { k: "Healthy Models", v: cardNum(s.healthy), s: s.tested ? `of ${s.tested} tested` : "no results yet", tone: "green" },
      { k: "Failed Models", v: cardNum(s.failed), s: s.tested ? `of ${s.tested} tested` : "no results yet", tone: "red" },
      { k: "Last Test", v: s.lastTest ? clock(s.lastTest) : "--", s: shortDate(s.lastTest) || "no test recorded", tone: "blue" },
    ];
    host.innerHTML = cards.map((c) =>
      `<div class="ph-metric ${c.tone}"><span class="ph-metric-k">${esc(c.k)}</span>` +
      `<b class="ph-metric-v">${esc(c.v)}</b><span class="ph-metric-s">${esc(c.s)}</span></div>`).join("");

    const ring = $("#ph-ring-fg");
    if (ring) {
      const r = 18, circ = 2 * Math.PI * r;
      ring.setAttribute("stroke-dasharray", circ.toFixed(2));
      ring.setAttribute("stroke-dashoffset", (circ * (1 - (p || 0) / 100)).toFixed(2));
      ring.setAttribute("data-pct", String(p === null ? 0 : p));
    }
    const pctEl = $("#ph-pct");
    if (pctEl) pctEl.textContent = p === null ? "--" : p + "%";
    const note = $("#ph-overall-note");
    if (note) note.textContent = s.total ? `${s.healthy} / ${s.total} models healthy` : "no models configured";
  }

  /* ------------------------------ filter counts ---------------------------- */
  function rowStats() {
    const provRows = $$("#providers-list .provider-card");
    const gwRows = $$("#gateway-card .provider-card");
    const all = provRows.concat(gwRows);
    return {
      counts: {
        all: all.length, providers: provRows.length, gateway: gwRows.length,
        healthy: all.filter((r) => r.dataset.phStatus === "healthy").length,
        failed: all.filter((r) => r.dataset.phStatus === "failed").length,
        running: all.filter((r) => r.dataset.phRunning === "1").length,
      },
      provHealthy: provRows.filter((r) => r.dataset.phStatus === "healthy").length,
      provFailed: provRows.filter((r) => r.dataset.phStatus === "failed").length,
      gwHealthy: gwRows.filter((r) => r.dataset.phStatus === "healthy").length,
      gwFailed: gwRows.filter((r) => r.dataset.phStatus === "failed").length,
      provModels: provRows.reduce((n, r) => n + (parseInt(r.dataset.phCount, 10) || 0), 0),
      gwModels: gwRows.reduce((n, r) => n + (parseInt(r.dataset.phCount, 10) || 0), 0),
      provKeys: provRows.reduce((n, r) => n + (parseInt(r.dataset.phKeys, 10) || 0), 0),
      gwKeys: gwRows.reduce((n, r) => n + (parseInt(r.dataset.phKeys, 10) || 0), 0),
    };
  }

  function badge(text, tone) {
    return `<span class="ph-badge ${esc(tone)}"><span class="status-dot ${tone === "healthy" ? "ok" : tone === "failed" ? "bad" : "warn"}"></span>${esc(text)}</span>`;
  }

  function renderBadges() {
    const st = rowStats();
    const p = $("#ph-prov-badges");
    if (p) p.innerHTML = [badge(`${st.provHealthy} healthy`, "healthy"), badge(`${st.provFailed} failed`, "failed"),
      badge(`${st.provModels} models`, "neutral"), badge(`${st.provKeys} keys`, "neutral")].join("");
    const g = $("#ph-gw-badges");
    if (g) g.innerHTML = [badge(`${st.gwHealthy} healthy`, "healthy"), badge(`${st.gwFailed} failed`, "failed"),
      badge(`${st.gwModels} models`, "neutral"), badge(`${st.gwKeys} keys`, "neutral")].join("");
    $$("#ph-toolbar [data-ph-count]").forEach((el) => {
      const k = el.dataset.phCount;
      if (k in st.counts) el.textContent = String(st.counts[k]);
    });
  }

  /* -------------------------------- filters -------------------------------- */
  function matchesSearch(row) {
    if (!STATE.search) return true;
    const q = STATE.search;
    const name = (row.dataset.phName || "").toLowerCase();
    const models = (row.dataset.phModels || "").toLowerCase();
    return name.indexOf(q) >= 0 || models.indexOf(q) >= 0;
  }
  function matchesKey(row) {
    if (!STATE.key) return true;
    const labels = (row.dataset.phKeyLabels || "").split("|");
    return labels.indexOf(STATE.key) >= 0;
  }
  function matchesStatus(row) {
    if (STATE.status === "all") return true;
    if (STATE.status === "running") return row.dataset.phRunning === "1";
    return row.dataset.phStatus === STATE.status;
  }

  function applyFilters() {
    const showProv = STATE.view === "all" || STATE.view === "providers";
    const showGw = STATE.view === "all" || STATE.view === "gateway";
    const secP = $("#ph-sec-providers"), secG = $("#ph-sec-gateway");
    if (secP) secP.hidden = !showProv;
    if (secG) secG.hidden = !showGw;

    $$("#providers-list .provider-card, #gateway-card .provider-card").forEach((row) => {
      const inView = (row.dataset.phKind === "gateway" ? showGw : showProv);
      row.classList.toggle("ph-filtered", !(inView && matchesSearch(row) && matchesKey(row) && matchesStatus(row)));
      // Only the models the caller asked for stay visible inside a row.
      $$(".model-health-row", row).forEach((mr) => {
        const q = STATE.search;
        const hit = !q || String(mr.dataset.phModel || "").toLowerCase().indexOf(q) >= 0
          || (row.dataset.phName || "").toLowerCase().indexOf(q) >= 0;
        mr.classList.toggle("ph-filtered", !hit);
      });
    });

    const empty = $("#ph-empty");
    if (empty) {
      const vis = $$("#providers-list .provider-card, #gateway-card .provider-card")
        .some((r) => !r.classList.contains("ph-filtered"));
      empty.hidden = vis || !(api().ready);
    }
  }

  function renderKeyFilter() {
    const sel = $("#ph-key-filter");
    if (!sel) return;
    const labels = new Set();
    const P = api();
    Object.values(P.providers || {}).forEach((p) => (p.keys || []).forEach((k) => labels.add(k.label)));
    Object.values(P.gateway || {}).forEach((c) => (c.keys || []).forEach((k) => labels.add(k.label)));
    const want = [...labels].sort();
    const have = [...sel.options].slice(1).map((o) => o.value);
    if (want.length !== have.length || want.some((v, i) => v !== have[i])) {
      sel.innerHTML = `<option value="">All keys</option>` +
        want.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("");
      sel.value = STATE.key;
    }
  }

  /* --------------------------- Live Test Activity -------------------------- */
  function activityRow(e) {
    const tone = e.state === "ok" ? "ok" : e.state === "bad" ? "bad" : "run";
    const scope = e.kind === "gateway" ? "Astra Gateway" : (e.owner || "");
    const ms = (Number(e.latency_ms) > 0 && api().formatMs) ? api().formatMs(e.latency_ms) : null;
    const right = e.state === "testing" ? "Testing..."
      : e.state === "ok" ? (ms ? `Test successful · ${ms}` : "Test successful")
      : `Test failed${e.error ? " · " + e.error : ""}`;
    return `<div class="ph-act-row ${tone}">` +
      `<span class="ph-act-line"></span>` +
      `<div class="ph-act-body">` +
      `<div class="ph-act-head"><b>${esc(scope)}</b><span class="ph-act-dot">·</span><span class="ph-act-model">${esc(e.model || "")}</span>` +
      `<time class="ph-act-time">${esc(e.clock)}</time></div>` +
      `<div class="ph-act-text">${esc(right)}</div>` +
      `</div></div>`;
  }
  function renderActivity() {
    const feed = $("#ph-activity-feed");
    if (!feed) return;
    if (!ACTIVITY.length) {
      feed.innerHTML = `<div class="ph-act-empty">No test activity yet. Run a test to see live results.</div>`;
      return;
    }
    feed.innerHTML = ACTIVITY.slice(0, MAX_ACTIVITY).map(activityRow).join("");
  }
  function onTestEvent(ev) {
    const d = (ev && ev.detail) || {};
    if (!d.owner) return;
    const id = d.kind + "\u0000" + d.owner + "\u0000" + (d.model || "");
    let entry = ACTIVITY.find((e) => e.id === id);
    if (d.phase === "start") {
      entry = { id, kind: d.kind, owner: d.owner, model: d.model || "", state: "testing", clock: clock(new Date()), seq: ACTIVITY_SEQ++ };
      const i = ACTIVITY.indexOf(entry);
      if (i >= 0) ACTIVITY.splice(i, 1);
      ACTIVITY.unshift(entry);
    } else if (d.phase === "done") {
      if (!entry) {
        entry = { id, kind: d.kind, owner: d.owner, model: d.model || "", seq: ACTIVITY_SEQ++ };
        ACTIVITY.unshift(entry);
      }
      entry.state = d.ok ? "ok" : "bad";
      entry.latency_ms = d.latency_ms;
      entry.error = d.error;
      entry.clock = clock(new Date());
    } else {
      return;
    }
    renderActivity();
  }

  /* --------------------------------- render -------------------------------- */
  function render() {
    renderMetrics();
    renderBadges();
    renderKeyFilter();
    applyFilters();
  }

  /* ------------------------------- wiring ---------------------------------- */
  function wire() {
    const tabs = $("#ph-view-tabs");
    if (tabs) tabs.addEventListener("click", (e) => {
      const b = e.target.closest("[data-ph-view]");
      if (!b) return;
      STATE.view = b.dataset.phView;
      $$("#ph-view-tabs button").forEach((x) => {
        const on = x === b;
        x.classList.toggle("active", on);
        x.setAttribute("aria-selected", on ? "true" : "false");
      });
      applyFilters();
    });

    const st = $("#ph-status-filters");
    if (st) st.addEventListener("click", (e) => {
      const b = e.target.closest("[data-ph-status]");
      if (!b) return;
      STATE.status = b.dataset.phStatus;
      $$("#ph-status-filters button").forEach((x) => x.classList.toggle("active", x === b));
      applyFilters();
    });

    const search = $("#ph-search");
    if (search) search.addEventListener("input", () => {
      STATE.search = String(search.value || "").trim().toLowerCase();
      applyFilters();
    });

    const keySel = $("#ph-key-filter");
    if (keySel) keySel.addEventListener("change", () => {
      STATE.key = keySel.value || "";
      applyFilters();
    });

    const refresh = $("#ph-refresh");
    if (refresh) refresh.addEventListener("click", () => {
      refresh.disabled = true;
      const done = () => { refresh.disabled = false; };
      const r = api().refresh ? api().refresh() : null;
      if (r && r.then) r.then(done, done); else done();
    });

    // Both Test All entry points run the SAME existing operation: the toolbar
    // button forwards to the primary button, which astra.js owns (progress
    // label, disabled state and restored-run state included). A second click
    // can never start a duplicate run because that button is disabled while
    // one is in flight.
    const testAll2 = $("#ph-test-all");
    if (testAll2) testAll2.addEventListener("click", () => {
      if (api().testAll) api().testAll();
    });

    const clear = $("#ph-activity-clear");
    if (clear) clear.addEventListener("click", () => { ACTIVITY.length = 0; renderActivity(); });

    if (typeof document.addEventListener === "function") {
      document.addEventListener("astra:provider-test", onTestEvent);
      document.addEventListener("astra:providers-updated", render);
    }
  }

  function boot() {
    wire();
    renderActivity();
    render();
    // Keep the page chrome in step with every loader run (initial paint,
    // refresh, post-test resync) — loaders.providers is astra.js's own.
    const prev = loaders.providers;
    loaders.providers = async function () {
      const r = prev ? await prev.apply(this, arguments) : undefined;
      render();
      return r;
    };
  }

  // Exposed surface: the render entry point plus the pure view logic
  // (summary/model-state derivation, filters, activity reducer) so the
  // no-fake-data and filter behaviour can be asserted without a browser.
  window.ProvidersHealth = {
    render, state: STATE, summary, modelStates, activity: ACTIVITY,
    applyFilters, rowStats, renderMetrics, renderActivity, onTestEvent,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
