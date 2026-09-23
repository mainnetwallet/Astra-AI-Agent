/* Astra AI Agent — 🔀 Agent Workflow tab.
 *
 * A visual editor, runner and monitor over the EXISTING backend:
 *
 *   canvas node  ->  a real workflow step {id, tool, params, depends_on, if}
 *   Run          ->  POST /api/workflows/{id}/run  ->  WorkflowEngine
 *                    ->  ToolRegistry -> the real Astra tools
 *   live state   ->  the SAME /api/events + /api/events/stream feed the
 *                    Activity Log uses (no second event system)
 *   Schedule     ->  the existing SchedulerManager (/api/schedules), whose
 *                    schedules carry workflow_id (Workflow -> Schedule -> Run)
 *
 * Nothing is simulated: every status shown comes from a persisted run, a
 * persisted event, or the live event stream. There is no fake timer and no
 * mock success path.
 *
 * Pure logic (draft <-> definition, canvas graph, validation, run-status
 * mapping) lives in workflow_model.js so it can be unit-tested under node;
 * this file is the DOM + wiring half.
 */
(function () {
  "use strict";

  const M = window.AstraWorkflowModel;
  if (!M) {
    console.warn("Agent Workflow: workflow_model.js must load before workflow.js");
    return;
  }

  /* ------------------------------------------------------------- state ---- */
  const WF = {
    ready: false,         // options + list loaded at least once
    options: null,        // /api/workflows/options
    tools: [],
    toolMap: {},
    list: [],
    schedules: [],
    draft: null,          // the ONE workflow being edited (null = nothing open)
    selected: null,       // selected canvas node id
    run: null,            // the run currently being displayed
    runs: [],
    events: [],           // execution log rows for WF.run
    live: {},             // stepId -> "running" (in-flight run only)
    busy: false,
    error: "",
    note: "",
    filter: "",
    bottomTab: "log",
    sse: null,
    connectFrom: null,    // node id while dragging a connection
    drag: null,
  };

  const STATUS_TEXT = {
    created: "queued", running: "running", completed: "completed",
    failed: "failed", paused: "paused", cancelled: "cancelled",
  };

  /* ---------------------------------------------------------- utilities --- */
  function el(id) { return document.getElementById(id); }
  function setError(msg) { WF.error = msg || ""; renderStatus(); }
  function setNote(msg) { WF.note = msg || ""; renderStatus(); }

  function truncate(text, n) {
    const s = String(text == null ? "" : text);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function statusClass(status) {
    if (status === "completed" || status === "success") return "ok";
    if (status === "failed" || status === "blocked") return "bad";
    if (status === "running" || status === "created") return "run";
    if (status === "skipped" || status === "paused") return "warn";
    if (status === "cancelled") return "muted";
    return "idle";
  }

  /* ------------------------------------------------------------ fetching -- */
  async function refreshOptions() {
    const r = await api("/api/workflows/options");
    if (r && r.ok) {
      WF.options = r.data || {};
      WF.tools = WF.options.tools || [];
      WF.toolMap = M.toolMapFrom(WF.tools);
    }
    return WF.options;
  }

  async function refreshList() {
    const r = await api("/api/workflows?stats=1");
    WF.list = (r && r.ok ? r.data : []) || [];
    return WF.list;
  }

  async function refreshSchedules() {
    const r = await api("/api/schedules");
    WF.schedules = (r && r.ok ? r.data : []) || [];
    return WF.schedules;
  }

  function schedulesFor(id) {
    return WF.schedules.filter((s) => Number(s.workflow_id) === Number(id));
  }

  /* -------------------------------------------------------- open / create - */
  /* A brand-new workflow is a brand-new draft object with its own id-less
   * identity. It never inherits the selected workflow's steps, layout, runs
   * or events — that inheritance is exactly what made "New Workflow" look
   * like it was editing the previous one. */
  function newWorkflow() {
    WF.draft = M.blankDraft();
    WF.draft.name = M.uniqueName(WF.list.map((w) => w.name), "Untitled Workflow");
    WF.draft.mode = "new";
    WF.draft.id = null;
    WF.selected = null;
    WF.run = null;
    WF.runs = [];
    WF.events = [];
    WF.live = {};
    WF.error = "";
    WF.note = "Unsaved workflow — press Save to create it.";
    WF.bottomTab = "log";
    renderAll();
    focusName();
  }

  async function selectWorkflow(id, opts) {
    const keepTab = (opts && opts.keepTab) || false;
    const def = await api(`/api/workflows/${encodeURIComponent(id)}`);
    if (!def || !def.ok) {
      setError((def && def.error) || "workflow not found");
      await refreshList();
      renderList();
      return false;
    }
    // A fresh deep copy every time: editing this draft can never mutate the
    // JSON the API handed back, nor any previously opened draft.
    WF.draft = M.draftFromDefinition(def.data);
    WF.selected = null;
    WF.events = [];
    WF.live = {};
    WF.error = "";
    WF.note = "";
    if (!keepTab) WF.bottomTab = "log";
    const runs = await api(`/api/workflows/${encodeURIComponent(id)}/runs?limit=50`);
    WF.runs = (runs && runs.ok ? runs.data : []) || [];
    WF.run = WF.runs[0] || null;
    if (WF.run) {
      await loadRunLog(WF.run.id);
      setNote(describeRun(WF.run));
    }
    rememberSelected(WF.draft.id);
    renderAll();
    return true;
  }

  function rememberSelected(id) {
    try {
      if (id == null) localStorage.removeItem("astra:workflow:selected");
      else localStorage.setItem("astra:workflow:selected", String(id));
    } catch (_) { /* private mode */ }
  }

  /* ------------------------------------------------------------- saving --- */
  async function saveWorkflow() {
    if (!WF.draft || WF.busy) return false;
    const check = M.validateDraft(WF.draft, WF.toolMap);
    if (!check.ok) {
      setError(check.errors[0]);
      return false;
    }
    const payload = M.definitionFromDraft(WF.draft);
    WF.busy = true;
    setError("");
    setNote(WF.draft.mode === "new" ? "Creating…" : "Saving…");
    renderStatus();

    // Create vs update is decided by the DRAFT, never by "does the name
    // exist": mode === "new" is the only path that POSTs.
    const isCreate = WF.draft.mode === "new" || WF.draft.id == null;
    const r = isCreate
      ? await post("/api/workflows", payload)
      : await patch(`/api/workflows/${encodeURIComponent(WF.draft.id)}`, payload);
    WF.busy = false;

    if (!r || !r.ok) {
      setError((r && r.error) || "save failed");
      setNote("");
      return false;
    }
    // The id, name and layout of the SAVED workflow come from the response —
    // so a uniquified name ("Untitled Workflow 2") is what the editor shows,
    // and the draft switches to update-mode against that exact id.
    const saved = r.data || {};
    await refreshList();
    const loaded = await api(`/api/workflows/${encodeURIComponent(saved.id)}`);
    if (loaded && loaded.ok) {
      WF.draft = M.draftFromDefinition(loaded.data);
      rememberSelected(WF.draft.id);
      const runs = await api(`/api/workflows/${saved.id}/runs?limit=50`);
      WF.runs = (runs && runs.ok ? runs.data : []) || [];
      WF.run = WF.runs[0] || WF.run;
    }
    setNote(isCreate ? `Created "${WF.draft.name}"` : "Saved");
    renderAll();
    return true;
  }

  async function renameWorkflow(name) {
    if (!WF.draft || WF.draft.mode === "new") { renderToolbar(); return; }
    const r = await patch(`/api/workflows/${encodeURIComponent(WF.draft.id)}`,
                          { name });
    if (!r || !r.ok) { setError((r && r.error) || "rename failed"); renderAll(); return; }
    WF.draft.name = r.data.name;
    WF.draft.dirty = false;
    await refreshList();
    setNote(`Renamed to "${r.data.name}"`);
    renderAll();
  }

  async function deleteWorkflow() {
    if (!WF.draft || WF.draft.id == null) { newWorkflow(); return; }
    if (!window.confirm(`Delete workflow "${WF.draft.name}"? Its run history goes too.`)) return;
    const r = await del(`/api/workflows/${encodeURIComponent(WF.draft.id)}`);
    if (!r || !r.ok) { setError((r && r.error) || "delete failed"); return; }
    const gone = WF.draft.id;
    WF.draft = null;
    WF.run = null;
    WF.runs = [];
    WF.events = [];
    WF.selected = null;
    rememberSelected(null);
    await refreshList();
    const next = WF.list[0];
    if (next) await selectWorkflow(next.id);
    else newWorkflow();
    setNote(`Deleted workflow ${gone}`);
    renderAll();
  }

  /* ------------------------------------------------------------ running --- */
  async function runWorkflow() {
    if (WF.busy) return;
    if (!WF.draft) newWorkflow();
    if (WF.draft.mode === "new" || WF.draft.id == null) {
      const saved = await saveWorkflow();
      if (!saved) return;
    }
    let params = {};
    const raw = el("wf-run-params");
    if (raw && raw.value.trim()) {
      try {
        params = JSON.parse(raw.value);
        if (!params || typeof params !== "object" || Array.isArray(params)) {
          throw new Error("params must be a JSON object");
        }
      } catch (e) {
        setError("Run params: " + e.message);
        return;
      }
    }
    WF.busy = true;
    WF.error = "";
    WF.live = {};
    WF.events = [];
    WF.bottomTab = "log";
    setNote("Running…");
    renderAll();

    // The engine emits its progress on the shared event bus while this
    // request is in flight, so the canvas updates live (see onEvent).
    const r = await post(`/api/workflows/${encodeURIComponent(WF.draft.id)}/run`,
                         { params });
    WF.busy = false;
    if (!r || !r.ok) {
      WF.live = {};
      WF.error = (r && r.error) || "run failed to start";
      setNote("");
      renderAll();
      return;
    }
    WF.run = r.data;
    WF.live = {};
    await loadRunLog(WF.run.id);       // authoritative log from stored events
    const runs = await api(`/api/workflows/${WF.draft.id}/runs?limit=50`);
    WF.runs = (runs && runs.ok ? runs.data : WF.runs) || [];
    await refreshList();
    setNote(describeRun(WF.run));
    renderAll();
  }

  function describeRun(run) {
    if (!run) return "";
    const s = M.runSummary(run);
    const bits = [`run #${run.id} ${STATUS_TEXT[run.status] || run.status}`];
    if (s && s.duration) bits.push(s.duration);
    if (s && s.failed) bits.push(s.failed + " failed step" + (s.failed > 1 ? "s" : ""));
    return bits.join(" · ");
  }

  async function cancelRun() {
    if (!WF.run || WF.busy) return;
    const r = await post(`/api/workflows/runs/${encodeURIComponent(WF.run.id)}/cancel`, {});
    if (r && r.ok) {
      WF.run = r.data;
      setNote("Cancellation requested — it stops between steps.");
      renderAll();
    } else {
      setError((r && r.error) || "cancel failed");
    }
  }

  async function selectRun(runId) {
    const r = await api(`/api/workflows/runs/${encodeURIComponent(runId)}`);
    if (!r || !r.ok) { setError((r && r.error) || "run not found"); return; }
    WF.run = r.data;
    WF.live = {};
    await loadRunLog(runId);
    WF.bottomTab = "log";
    renderAll();
  }

  /* The execution log is rebuilt from the persisted event table, so it is
   * correct even if the SSE connection missed frames (reload, reconnect). */
  async function loadRunLog(runId) {
    const r = await api("/api/events?limit=400");
    const rows = (r && r.ok ? r.data : []) || [];
    WF.events = rows.filter((e) => M.isRunEvent(e, runId))
                    .map(M.eventRow)
                    .sort((a, b) => a.id - b.id);
  }

  /* ---------------------------------------------------- live event stream - */
  /* The SAME /api/events/stream the Activity Log tails — filtered to the run
   * on screen. Reusing the existing bus is deliberate: there is no second
   * event system to keep in sync. */
  function openStream() {
    if (WF.sse || !window.EventSource) return;
    try {
      WF.sse = new EventSource("/api/events/stream");
    } catch (_) {
      WF.sse = null;
      return;
    }
    WF.sse.onmessage = (msg) => {
      let ev = null;
      try { ev = JSON.parse(msg.data); } catch (_) { return; }
      onEvent(ev);
    };
    WF.sse.onerror = () => { /* EventSource reconnects on its own */ };
  }

  function onEvent(ev) {
    const d = (ev && ev.data) || {};
    const kind = String((ev && ev.kind) || "");
    const liveRun = WF.run ? Number(WF.run.id) : null;
    const matches = M.isRunEvent(ev, liveRun);

    if (matches) {
      if (kind === "task.started" && d.step) {
        WF.live[d.step] = "running";
        if (WF.run) WF.run.current_step = d.step;
        paintCanvas();
        renderStatus();
      } else if (kind === "task.completed" || kind === "task.failed") {
        if (d.step) delete WF.live[d.step];
        paintCanvas();
      }
      const row = M.eventRow(ev);
      if (!WF.events.some((e) => e.id === row.id)) {
        WF.events.push(row);
        WF.events.sort((a, b) => a.id - b.id);
        renderBottom();
      }
    }
    // A run finishing anywhere (this tab, the scheduler, another client)
    // invalidates what we are showing — refresh from the database.
    if (kind === "workflow.completed" || kind === "workflow.failed") {
      if (matches || !WF.run) {
        refreshAfterRun(kind, d);
      } else if (WF.draft && d.workflow === WF.draft.name) {
        refreshList().then(renderList);
      }
    }
  }

  let _refreshTimer = null;
  function refreshAfterRun(kind, data) {
    if (!WF.draft || WF.draft.id == null) return;
    const runId = data && data.run_id != null ? data.run_id : (WF.run && WF.run.id);
    if (runId == null) return;
    if (_refreshTimer) clearTimeout(_refreshTimer);
    _refreshTimer = setTimeout(async () => {
      const r = await api(`/api/workflows/runs/${encodeURIComponent(runId)}`);
      if (r && r.ok && (!WF.run || Number(r.data.id) === Number(WF.run.id))) {
        WF.run = r.data;
        await loadRunLog(runId);
        setNote(describeRun(WF.run));
      }
      const runs = await api(`/api/workflows/${WF.draft.id}/runs?limit=50`);
      WF.runs = (runs && runs.ok ? runs.data : WF.runs) || [];
      await refreshList();
      renderAll();
    }, 120);
  }

  /* -------------------------------------------------------------- render -- */
  function renderAll() {
    renderList();
    renderToolbar();
    renderCanvas();
    renderInspector();
    renderBottom();
    renderStatus();
  }

  function renderStatus() {
    const box = el("wf-status");
    if (!box) return;
    const parts = [];
    if (WF.error) parts.push(`<span class="wf-err">⚠ ${esc(WF.error)}</span>`);
    if (WF.note) parts.push(`<span class="wf-note">${esc(WF.note)}</span>`);
    box.innerHTML = parts.join(" ");
  }

  function renderList() {
    const box = el("wf-list");
    if (!box) return;
    const needle = WF.filter.trim().toLowerCase();
    const items = WF.list.filter((w) => !needle ||
      String(w.name || "").toLowerCase().includes(needle));
    if (!items.length) {
      box.innerHTML = `<div class="empty">${WF.list.length
        ? "No workflow matches that search."
        : "No workflows yet — press ＋ New."}</div>`;
      return;
    }
    const current = WF.draft && WF.draft.id;
    box.innerHTML = items.map((w) => {
      const last = w.last_run || null;
      const st = last ? STATUS_TEXT[last.status] || last.status : "never run";
      const cls = statusClass(last ? last.status : "idle");
      const active = current != null && Number(current) === Number(w.id);
      return `<button type="button" class="wf-item${active ? " active" : ""}"
                data-wf="${w.id}" title="${esc(w.name)}">
          <span class="wf-item-dot ${cls}"></span>
          <span class="wf-item-text">
            <span class="wf-item-name">${esc(truncate(w.name, 32))}</span>
            <span class="wf-item-meta">${esc(String(st))}${
              w.run_count ? " · " + w.run_count + " run" + (w.run_count > 1 ? "s" : "") : ""}</span>
          </span>
        </button>`;
    }).join("");
  }

  function renderToolbar() {
    const name = el("wf-name");
    if (name && document.activeElement !== name) name.value = (WF.draft && WF.draft.name) || "";
    const badge = el("wf-badge");
    if (badge) {
      if (!WF.draft) badge.textContent = "no workflow";
      else if (WF.draft.mode === "new") badge.textContent = "new (unsaved)";
      else badge.textContent = "workflow #" + WF.draft.id;
      badge.className = "pill" + (WF.draft && WF.draft.mode === "new" ? " warn" : "");
    }
    const save = el("wf-save"), run = el("wf-run"), del = el("wf-delete"),
          cancel = el("wf-cancel");
    if (save) save.disabled = !WF.draft || WF.busy;
    if (run) run.disabled = !WF.draft || WF.busy;
    if (del) del.disabled = !WF.draft || WF.draft.id == null;
    if (cancel) cancel.hidden = !(WF.busy && WF.run);
  }

  /* ---- canvas ---- */
  function canvasNodes() {
    if (!WF.draft) return [];
    const list = M.canvasNodes(WF.draft, WF.toolMap);
    const scheds = WF.draft.id != null ? schedulesFor(WF.draft.id) : [];
    const trigger = list.filter((n) => n.kind === "trigger")[0];
    if (trigger) {
      trigger.subtitle = scheds.length
        ? "Schedule: " + scheds.map((s) => s.kind + " " + s.value).join(", ")
        : "Manual run";
    }
    return M.withPositions(list, WF.draft.layout || {});
  }

  function nodeStatusOf(node) {
    if (node.kind === "trigger") return WF.busy ? "running" : "idle";
    if (node.kind === "condition") {
      const src = WF.run && WF.run.results ? WF.run.results[node.cond.step] : null;
      if (!src) return "idle";
      const ok = !!src.ok;
      const want = node.cond.op === "not_ok" ? !ok : ok;
      return want ? "success" : "skipped";
    }
    const live = WF.live[node.id];
    if (live === "running") return "running";
    if (WF.run) return M.nodeStatus(WF.run, node.id);
    return "idle";
  }

  function paintCanvas() {
    const nodes = canvasNodes();
    const byId = {};
    nodes.forEach((n) => { byId[n.id] = n; });
    document.querySelectorAll("#wf-nodes .wf-node").forEach((dom) => {
      const node = byId[dom.dataset.nodeId];
      if (!node) return;
      const status = nodeStatusOf(node);
      dom.className = nodeClass(node, status);
      const dot = dom.querySelector(".wf-node-status");
      if (dot) { dot.className = "wf-node-status " + statusClass(status); dot.title = status; }
    });
    drawEdges(nodes);
  }

  function nodeClass(node, status) {
    return "wf-node kind-" + node.kind + " status-" + status +
           (WF.selected === node.id ? " selected" : "");
  }

  function renderCanvas() {
    const wrap = el("wf-canvas");
    const holder = el("wf-nodes");
    if (!wrap || !holder) return;
    if (!WF.draft) {
      holder.innerHTML = `<div class="empty wf-canvas-empty">Select or create a workflow.</div>`;
      const svg = el("wf-edges");
      if (svg) svg.innerHTML = "";
      return;
    }
    const nodes = canvasNodes();
    let maxX = 320, maxY = 240;
    nodes.forEach((n) => {
      maxX = Math.max(maxX, n.x + n.w + 40);
      maxY = Math.max(maxY, n.y + n.h + 40);
    });
    wrap.style.width = maxX + "px";
    wrap.style.height = maxY + "px";
    holder.innerHTML = nodes.map(renderNode).join("");
    paintCanvas();
  }

  function renderNode(n) {
    const status = nodeStatusOf(n);
    const tags = [];
    if (n.kind === "tool" || n.kind === "ai") {
      tags.push(`<span class="wf-tag">${esc(n.toolLabel || "")}</span>`);
      if (n.category) tags.push(`<span class="wf-tag alt">${esc(M.categoryLabel(n.category))}</span>`);
      if (n.deps && n.deps.length) tags.push(`<span class="wf-tag">↳ ${esc(n.deps.join(","))}</span>`);
      if (n.cond) {
        tags.push(`<span class="wf-tag cond">⚑ ${esc(n.cond.step)} ${
          n.cond.op === "not_ok" ? "not ok" : "ok"}</span>`);
      }
    }
    if (n.kind === "condition") {
      tags.push(`<span class="wf-tag">gates ${esc(String(n.gates.length))}</span>`);
    }
    const ports = n.kind === "trigger"
      ? `<span class="wf-port out" data-port="out"></span>`
      : `<span class="wf-port in" data-port="in"></span>
         <span class="wf-port out" data-port="out"></span>`;
    return `<div class="${nodeClass(n, status)}" data-node-id="${esc(n.id)}"
                 style="left:${n.x}px;top:${n.y}px;width:${n.w}px">
        <span class="wf-node-bar"></span>
        <div class="wf-node-main">
          <span class="wf-node-icon">${n.icon || "🔧"}</span>
          <div class="wf-node-text">
            <div class="wf-node-title">${esc(truncate(n.name, 26))}</div>
            <div class="wf-node-sub">${esc(truncate(n.subtitle || "", 34))}</div>
          </div>
          <span class="wf-node-status ${statusClass(status)}" title="${esc(status)}"></span>
        </div>
        ${tags.length ? `<div class="wf-node-tags">${tags.join("")}</div>` : ""}
        ${ports}
      </div>`;
  }

  function drawEdges(nodes) {
    const svg = el("wf-edges");
    if (!svg) return;
    const byId = {};
    nodes.forEach((n) => { byId[n.id] = n; });
    const edges = M.canvasEdges(WF.draft, WF.toolMap);
    const w = parseFloat(el("wf-canvas").style.width) || 0;
    const h = parseFloat(el("wf-canvas").style.height) || 0;
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.innerHTML = edges.map((e) => {
      const a = byId[e.from], b = byId[e.to];
      if (!a || !b) return "";
      return `<path class="wf-edge ${e.kind}" d="${M.edgePath(a, b)}"></path>`;
    }).join("");
  }

  /* ---- node library ---- */
  function renderPalette() {
    const box = el("wf-palette-list");
    if (!box) return;
    const cats = (WF.options && WF.options.categories) || {};
    const names = Object.keys(cats).sort((a, b) => {
      if (a === "ai") return -1;
      if (b === "ai") return 1;
      return M.categoryLabel(a).localeCompare(M.categoryLabel(b));
    });
    const groups = names.map((cat) => {
      const tools = (cats[cat] || []).slice().sort();
      return `<div class="wf-pal-group">
          <div class="wf-pal-title">${M.categoryIcon(cat)} ${esc(M.categoryLabel(cat))}</div>
          ${tools.map((t) => {
            const tool = WF.toolMap[t] || {};
            return `<button type="button" class="wf-pal-item" data-add-tool="${esc(t)}"
                       title="${esc(tool.description || t)}">
                <span class="wf-pal-name">${esc(t)}</span>
                <span class="wf-pal-risk ${esc(tool.risk_level || "read")}">${esc(tool.risk_level || "")}</span>
              </button>`;
          }).join("")}
        </div>`;
    }).join("");
    box.innerHTML = (groups || `<div class="empty">No tools registered.</div>`) +
      `<div class="wf-pal-group">
         <div class="wf-pal-title">⚑ Condition</div>
         <button type="button" class="wf-pal-item" data-add-cond="1">
           <span class="wf-pal-name">Gate steps on a step's result</span>
         </button>
       </div>`;
  }

  /* ---- inspector ---- */
  function renderInspector() {
    const body = el("wf-inspector-body");
    const title = el("wf-inspector-title");
    if (!body) return;
    if (!WF.draft) {
      if (title) title.textContent = "Inspector";
      body.innerHTML = `<div class="empty">Nothing selected.</div>`;
      return;
    }
    if (!WF.selected) {
      if (title) title.textContent = "Workflow";
      body.innerHTML = workflowInspector();
      wireInspector();
      return;
    }
    const nodes = canvasNodes();
    const node = nodes.filter((n) => n.id === WF.selected)[0];
    if (!node) {
      WF.selected = null;
      renderInspector();
      return;
    }
    if (title) title.textContent = node.kind === "trigger" ? "Trigger"
      : node.kind === "condition" ? "Condition" : "Node " + node.id;
    body.innerHTML = node.kind === "trigger" ? triggerInspector()
      : node.kind === "condition" ? conditionInspector(node)
      : stepInspector(node);
    wireInspector();
  }

  function workflowInspector() {
    const d = WF.draft;
    const scheds = d.id != null ? schedulesFor(d.id) : [];
    return `<div class="wf-insp">
      <div class="wf-field">
        <label>Name</label>
        <input id="wf-ins-name" value="${esc(d.name)}" placeholder="Workflow name">
      </div>
      <div class="wf-field">
        <label>Description</label>
        <textarea id="wf-ins-desc" rows="2" placeholder="What does it do?">${esc(d.description)}</textarea>
      </div>
      <div class="wf-hint">${d.steps.length} node${d.steps.length === 1 ? "" : "s"} ·
        ${esc(d.mode === "new" ? "unsaved" : "saved as #" + d.id)}</div>
      ${d.id != null ? `<div class="wf-field">
        <label>Schedule (Workflow → Schedule → Run)</label>
        <div class="wf-sched-list">${scheds.length ? scheds.map((s) => `
          <div class="wf-sched">
            <span class="wf-tag">${esc(s.kind)} ${esc(s.value)}</span>
            <span class="muted small">next ${esc(s.next_run || "—")}</span>
            <button class="btn mini" data-sched-toggle="${s.id}" data-enabled="${s.enabled ? 1 : 0}">
              ${s.enabled ? "Disable" : "Enable"}</button>
            <button class="btn mini danger" data-sched-del="${s.id}">✕</button>
          </div>`).join("") : `<div class="muted small">Not scheduled — runs only when you press ▶ Run.</div>`}
        </div>
      </div>` : `<div class="wf-hint">Save the workflow to attach a schedule.</div>`}
    </div>`;
  }

  function triggerInspector() {
    const d = WF.draft;
    const scheds = d.id != null ? schedulesFor(d.id) : [];
    const kinds = (WF.options && WF.options.schedule_kinds) || ["oneshot", "interval", "daily", "weekly", "deadline"];
    const schedulerOn = !!(WF.options && WF.options.scheduler);
    if (d.id == null) {
      return `<div class="wf-insp"><div class="wf-hint">
        The trigger is manual until the workflow is saved. Save it first, then
        attach a real schedule (the existing SchedulerManager fires it exactly
        like any other Astra schedule).</div></div>`;
    }
    return `<div class="wf-insp">
      <div class="wf-hint">A schedule runs THIS workflow through the existing
        SchedulerManager — Workflow → Schedule → Run.</div>
      ${schedulerOn ? "" : `<div class="wf-warn">The scheduler is disabled on this
        server (start it with ASTRA_SCHEDULER=1).</div>`}
      <div class="wf-field">
        <label>Add schedule</label>
        <select id="wf-sched-kind">${kinds.map((k) =>
          `<option value="${esc(k)}">${esc(k)}</option>`).join("")}</select>
      </div>
      <div class="wf-field">
        <label>Value</label>
        <input id="wf-sched-value" placeholder="09:00 · 300 · Mon 09:00 · 2026-10-01 09:00">
        <div class="wf-hint">daily=HH:MM · weekly="Mon 09:00" · interval=seconds ·
          oneshot="YYYY-MM-DD HH:MM" · deadline=days</div>
      </div>
      <button class="btn primary mini" id="wf-sched-add">＋ Add schedule</button>
      <div class="wf-sched-list">${scheds.length ? scheds.map((s) => `
        <div class="wf-sched">
          <span class="wf-tag">${esc(s.kind)} ${esc(s.value)}</span>
          <span class="muted small">next ${esc(s.next_run || "—")}</span>
          <button class="btn mini" data-sched-toggle="${s.id}" data-enabled="${s.enabled ? 1 : 0}">
            ${s.enabled ? "Disable" : "Enable"}</button>
          <button class="btn mini danger" data-sched-del="${s.id}">✕</button>
        </div>`).join("") : `<div class="muted small">No schedules attached.</div>`}
      </div>
    </div>`;
  }

  function conditionInspector(node) {
    const steps = (WF.draft.steps || []).filter((s) => s.id !== node.cond.step);
    return `<div class="wf-insp">
      <div class="wf-hint">Complies to Astra's real step condition
        (<code>if: {step, op}</code>) on every gated step. No second
        branching engine.</div>
      <div class="wf-field">
        <label>Condition on step</label>
        <select id="wf-cond-step">${(WF.draft.steps || []).map((s) =>
          `<option value="${esc(s.id)}"${s.id === node.cond.step ? " selected" : ""}>${
            esc(s.id + (s.name ? " · " + s.name : ""))}</option>`).join("")}</select>
      </div>
      <div class="wf-field">
        <label>When it</label>
        <select id="wf-cond-op">
          <option value="ok"${node.cond.op === "ok" ? " selected" : ""}>succeeded</option>
          <option value="not_ok"${node.cond.op === "not_ok" ? " selected" : ""}>did not succeed</option>
        </select>
      </div>
      <div class="wf-field">
        <label>Gate these steps</label>
        <div class="wf-hint">Unticking every step removes the condition
          (Astra stores it as the <code>if</code> on each gated step).</div>
        <div class="wf-checks">${steps.map((s) => `
          <label class="check"><input type="checkbox" data-cond-gate="${esc(s.id)}"
            ${node.gates.indexOf(s.id) >= 0 ? "checked" : ""}>
            <span>${esc(s.id)}${s.name ? " · " + esc(s.name) : ""}</span></label>`).join("")
          || `<span class="muted small">Add another node first.</span>`}</div>
      </div>
      <button class="btn mini danger" id="wf-cond-del">Remove condition</button>
    </div>`;
  }

  function stepInspector(node) {
    const step = M.findStep(WF.draft, node.id);
    if (!step) return `<div class="empty">Node gone.</div>`;
    const tool = WF.toolMap[step.tool] || {};
    const fields = M.toolFields(tool);
    const isAI = node.kind === "ai";
    const others = (WF.draft.steps || []).filter((s) => s.id !== step.id);
    const fieldHtml = fields.map((f) => {
      const val = step.params ? step.params[f.name] : "";
      const label = `${esc(f.name)}${f.required ? ' <span class="req">required</span>' : ""}`;
      if (f.type === "bool") {
        return `<div class="wf-field"><label class="check">
            <input type="checkbox" data-param="${esc(f.name)}" data-ptype="bool"
              ${val === true ? "checked" : ""}><span>${label}</span></label>
          ${f.description ? `<div class="wf-hint">${esc(f.description)}</div>` : ""}</div>`;
      }
      if (f.type === "int" || f.type === "number" || f.type === "float") {
        return `<div class="wf-field"><label>${label}</label>
          <input type="number" data-param="${esc(f.name)}" data-ptype="number"
            value="${esc(val === undefined || val === null ? "" : val)}">
          ${f.description ? `<div class="wf-hint">${esc(f.description)}</div>` : ""}</div>`;
      }
      if (f.type === "list" || f.type === "dict") {
        return `<div class="wf-field"><label>${label}</label>
          <textarea rows="2" data-param="${esc(f.name)}" data-ptype="json"
            placeholder='${f.type === "list" ? "[...]" : "{...}"}'>${esc(
              typeof val === "string" ? val : JSON.stringify(val === undefined ? "" : val))}</textarea>
          ${f.description ? `<div class="wf-hint">${esc(f.description)}</div>` : ""}</div>`;
      }
      const big = /prompt|content|command|body|text/i.test(f.name);
      return `<div class="wf-field"><label>${label}</label>
        ${big
          ? `<textarea rows="4" data-param="${esc(f.name)}" data-ptype="string"
               placeholder="Supports {{step_id.output}}">${esc(val === undefined || val === null ? "" : val)}</textarea>`
          : `<input data-param="${esc(f.name)}" data-ptype="string"
               value="${esc(val === undefined || val === null ? "" : val)}">`}
        ${f.description ? `<div class="wf-hint">${esc(f.description)}</div>` : ""}</div>`;
    }).join("");

    const aiExtras = isAI ? aiPicker(step) : "";
    const paramsJson = `<div class="wf-field">
        <label>Raw params (JSON)</label>
        <textarea rows="3" id="wf-node-params">${esc(JSON.stringify(step.params || {}, null, 1))}</textarea>
        <div class="wf-hint">Applied on blur. <code>{{step_id.output}}</code> is resolved by
          the engine from earlier step results.</div>
      </div>`;

    return `<div class="wf-insp">
      <div class="wf-field"><label>Node id</label>
        <input id="wf-node-id" value="${esc(step.id)}"></div>
      <div class="wf-field"><label>Name (label only)</label>
        <input id="wf-node-name" value="${esc(step.name || "")}" placeholder="optional label"></div>
      <div class="wf-field"><label>Tool</label>
        <select id="wf-node-tool">${Object.keys(WF.toolMap).sort().map((t) =>
          `<option value="${esc(t)}"${t === step.tool ? " selected" : ""}>${
            esc(t)} — ${esc(M.categoryLabel((WF.toolMap[t] || {}).category))}</option>`).join("")}
        </select>
        ${tool.description ? `<div class="wf-hint">${esc(tool.description)}</div>` : ""}
        <div class="wf-hint">risk: <b>${esc(tool.risk_level || "read")}</b>${
          tool.requires_confirmation ? " · needs confirmation (a workflow run is unattended, so it will block)" : ""}</div>
      </div>
      ${aiExtras}
      ${fieldHtml}
      <div class="wf-field"><label>Depends on</label>
        <div class="wf-checks">${others.map((s) => `
          <label class="check"><input type="checkbox" data-dep="${esc(s.id)}"
            ${(step.depends_on || []).indexOf(s.id) >= 0 ? "checked" : ""}>
            <span>${esc(s.id)}${s.name ? " · " + esc(s.name) : ""}</span></label>`).join("")
          || `<span class="muted small">First node — nothing to depend on.</span>`}</div>
      </div>
      <label class="check wf-cond-toggle"><input type="checkbox" id="wf-node-if"
        ${step.if ? "checked" : ""}><span>Run only if another step succeeded</span></label>
      ${step.if ? `<div class="wf-field">
        <select id="wf-node-if-step">${(WF.draft.steps || []).filter((o) => o.id !== step.id).map((o) =>
          `<option value="${esc(o.id)}"${step.if.step === o.id ? " selected" : ""}>${
            esc(o.id)}${o.name ? " · " + esc(o.name) : ""}</option>`).join("")}</select>
        <select id="wf-node-if-op">
          <option value="ok"${(step.if.op || "ok") === "ok" ? " selected" : ""}>succeeded</option>
          <option value="not_ok"${step.if.op === "not_ok" ? " selected" : ""}>did not succeed</option>
        </select></div>` : ""}
      ${paramsJson}
      <div class="wf-hint">Last result: <span class="mono">${esc(lastResultText(step.id))}</span></div>
      <button class="btn mini danger" id="wf-node-del">Delete node</button>
    </div>`;
  }

  function lastResultText(stepId) {
    if (!WF.run || !WF.run.results) return "no run yet";
    const res = WF.run.results[stepId];
    if (!res) return "not part of run #" + WF.run.id;
    if (res.skipped) return "skipped (" + (res.reason || "condition false") + ")";
    if (res.blocked) return "blocked (" + (res.reason || "") + ")";
    if (res.error) return "error: " + truncate(res.error, 120);
    return truncate(JSON.stringify(res.output === undefined ? res : res.output), 200);
  }

  function aiPicker(step) {
    const providers = (WF.options && WF.options.providers) || [];
    const current = step.params || {};
    const models = providers.filter((p) => p.name === current.provider)[0];
    return `<div class="wf-ai-note">Provider/model are honoured by the AstraRouter;
      giving both pins the exact target (the step then fails honestly instead of
      quietly answering from another model).</div>
      <div class="wf-field"><label>Preferred provider</label>
        <select id="wf-ai-provider">
          <option value="">auto (router decides)</option>
          ${providers.map((p) => `<option value="${esc(p.name)}"${
            current.provider === p.name ? " selected" : ""}>${esc(p.name)}${
            p.healthy === false ? " (unhealthy)" : ""}</option>`).join("")}
        </select></div>
      <div class="wf-field"><label>Preferred model</label>
        <select id="wf-ai-model">
          <option value="">auto</option>
          ${((models && models.models) || []).map((m) =>
            `<option value="${esc(m.id)}"${current.model === m.id ? " selected" : ""}>${esc(m.id)}</option>`).join("")}
        </select></div>`;
  }

  function wireInspector() {
    const name = el("wf-ins-name");
    if (name) name.addEventListener("change", () => {
      WF.draft.name = name.value;
      WF.draft.dirty = true;
      renderToolbar();
      renderList();
    });
    const desc = el("wf-ins-desc");
    if (desc) desc.addEventListener("change", () => {
      WF.draft.description = desc.value;
      WF.draft.dirty = true;
    });
    const nodeName = el("wf-node-name");
    if (nodeName) nodeName.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (!s) return;
      s.name = nodeName.value;
      WF.draft.dirty = true;
      renderCanvas();
    });
    const tool = el("wf-node-tool");
    if (tool) tool.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (!s) return;
      s.tool = tool.value;
      s.params = {};
      WF.draft.dirty = true;
      renderAll();
    });
    const nodeId = el("wf-node-id");
    if (nodeId) nodeId.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      const want = nodeId.value.trim();
      if (!s || !want || M.findStep(WF.draft, want)) { renderInspector(); return; }
      const old = s.id;
      s.id = want;
      (WF.draft.steps || []).forEach((o) => {
        o.depends_on = (o.depends_on || []).map((d) => (d === old ? want : d));
        if (o.if && o.if.step === old) o.if.step = want;
      });
      if (WF.draft.layout && WF.draft.layout[old]) {
        WF.draft.layout[want] = WF.draft.layout[old];
        delete WF.draft.layout[old];
      }
      WF.selected = want;
      WF.draft.dirty = true;
      renderAll();
    });
    document.querySelectorAll("#wf-inspector-body [data-param]").forEach((input) => {
      const ev = (input.tagName === "SELECT" || input.type === "checkbox" ||
                  input.type === "number") ? "change" : "blur";
      input.addEventListener(ev, () => applyParam(input));
    });
    document.querySelectorAll("#wf-inspector-body [data-dep]").forEach((box) => {
      box.addEventListener("change", () => {
        const step = M.findStep(WF.draft, WF.selected);
        if (!step) return;
        const deps = Array.from(
          document.querySelectorAll("#wf-inspector-body [data-dep]"))
          .filter((b) => b.checked).map((b) => b.dataset.dep);
        if (!M.setDependsOn(WF.draft, step.id, deps)) {
          box.checked = false;
          setError("That would create a dependency cycle.");
        } else {
          setError("");
        }
        renderCanvas();
      });
    });
    const paramsBox = el("wf-node-params");
    if (paramsBox) paramsBox.addEventListener("blur", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (!s) return;
      try {
        const parsed = JSON.parse(paramsBox.value || "{}");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("params must be a JSON object");
        }
        s.params = parsed;
        WF.draft.dirty = true;
        setError("");
        renderInspector();
      } catch (e) {
        setError("Params: " + e.message);
      }
    });
    const ifBox = el("wf-node-if");
    if (ifBox) ifBox.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (!s) return;
      if (ifBox.checked) {
        const target = (WF.draft.steps || []).filter((o) => o.id !== s.id)[0];
        if (!target) { ifBox.checked = false; setError("Add another step first."); return; }
        s.if = { step: target.id, op: "ok" };
      } else {
        s.if = null;
      }
      WF.draft.dirty = true;
      renderAll();
    });
    const ifStep = el("wf-node-if-step");
    if (ifStep) ifStep.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (s && s.if) { s.if.step = ifStep.value; WF.draft.dirty = true; renderCanvas(); }
    });
    const ifOp = el("wf-node-if-op");
    if (ifOp) ifOp.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (s && s.if) { s.if.op = ifOp.value; WF.draft.dirty = true; renderCanvas(); }
    });
    const delBtn = el("wf-node-del");
    if (delBtn) delBtn.addEventListener("click", () => {
      M.removeStep(WF.draft, WF.selected);
      WF.selected = null;
      WF.draft.dirty = true;
      renderAll();
    });
    const provider = el("wf-ai-provider");
    if (provider) provider.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (!s) return;
      s.params = s.params || {};
      s.params.provider = provider.value;
      WF.draft.dirty = true;
      renderInspector();
    });
    const model = el("wf-ai-model");
    if (model) model.addEventListener("change", () => {
      const s = M.findStep(WF.draft, WF.selected);
      if (!s) return;
      s.params = s.params || {};
      s.params.model = model.value;
      WF.draft.dirty = true;
    });
    const schedAdd = el("wf-sched-add");
    if (schedAdd) schedAdd.addEventListener("click", addSchedule);
    const condDel = el("wf-cond-del");
    if (condDel) condDel.addEventListener("click", () => {
      M.clearCondition(WF.draft, WF.selected);
      WF.selected = null;
      WF.draft.dirty = true;
      renderAll();
    });
    const condStep = el("wf-cond-step");
    if (condStep) condStep.addEventListener("change", () => applyCondition());
    const condOp = el("wf-cond-op");
    if (condOp) condOp.addEventListener("change", () => applyCondition());
    document.querySelectorAll("#wf-inspector-body [data-cond-gate]").forEach((box) => {
      box.addEventListener("change", () => applyCondition());
    });
    document.querySelectorAll("#wf-inspector-body [data-sched-toggle]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await patch(`/api/schedules/${btn.dataset.schedToggle}`,
                    { enabled: btn.dataset.enabled !== "1" });
        await refreshSchedules();
        renderAll();
      });
    });
    document.querySelectorAll("#wf-inspector-body [data-sched-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await del(`/api/schedules/${btn.dataset.schedDel}`);
        await refreshSchedules();
        renderAll();
      });
    });
  }

  function applyCondition() {
    if (!WF.draft) return;
    const step = (el("wf-cond-step") || {}).value;
    const op = (el("wf-cond-op") || {}).value || "ok";
    const gates = Array.from(document.querySelectorAll("#wf-inspector-body [data-cond-gate]"))
      .filter((b) => b.checked).map((b) => b.dataset.condGate);
    const newId = M.upsertCondition(WF.draft, WF.selected, { step, op, gates });
    if (!newId) { setError("Cannot gate a step on itself."); return; }
    WF.selected = newId;
    WF.draft.dirty = true;
    setError("");
    renderAll();
  }

  function applyParam(input) {
    const step = M.findStep(WF.draft, WF.selected);
    if (!step) return;
    step.params = step.params || {};
    const name = input.dataset.param;
    const type = input.dataset.ptype;
    let value;
    if (type === "bool") value = !!input.checked;
    else if (type === "number") {
      value = input.value === "" ? "" : Number(input.value);
      if (input.value !== "" && Number.isNaN(value)) { setError(name + " must be a number"); return; }
    } else if (type === "json") {
      const raw = input.value.trim();
      if (!raw) value = "";
      else {
        try { value = JSON.parse(raw); }
        catch (e) { setError(name + ": " + e.message); return; }
      }
    } else value = input.value;
    step.params[name] = value;
    WF.draft.dirty = true;
    setError("");
    renderCanvas();
  }

  async function addSchedule() {
    if (!WF.draft || WF.draft.id == null) { setError("Save the workflow first."); return; }
    const kind = (el("wf-sched-kind") || {}).value || "daily";
    const value = ((el("wf-sched-value") || {}).value || "").trim();
    if (!value) { setError("A schedule needs a value (e.g. 09:00)."); return; }
    const r = await post("/api/schedules", {
      name: WF.draft.name + " · " + kind,
      kind,
      value,
      workflow_id: WF.draft.id,
    });
    if (!r || !r.ok) { setError((r && r.error) || "could not add the schedule"); return; }
    setError("");
    setNote("Schedule added — the SchedulerManager will fire this workflow.");
    await refreshSchedules();
    renderAll();
  }

  /* ---- bottom panel ---- */
  function renderBottom() {
    const panel = el("wf-panel");
    const tabs = el("wf-tabs");
    if (!panel || !tabs) return;
    tabs.querySelectorAll("[data-wf-tab]").forEach((b) =>
      b.classList.toggle("active", b.dataset.wfTab === WF.bottomTab));
    if (WF.bottomTab === "runs") panel.innerHTML = runsPanel();
    else if (WF.bottomTab === "schedule") panel.innerHTML = schedulePanel();
    else panel.innerHTML = logPanel();
    wireBottom();
  }

  function logPanel() {
    const head = `<div class="wf-log-head">
        <span class="mono small">${WF.run ? "run #" + WF.run.id + " · " +
          esc(STATUS_TEXT[WF.run.status] || WF.run.status) : "no run selected"}</span>
        <span class="grow"></span>
        <input id="wf-run-params" class="wf-params-input" placeholder='run params JSON ({{key}})' >
        <button class="btn mini" id="wf-cancel" ${WF.run && WF.run.status === "running" ? "" : "hidden"}>■ Cancel</button>
      </div>`;
    if (!WF.events.length) {
      return head + `<div class="empty">${
        WF.run ? "No events recorded for this run." :
        "Press ▶ Run to execute the workflow on the real backend."}</div>`;
    }
    return head + `<div class="wf-log">${WF.events.map((e) => `
      <div class="wf-log-row ${statusClass(
        e.kind.indexOf("failed") >= 0 ? "failed" : e.kind.indexOf("completed") >= 0 ? "completed" : "running")}">
        <span class="wf-log-at">${esc((e.at || "").slice(11) || "—")}</span>
        <span class="wf-log-kind">${esc(e.kind)}</span>
        <span class="wf-log-step">${esc(e.step || e.workflow || "")}</span>
        <span class="wf-log-text">${esc(e.tool ? e.tool + " · " : "")}${esc(truncate(e.text, 60))}${
          e.duration_ms !== undefined && e.duration_ms !== null ? ` <span class="muted">${Math.round(e.duration_ms)}ms</span>` : ""}</span>
        ${e.error ? `<span class="wf-log-err">${esc(truncate(e.error, 120))}</span>` : ""}
      </div>`).join("")}</div>`;
  }

  function runsPanel() {
    if (!WF.draft || WF.draft.id == null) {
      return `<div class="empty">Save the workflow to build run history.</div>`;
    }
    if (!WF.runs.length) return `<div class="empty">No runs yet.</div>`;
    return `<div class="wf-runs">${WF.runs.map((r) => {
      const s = M.runSummary(r);
      const active = WF.run && Number(WF.run.id) === Number(r.id);
      const counts = M.runCounts(r);
      return `<button type="button" class="wf-run${active ? " active" : ""}" data-run="${r.id}">
        <span class="wf-item-dot ${statusClass(r.status)}"></span>
        <span class="wf-run-id">#${r.id}</span>
        <span class="wf-run-status">${esc(STATUS_TEXT[r.status] || r.status)}${
          counts.failed ? ` · ${counts.failed} failed` : ""}</span>
        <span class="wf-run-time muted small">${esc(r.started_at || "")}</span>
        <span class="wf-run-dur muted small">${esc(s.duration || "")}</span>
      </button>`;
    }).join("")}</div>`;
  }

  function schedulePanel() {
    if (!WF.draft || WF.draft.id == null) {
      return `<div class="empty">Save the workflow to attach schedules.</div>`;
    }
    const scheds = schedulesFor(WF.draft.id);
    if (!scheds.length) {
      return `<div class="empty">No schedules. Open the Trigger node ⚙ to add one.</div>`;
    }
    return `<div class="wf-runs">${scheds.map((s) => `
      <div class="wf-sched wide">
        <span class="wf-tag">${esc(s.kind)} ${esc(s.value)}</span>
        <span>${esc(s.name)}</span>
        <span class="muted small">last ${esc(s.last_run || "—")} · next ${esc(s.next_run || "—")}</span>
        <span class="grow"></span>
        <button class="btn mini" data-sched-toggle="${s.id}" data-enabled="${s.enabled ? 1 : 0}">
          ${s.enabled ? "Disable" : "Enable"}</button>
        <button class="btn mini danger" data-sched-del="${s.id}">✕</button>
      </div>`).join("")}</div>`;
  }

  function wireBottom() {
    const cancel = el("wf-cancel");
    if (cancel) cancel.addEventListener("click", cancelRun);
  }

  /* ---- canvas interaction ---- */
  function wireCanvas() {
    const wrap = el("wf-canvas-wrap");
    const addBtn = el("wf-add");
    if (addBtn) addBtn.addEventListener("click", () => {
      el("wf-palette").classList.toggle("open");
      renderPalette();
    });
    const closeBtn = el("wf-palette-close");
    if (closeBtn) closeBtn.addEventListener("click", () =>
      el("wf-palette").classList.remove("open"));

    const palette = el("wf-palette-list");
    if (palette) palette.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-add-tool]");
      if (btn) {
        addNodeWithTool(btn.dataset.addTool);
        return;
      }
      if (e.target.closest("[data-add-cond]")) addConditionNode();
    });

    if (wrap) wrap.addEventListener("pointerdown", onCanvasDown);
  }

  function addNodeWithTool(toolName) {
    if (!WF.draft) newWorkflow();
    // Chain by default: the new node follows whatever is selected.
    const prev = WF.selected && M.findStep(WF.draft, WF.selected)
      ? [WF.selected] : [];
    const step = M.addStep(WF.draft, toolName, { dependsOn: prev });
    WF.selected = step.id;
    if (!WF.draft.layout) WF.draft.layout = {};
    WF.draft.layout[step.id] = nextFreeSpot();
    WF.draft.dirty = true;
    el("wf-palette").classList.remove("open");
    renderAll();
  }

  /* A condition is not a free-floating box: it exists only as the `if` on the
   * steps it gates. So the default is a real one — gate the selected step on
   * the step it depends on (or on the previous step), which is exactly what
   * "insert a condition here" means. */
  function addConditionNode() {
    if (!WF.draft) newWorkflow();
    const steps = WF.draft.steps || [];
    if (steps.length < 2) {
      setError("A condition needs two steps — the one it checks and the one it gates.");
      return;
    }
    const target = (WF.selected && M.findStep(WF.draft, WF.selected)) ||
                   steps[steps.length - 1];
    const others = steps.filter((s) => s.id !== target.id);
    const src = (target.depends_on || []).filter((d) => M.findStep(WF.draft, d))[0] ||
                others[others.length - 1].id;
    const newId = M.upsertCondition(WF.draft, null,
                                    { step: src, op: "ok", gates: [target.id] });
    if (!newId) { setError("Cannot gate a step on itself."); return; }
    if (!WF.draft.layout) WF.draft.layout = {};
    WF.draft.layout[newId] = nextFreeSpot();
    WF.selected = newId;
    WF.draft.dirty = true;
    setError("");
    el("wf-palette").classList.remove("open");
    renderAll();
  }

  function nextFreeSpot() {
    const layout = (WF.draft && WF.draft.layout) || {};
    let y = 40, x = 340;
    Object.keys(layout).forEach((k) => {
      const p = layout[k];
      if (p && typeof p.y === "number") y = Math.max(y, p.y + 132);
      if (p && typeof p.x === "number") x = Math.max(x, p.x);
    });
    return { x: Math.min(x, 340), y };
  }

  function nodeAt(clientX, clientY) {
    const doms = document.querySelectorAll("#wf-nodes .wf-node");
    for (let i = 0; i < doms.length; i += 1) {
      const rect = doms[i].getBoundingClientRect();
      if (clientX >= rect.left && clientX <= rect.right &&
          clientY >= rect.top && clientY <= rect.bottom) {
        return doms[i].dataset.nodeId;
      }
    }
    return null;
  }

  function onCanvasDown(e) {
    const nodeDom = e.target.closest(".wf-node");
    const canvas = el("wf-canvas");
    if (!nodeDom || !canvas || !WF.draft) return;
    const nodeId = nodeDom.dataset.nodeId;
    if (nodeId === "__trigger__" && e.target.dataset.port !== "out") {
      selectNode(nodeId);
      return;
    }
    const isOut = e.target.classList && e.target.classList.contains("out");
    const rect = canvas.getBoundingClientRect();

    if (isOut) {
      // drag a connection: from this node to another one
      WF.connectFrom = nodeId;
      const temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
      temp.setAttribute("class", "wf-edge drafting");
      el("wf-edges").appendChild(temp);
      moveHandler(e);
      function moveHandler(ev) {
        const p = canvasNodes().filter((n) => n.id === WF.connectFrom)[0];
        if (!p) return;
        const x1 = p.x + p.w / 2, y1 = p.y + p.h;
        const x2 = ev.clientX - rect.left + canvas.parentElement.scrollLeft;
        const y2 = ev.clientY - rect.top + canvas.parentElement.scrollTop;
        temp.setAttribute("d", `M${x1} ${y1} L${x2} ${y2}`);
      }
      function upHandler(ev) {
        window.removeEventListener("pointermove", moveHandler);
        window.removeEventListener("pointerup", upHandler);
        temp.remove();
        const target = nodeAt(ev.clientX, ev.clientY);
        const from = WF.connectFrom;
        WF.connectFrom = null;
        if (target && target !== from && target !== "__trigger__") {
          const step = M.findStep(WF.draft, target);
          if (!step) { renderAll(); return; }
          const deps = (step.depends_on || []).slice();
          if (deps.indexOf(from) < 0) deps.push(from);
          if (!M.setDependsOn(WF.draft, target, deps)) {
            setError("That would create a dependency cycle.");
          } else {
            setError("");
            WF.draft.dirty = true;
          }
          WF.selected = target;
        }
        renderAll();
      }
      window.addEventListener("pointermove", moveHandler);
      window.addEventListener("pointerup", upHandler);
      return;
    }

    if (nodeId === "__trigger__") { selectNode(nodeId); return; }

    // move the node (trigger stays put — it is a fixed start marker)
    const node = canvasNodes().filter((n) => n.id === nodeId)[0];
    if (!node) return;
    const start = { x: e.clientX, y: e.clientY, ox: node.x, oy: node.y };
    const scrollLeft = canvas.parentElement.scrollLeft;
    const scrollTop = canvas.parentElement.scrollTop;
    let moved = false;
    function move(ev) {
      const dx = ev.clientX - start.x, dy = ev.clientY - start.y;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
      const nx = Math.max(0, start.ox + dx);
      const ny = Math.max(0, start.oy + dy);
      nodeDom.style.left = nx + "px";
      nodeDom.style.top = ny + "px";
      node.x = nx; node.y = ny;
      WF.draft.layout = WF.draft.layout || {};
      WF.draft.layout[nodeId] = { x: Math.round(nx), y: Math.round(ny) };
      drawEdges(canvasNodes());
      void scrollLeft; void scrollTop;
    }
    function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (moved) WF.draft.dirty = true;
      else selectNode(nodeId);
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function selectNode(id) {
    WF.selected = id;
    renderCanvas();
    renderInspector();
    // touch/mobile: bring the inspector sheet up so the tap does something
    if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
      el("wf-inspector").classList.add("open");
    }
  }

  /* ---- toolbar wiring ---- */
  function focusName() {
    const name = el("wf-name");
    if (name) { name.focus(); name.select(); }
  }

  function wireShell() {
    const newBtn = el("wf-new");
    if (newBtn) newBtn.addEventListener("click", newWorkflow);
    const saveBtn = el("wf-save");
    if (saveBtn) saveBtn.addEventListener("click", saveWorkflow);
    const runBtn = el("wf-run");
    if (runBtn) runBtn.addEventListener("click", runWorkflow);
    const delBtn = el("wf-delete");
    if (delBtn) delBtn.addEventListener("click", deleteWorkflow);

    const name = el("wf-name");
    if (name) name.addEventListener("change", () => {
      const value = name.value.trim();
      if (!value || !WF.draft) { renderToolbar(); return; }
      if (WF.draft.name === value) return;
      if (WF.draft.mode === "new" || WF.draft.id == null) {
        WF.draft.name = value;
        WF.draft.dirty = true;
        renderList();
      } else {
        renameWorkflow(value);
      }
    });
    const search = el("wf-search");
    if (search) search.addEventListener("input", () => {
      WF.filter = search.value;
      renderList();
    });
    const list = el("wf-list");
    if (list) list.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-wf]");
      if (!btn) return;
      // On mobile the rail is a drawer: picking a workflow closes it so the
      // canvas is visible again.
      el("wf-rail").classList.remove("open");
      selectWorkflow(btn.dataset.wf);
    });
    const tabs = el("wf-tabs");
    if (tabs) tabs.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-wf-tab]");
      if (!btn) return;
      WF.bottomTab = btn.dataset.wfTab;
      renderBottom();
    });
    const panel = el("wf-panel");
    if (panel) panel.addEventListener("click", (e) => {
      const run = e.target.closest("[data-run]");
      if (run) { selectRun(run.dataset.run); return; }
      const toggle = e.target.closest("[data-sched-toggle]");
      if (toggle) {
        patch(`/api/schedules/${toggle.dataset.schedToggle}`,
              { enabled: toggle.dataset.enabled !== "1" })
          .then(refreshSchedules).then(renderAll);
        return;
      }
      const rm = e.target.closest("[data-sched-del]");
      if (rm) {
        del(`/api/schedules/${rm.dataset.schedDel}`)
          .then(refreshSchedules).then(renderAll);
      }
    });
    const railToggle = el("wf-rail-toggle");
    if (railToggle) railToggle.addEventListener("click", () =>
      el("wf-rail").classList.toggle("open"));
    const insToggle = el("wf-inspector-toggle");
    if (insToggle) insToggle.addEventListener("click", () =>
      el("wf-inspector").classList.toggle("open"));
    const bottomToggle = el("wf-bottom-toggle");
    if (bottomToggle) bottomToggle.addEventListener("click", () =>
      el("wf-bottom").classList.toggle("collapsed"));
    const insClose = el("wf-inspector-close");
    if (insClose) insClose.addEventListener("click", () =>
      el("wf-inspector").classList.remove("open"));
  }

  /* --------------------------------------------------------------- boot --- */
  async function boot() {
    if (WF.ready || !el("tab-workflow")) return;
    WF.ready = true;
    wireShell();
    wireCanvas();
    renderBottom();
    openStream();
    await refreshOptions();
    await refreshList();
    await refreshSchedules();

    let initial = null;
    try {
      const saved = localStorage.getItem("astra:workflow:selected");
      if (saved) initial = Number(saved);
    } catch (_) { /* ignore */ }
    if (initial == null || !WF.list.some((w) => Number(w.id) === initial)) {
      initial = WF.list.length ? WF.list[0].id : null;
    }
    if (initial != null) {
      const ok = await selectWorkflow(initial, { keepTab: true });
      if (!ok && WF.list.length) await selectWorkflow(WF.list[0].id, { keepTab: true });
    } else {
      newWorkflow();
    }
    renderAll();
  }

  /* astra.js resolves the tab loader from this shared registry — the same
   * mechanism the core tabs use (there is no plugin system any more). */
  if (window.Astra && window.Astra.loaders) window.Astra.loaders.workflow = boot;
  else if (typeof loaders !== "undefined") loaders.workflow = boot;

  /* Public surface: the tab loader plus the pieces worth driving from a
   * test or the console (state, re-render, live event handler). */
  window.AstraWorkflow = { boot, state: WF, refresh: renderAll, onEvent };
})();
