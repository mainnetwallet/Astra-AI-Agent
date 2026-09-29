/* Astra AI Agent — 🔧 Tool Center (dedicated tool-browsing page).
 *
 * A separate, user-facing page over the EXISTING universal tool registry.
 * The ONE data source is GET /api/tools (the ToolRegistry listing); nothing
 * is hardcoded and no second registry is created. Category names/counts
 * reuse the shared normalisation in system_map_model.js (toolGroupOf) so the
 * System Map and this page can never disagree.
 *
 * This is an INSPECTION surface, on purpose: there is no generic "run tool"
 * endpoint in the backend, so the detail panel shows only what the API
 * actually returns — description, real safety metadata (risk level, whether
 * confirmation is required, whether the Agent is structurally blocked from
 * the tool), the declared input/output schema, and real per-tool usage
 * stats. Fields the backend does not supply are not shown at all.
 *
 * Secrets are never read: provider keys, wallet material and tokens never
 * reach this file, and API bodies are already redacted server-side
 * (astra/security.py). Everything rendered goes through esc().
 *
 * DOM + wiring only; registers its tab loader the same way the other core
 * tabs do (window.Astra.loaders), so astra.js opens it on showTab().
 * The System Map keeps its own Tool System / ToolRegistry node — unchanged.
 */
(function () {
  "use strict";

  const M = window.SystemMapModel;
  const TC = {
    loaded: false,      // at least one successful /api/tools response
    loading: false,
    rows: [],           // normalised tools from the real registry
    raw: null,          // last /api/tools payload
    cat: "all",         // active category (a group label, or "all")
    query: "",          // search box text
    selected: null,     // selected tool name
    error: null,        // last load error (kept apart from stale rows)
    mounted: false,
  };

  /* Presentation-only maps (no data invented — a group/category the backend
   * does not return simply never appears). */
  const GROUP_ICONS = {
    Browser: "🌐", File: "📄", Memory: "🧠", Research: "🔎", System: "⚙️",
    Tasks: "🗒️", Terminal: "💻", Wallet: "👛", Web3: "⛓️", Ai: "✨",
    "Built-in": "🧰", Custom: "🧩",
  };
  const RISK_LABEL = {
    read: "Read", low_risk_write: "Low-risk write", browser_action: "Browser action",
    financial_action: "Financial", system_action: "System", admin: "Admin",
  };
  const iconFor = (g) => GROUP_ICONS[g] || "🔧";
  const slug = (g) => String(g || "").toLowerCase().replace(/[^a-z]/g, "");
  const riskLabel = (r) => RISK_LABEL[r] || (r ? String(r).replace(/_/g, " ") : "Read");
  /** Some tools carry their own name as the description; don't echo it twice. */
  const describe = (t) => (t.description && t.description !== t.name ? t.description : "");

  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const closest = (e, sel) => (e && e.target && e.target.closest ? e.target.closest(sel) : null);
  const byId = (id) => document.getElementById(id);
  const dur = (ms) => ms == null ? "—"
    : ms < 1 ? "<1 ms" : ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s";
  const isNarrow = () => !!(window.matchMedia && window.matchMedia("(max-width: 1000px)").matches);

  /* ------------------------------ normalise -------------------------------- */
  /** /api/tools payload -> rows; category label comes from the SHARED
   * system_map_model.toolGroupOf() so grouping stays identical everywhere. */
  function normalize(payload) {
    const tools = isObj(payload) && Array.isArray(payload.tools) ? payload.tools : [];
    const stats = isObj(payload) && isObj(payload.stats) ? payload.stats : {};
    const rows = [];
    tools.forEach((t) => {
      if (!isObj(t) || !t.name) return;
      const st = isObj(stats[t.name]) ? stats[t.name] : {};
      rows.push({
        name: String(t.name),
        description: String(t.description || "").trim(),
        rawCategory: t.category || null,
        group: M.toolGroupOf(t),
        schema: isObj(t.input_schema) ? t.input_schema : {},
        outputSchema: isObj(t.output_schema) ? t.output_schema : {},
        risk: t.risk_level || "read",
        requiresConfirmation: !!t.requires_confirmation,
        confirmationDelegate: t.confirmation_delegate || "",
        agentForbidden: !!t.agent_forbidden,
        plugin: t.plugin || "",
        timeoutS: num(t.timeout_s),
        retries: num(t.retries),
        rateLimit: num(t.rate_limit_per_min),
        idempotent: !!t.idempotent,
        supportsAsync: !!t.supports_async,
        strict: !!t.strict,
        calls: num(st.calls),
        errors: num(st.errors),
        avgMs: num(st.average_duration),
        lastCalled: st.last_called || "",
      });
    });
    rows.sort((a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name));
    return rows;
  }

  /* -------------------------------- filtering ------------------------------ */
  function visibleRows() {
    const q = TC.query.trim().toLowerCase();
    return TC.rows.filter((t) => {
      if (TC.cat !== "all" && t.group !== TC.cat) return false;
      if (!q) return true;
      return t.name.toLowerCase().includes(q)
        || t.description.toLowerCase().includes(q)
        || t.group.toLowerCase().includes(q)
        || String(t.rawCategory || "").toLowerCase().includes(q);
    });
  }

  /* --------------------------------- render -------------------------------- */
  function skeleton() {
    let rows = "";
    for (let i = 0; i < 6; i++) {
      rows += `<div class="tc-skel"><i class="a"></i><i class="b"></i><i class="c"></i></div>`;
    }
    return `<div class="tc-state" style="padding:20px 16px 8px">Loading tools…</div>${rows}`;
  }

  function flagChips(t) {
    const out = [`<span class="tc-risk r-${esc(t.risk)}" title="Risk level">${esc(riskLabel(t.risk))}</span>`];
    if (t.requiresConfirmation) {
      out.push(`<span class="tc-tag t-confirm" title="Requires confirmation">🔒 Confirm</span>`);
    }
    if (t.agentForbidden) {
      out.push(`<span class="tc-tag t-host" title="Not callable by the Agent (host-only)">⛔ Host-only</span>`);
    }
    return out.join("");
  }

  function rowHtml(t) {
    const sel = t.name === TC.selected;
    return `<button class="tc-row${sel ? " sel" : ""}" type="button" role="listitem"` +
      ` data-tool="${esc(t.name)}" aria-pressed="${sel}">` +
      `<span class="tc-tool"><span class="tc-ic" aria-hidden="true">${iconFor(t.group)}</span>` +
      `<span class="tc-name">${esc(t.name)}</span></span>` +
      `<span class="tc-desc" title="${esc(describe(t) || t.name)}">${esc(describe(t) || "—")}</span>` +
      `<span class="tc-tag t-${slug(t.group)}">${esc(t.group)}</span>` +
      `<span class="tc-flags">${flagChips(t)}</span></button>`;
  }

  function renderCats() {
    const box = byId("tc-cats");
    if (!box) return;
    if (TC.error && !TC.rows.length) { box.innerHTML = ""; return; }
    const counts = {};
    TC.rows.forEach((t) => { counts[t.group] = (counts[t.group] || 0) + 1; });
    const names = Object.keys(counts).sort((a, b) => a.localeCompare(b));
    const chip = (label, val, count) =>
      `<button class="tc-catbtn${TC.cat === val ? " active" : ""}" type="button"` +
      ` data-cat="${esc(val)}" aria-pressed="${TC.cat === val}">${esc(label)}` +
      (count == null ? "" : `<span class="n">${count}</span>`) + `</button>`;
    box.innerHTML = chip("All", "all", TC.rows.length) +
      names.map((n) => chip(n, n, counts[n])).join("");
  }

  function renderList() {
    const box = byId("tc-list");
    if (!box) return;
    if (TC.error && !TC.rows.length) {
      box.innerHTML = `<div class="tc-state">Unable to load tools.` +
        `<div class="tc-none" style="margin-top:6px">${esc(TC.error)}</div>` +
        `<button class="tc-btn" type="button" id="tc-retry">Retry</button></div>`;
      return;
    }
    if (!TC.rows.length) { box.innerHTML = skeleton(); return; }
    const rows = visibleRows();
    if (!rows.length) {
      box.innerHTML = `<div class="tc-state">No tools found` +
        `<div class="tc-none" style="margin-top:6px">Try another search or category.</div></div>`;
      return;
    }
    box.innerHTML = `<div class="tc-thead" role="presentation">` +
      `<span>Tool</span><span class="h-desc">Description</span>` +
      `<span>Category</span><span class="h-safety">Safety</span></div>` +
      `<div role="list">${rows.map(rowHtml).join("")}</div>`;
  }

  function kvHtml(pairs) {
    return pairs.map(([k, v]) =>
      `<div class="tc-kv"><span>${esc(k)}</span><span>${v}</span></div>`).join("");
  }

  function codeSec(title, obj) {
    const json = JSON.stringify(obj == null ? {} : obj, null, 2);
    return `<div class="tc-sec"><h4>${esc(title)}</h4><div class="tc-codewrap">` +
      `<pre class="tc-code">${esc(json)}</pre>` +
      `<button class="tc-copy" type="button" data-copyblock aria-label="Copy ${esc(title)}">⧉ Copy</button>` +
      `</div></div>`;
  }

  function renderDetail() {
    const box = byId("tc-detail");
    if (!box) return;
    const t = TC.rows.find((x) => x.name === TC.selected);
    if (!t) {
      box.innerHTML = `<div class="tc-state">Select a tool to inspect its schema, safety metadata and usage.</div>`;
      return;
    }
    const basic = [
      ["Category", `<span class="tc-tag t-${slug(t.group)}">${esc(t.group)}</span>`],
      ["Risk level", `<span class="tc-risk r-${esc(t.risk)}">${esc(riskLabel(t.risk))}</span>`],
      ["Confirmation", t.requiresConfirmation
        ? "Required" + (t.confirmationDelegate ? " · " + esc(t.confirmationDelegate) : "")
        : "Not required"],
      ["Agent access", t.agentForbidden ? "Blocked (host-only)" : "Available to Agent"],
      ["Source", t.plugin ? esc(t.plugin === "core" ? "Built-in (core)" : t.plugin) : "—"],
    ];

    const exec = [];
    if (t.timeoutS) exec.push(["Timeout", esc(t.timeoutS + " s")]);
    if (t.retries) exec.push(["Retries", esc(String(t.retries))]);
    if (t.rateLimit) exec.push(["Rate limit", esc(t.rateLimit + " / min")]);
    if (t.idempotent) exec.push(["Idempotent", "Yes"]);
    if (t.supportsAsync) exec.push(["Async", "Supported"]);
    if (t.strict) exec.push(["Strict args", "Yes"]);

    const usage = [];
    if (t.calls != null) {
      usage.push(["Calls", esc(String(t.calls))]);
      usage.push(["Errors", esc(String(t.errors == null ? 0 : t.errors))]);
      if (t.avgMs != null) usage.push(["Avg duration", esc(dur(t.avgMs))]);
      if (t.lastCalled) usage.push(["Last called", esc(t.lastCalled)]);
    }

    const hasSchema = isObj(t.schema) && Object.keys(t.schema).length > 0;
    const hasOutput = isObj(t.outputSchema) && Object.keys(t.outputSchema).length > 0;

    box.innerHTML = `<div class="tc-dhead">` +
      `<span class="tc-ic" aria-hidden="true">${iconFor(t.group)}</span>` +
      `<div class="tc-dt"><div class="tc-dname">${esc(t.name)}</div>` +
      `<div class="tc-dsub">${esc(describe(t) || "No description provided.")}</div></div>` +
      `<button class="tc-x" type="button" data-close aria-label="Close details">✕</button></div>` +
      `<div class="tc-flags" style="margin-top:10px">${flagChips(t)}</div>` +
      `<div class="tc-sec"><h4>Basic information</h4>${kvHtml(basic)}</div>` +
      (exec.length ? `<div class="tc-sec"><h4>Execution</h4>${kvHtml(exec)}</div>` : "") +
      (hasSchema
        ? codeSec("Input schema", t.schema)
        : `<div class="tc-sec"><h4>Input schema</h4><div class="tc-none">None declared.</div></div>`) +
      (hasOutput ? codeSec("Output schema", t.outputSchema) : "") +
      (usage.length ? `<div class="tc-sec"><h4>Usage</h4>${kvHtml(usage)}</div>` : "") +
      `<div class="tc-sec"><div class="tc-flags">` +
      `<button class="tc-btn" type="button" data-copyname="${esc(t.name)}">⧉ Copy tool name</button>` +
      (hasSchema ? `<button class="tc-btn" type="button" data-copyschema="${esc(t.name)}">⧉ Copy input schema</button>` : "") +
      `</div></div>`;
  }

  function renderAll() { renderCats(); renderList(); renderDetail(); }

  /* --------------------------------- states -------------------------------- */
  function setRefreshing(on) {
    const b = byId("tc-refresh");
    if (!b) return;
    b.disabled = on;
    b.innerHTML = on ? `<span class="spin">⟳</span> Refreshing…` : `⟳ Refresh`;
  }

  /* ----------------------------------- data -------------------------------- */
  async function load() {
    if (TC.loading) return;
    TC.loading = true;
    setRefreshing(true);
    if (!TC.rows.length) renderList();          // skeleton only when we have nothing yet
    const r = await api("/api/tools");
    TC.loading = false;
    if (r && r.ok && isObj(r.data) && Array.isArray(r.data.tools)) {
      TC.raw = r.data;
      TC.rows = normalize(r.data);
      TC.error = null;
      TC.loaded = true;
      if (TC.selected && !TC.rows.some((t) => t.name === TC.selected)) TC.selected = null;
    } else {
      TC.error = (r && r.error) ? String(r.error) : "request failed";
      // keep any previously loaded rows on screen
    }
    renderAll();
    setRefreshing(false);
  }

  /* -------------------------------- selection ------------------------------- */
  function selectTool(name) {
    TC.selected = name;
    renderList();
    renderDetail();
    const root = drawerRoot();
    if (isNarrow() && root) root.classList.add("open");
  }
  function closeDrawer() {
    const root = drawerRoot();
    if (root) root.classList.remove("open");
  }
  /** The `.tc` grid container owns the mobile drawer state (CSS: .tc.open). */
  function drawerRoot() { return byId("tc-root") || byId("tab-tool-center"); }

  /* -------------------------------- clipboard ------------------------------- */
  function flash(btn, text) {
    if (!btn) return;
    const prev = btn.textContent;
    btn.textContent = text;
    setTimeout(() => { btn.textContent = prev; }, 1200);
  }
  async function copyText(text, btn) {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text || "");
      } else if (typeof document.createElement === "function" && document.body) {
        const ta = document.createElement("textarea");
        ta.value = text || "";
        document.body.appendChild(ta);
        ta.select();
        if (document.execCommand) document.execCommand("copy");
        document.body.removeChild(ta);
      }
      flash(btn, "Copied");
    } catch (e) { flash(btn, "Copy failed"); }
  }

  /* ----------------------------------- wiring ------------------------------- */
  const PAGE = `
    <div class="tc" id="tc-root">
      <div class="tc-scrim" data-scrim></div>
      <div class="tc-col">
        <header class="tc-head">
          <div class="tc-logo" aria-hidden="true">🔧</div>
          <div class="tc-ht">
            <h1 class="tc-title">Tool Center</h1>
            <p class="tc-sub">Explore and inspect Astra's available tools</p>
          </div>
          <button class="tc-btn" id="tc-refresh" type="button">⟳ Refresh</button>
        </header>
        <div class="tc-search">
          <span class="tc-si" aria-hidden="true">🔍</span>
          <input class="tc-q" id="tc-q" type="search" autocomplete="off"
                 placeholder="Search tools by name, description or category…"
                 aria-label="Search tools">
        </div>
        <div class="tc-cats" id="tc-cats" role="group" aria-label="Filter by category"></div>
        <div class="tc-list" id="tc-list"></div>
      </div>
      <aside class="tc-detail" id="tc-detail" aria-label="Tool details"></aside>
    </div>`;

  function bind() {
    const root = byId("tab-tool-center");
    if (!root || !root.addEventListener) return;
    root.addEventListener("click", (e) => {
      const row = closest(e, "[data-tool]");
      if (row) { selectTool(row.dataset.tool); return; }
      const cat = closest(e, "[data-cat]");
      if (cat) { TC.cat = cat.dataset.cat; renderCats(); renderList(); return; }
      if (closest(e, "#tc-refresh") || closest(e, "#tc-retry")) { load(); return; }
      if (closest(e, "[data-close]")) { closeDrawer(); return; }
      if (closest(e, "[data-scrim]")) { closeDrawer(); return; }
      const cb = closest(e, "[data-copyblock]");
      if (cb) {
        const pre = cb.parentElement && cb.parentElement.querySelector
          ? cb.parentElement.querySelector("pre") : null;
        copyText(pre ? pre.textContent : "", cb);
        return;
      }
      const cn = closest(e, "[data-copyname]");
      if (cn) { copyText(cn.dataset.copyname, cn); return; }
      const cs = closest(e, "[data-copyschema]");
      if (cs) {
        const t = TC.rows.find((x) => x.name === cs.dataset.copyschema);
        copyText(t ? JSON.stringify(t.schema, null, 2) : "", cs);
      }
    });
    const q = byId("tc-q");
    if (q && q.addEventListener) q.addEventListener("input", () => { TC.query = q.value; renderList(); });
    if (document.addEventListener) {
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
    }
  }

  function mount() {
    const host = byId("tab-tool-center");
    if (!host) return;
    if (!TC.mounted) {
      host.innerHTML = PAGE;
      TC.mounted = true;
      bind();
    }
  }

  async function open() {
    mount();
    if (!M || typeof M.toolGroupOf !== "function") {   // model missing -> say so, don't die
      const box = byId("tc-list");
      if (box) box.innerHTML = `<div class="tc-state">Unable to load tools.<div class="tc-none" style="margin-top:6px">system_map_model.js is required.</div></div>`;
      return;
    }
    await load();
  }

  /* Register into the shared loader registry, exactly like the other core
   * tabs (astra.js showTab() runs it). No plugin system involved. */
  if (window.Astra && window.Astra.loaders) window.Astra.loaders["tool-center"] = open;
  else if (typeof loaders !== "undefined") loaders["tool-center"] = open;

  window.ToolCenter = {
    state: TC, open, mount, load, normalize, visibleRows, selectTool, closeDrawer, renderAll,
  };
})();
