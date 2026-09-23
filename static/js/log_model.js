/* Astra Activity Log — pure presentation model + live-scroll state machine.
 *
 * Deliberately DOM-free so the timeline mapping rules and the bottom-up
 * live-scroll behaviour can be unit-tested under node without a browser.
 * astra.js consumes this through `window.AstraLog`; it is a *mapping layer*
 * over the existing /api/events + SSE stream, not a second log system.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AstraLog = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var NEAR_BOTTOM_PX = 48;

  /* ---------------------------------------------------------------- secrets */
  var SECRET_KEY = /(^|[_\-\s.])(api[_-]?key|apikey|secret|passwd|password|pwd|token|authorization|auth[_-]?header|private[_-]?key|seed|seed[_-]?phrase|mnemonic|cookie|session[_-]?id|master[_-]?secret|keyfile|credential|access[_-]?key|secret[_-]?key)(?=$|[_\-\s.]|\d)/i;
  var SECRET_VAL = /(sk-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}|glft-[A-Za-z0-9_\-]{10,}|Bearer\s+\S+|x-api-key\s*[:=]\s*\S+|0x[a-fA-F0-9]{60,})/g;
  var REDACTED = "***redacted***";

  function scrub(value) {
    if (value == null) return "";
    return String(value).replace(SECRET_VAL, REDACTED);
  }
  function isSecretKey(key) { return SECRET_KEY.test(String(key == null ? "" : key)); }
  function clip(value, max) {
    var s = value == null ? "" : String(value);
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  /* ------------------------------------------------------------- categories */
  function categoryOf(kind) {
    var k = String(kind == null ? "" : kind);
    var head = k.split(".")[0];
    if (head === "web3") return "web3";
    if (head === "browser") return "browser";
    if (head === "tool") return "tools";
    // Host-terminal FALLBACK lifecycle (astra/terminal/approval.py): the
    // approval request/decision and the one approved host command. Filed
    // under "tools" so it stays visible; it is deliberately NOT "system"
    // (which the noise filter drops).
    if (head === "host_terminal") return "tools";
    if (head === "ai" || head === "astra_gateway" || head === "gateway" ||
        head === "router" || head === "provider" || head === "credential" ||
        head === "supervision") return "ai";
    if (head === "agent" || head === "task" || head === "workflow" ||
        head === "scheduler" || head === "chat" || head === "memory" ||
        head === "experience") return "agents";
    return "system";
  }

  // Kinds that are pure heartbeats / duplicate another event and would only
  // add noise. Dropped before rendering (they stay in the persisted log).
  // `gateway.recovery_target_selected` is metadata for the *next* recovery
  // attempt (the attempt itself is reported by its own ai.*/astra_gateway.*
  // lifecycle), so on its own it renders as an empty "Recovery target" row.
  var NOISE_KINDS = { "ai.token": 1, "scheduler.tick": 1,
                      "gateway.recovery_target_selected": 1 };

  function isMeaningful(event) {
    var e = event || {};
    var kind = String(e.kind == null ? "" : e.kind);
    if (!kind) return false;
    if (NOISE_KINDS[kind]) return false;
    // The Gateway's own connections also emit ai.* while streaming, but every
    // Gateway call is already reported (with provider+model) by the
    // astra_gateway.* wrapper — showing both would double-count one call.
    if (kind.indexOf("ai.") === 0 && e.agent === "gateway") return false;
    // `operation.interrupted` is the startup reconciliation terminal — it
    // must stay visible or the stale start row it closes would look
    // permanently running again.
    if (categoryOf(kind) === "system" &&
        !/(failed|error|interrupted)$/.test(kind)) return false;
    return true;
  }

  /* ----------------------------------------------------------------- status */
  function statusOf(kind, data) {
    var k = String(kind == null ? "" : kind);
    var d = data || {};
    // Explicit lifecycle hints win: a non-terminal failure is a warning (the
    // operation is still going / about to retry), not a terminal error.
    if (d.retrying === true || d.terminal === false) return "warn";
    // Web3 stages: prepared/submitted/broadcast are in-flight; only
    // confirmed/rejected/failed end the transaction.
    if (k === "web3.transaction.confirmed") return "ok";
    if (k === "web3.transaction.rejected") return "warn";
    if (/^web3\.transaction\.(prepared|submitted|broadcast)$/.test(k)) return "running";
    if (k === "operation.interrupted") return "warn";
    if (/correction_(failed|exhausted)$/.test(k)) return "err";
    if (/(^|[._])(error|failed)$/.test(k)) return "err";
    if (k === "astra_gateway.stream_interrupted") return "err";
    if (k === "provider.health_changed") return d.healthy === false ? "err" : "ok";
    if (k === "correction_requested" || /correction_requested$/.test(k)) return "warn";
    if (k === "router.fallback" || k === "router.retry" ||
        k === "credential.rotation" || k === "gateway.target_cooldown") return "warn";
    if (k === "task.cancelled" || k === "task.skipped") return "warn";
    // Host-terminal fallback: a denied/expired approval is a real warning
    // (nothing ran); an allowed request is simply ok.
    if (k === "host_terminal.approval_requested") return "running";
    if (k === "host_terminal.approval_denied" ||
        k === "host_terminal.approval_expired") return "warn";
    if (k === "host_terminal.approval_allowed") return "ok";
    if (k === "astra_gateway.request" || k === "router.request" ||
        k === "chat.pipeline.started") return "running";
    if (/\.(started|running|requested)$/.test(k)) return "running";
    if (/\.(completed|succeeded|confirmed|broadcast|submitted|saved|recalled|learned|done)$/.test(k)) return "ok";
    if (k === "astra_gateway.success" || k === "chat.pipeline.verified" ||
        k === "chat.pipeline.finished" || k === "gateway.execution_recovered" ||
        k === "gateway.execution_completed" || k === "provider.selected" ||
        k === "router.decision" || k === "chat.pipeline.assigned") return "ok";
    if (k === "gateway.execution_failed") return "err";
    // Anything else is a discrete, non-lifecycle step that already
    // happened; treating it as info left a meaningless "•" as the visible
    // row state, so it reads as a completed step instead.
    return "ok";
  }

  /* --------------------------------------------------- human-readable copy */
  function humanize(part) {
    return String(part == null ? "" : part)
      .replace(/[._]+/g, " ").replace(/\s+/g, " ").trim()
      .replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  // icon + title per event kind (title is the bold first line).
  var TITLES = {
    "tool.completed": ["⚡", "Tool execution"],
    "tool.started":   ["⚡", "Tool execution"],
    "tool.failed":    ["⚡", "Tool failed"],
    "ai.started":     ["🧠", "Provider call"],
    "ai.completed":   ["🧠", "Provider response"],
    "ai.failed":      ["🧠", "Provider failed"],
    "astra_gateway.request": ["🧭", "Gateway routing"],
    "astra_gateway.success": ["🧭", "Gateway call"],
    "astra_gateway.error":   ["🧭", "Gateway error"],
    "astra_gateway.stream_interrupted": ["🧭", "Gateway stream interrupted"],
    "astra_gateway.test":    ["🧭", "Gateway test"],
    "router.request":  ["🧠", "Agent Router"],
    "router.decision": ["🧠", "Agent Router"],
    "router.fallback": ["🧠", "Agent Router"],
    "router.retry":    ["🧠", "Agent Router"],
    "router.gateway_supervision": ["🧠", "Agent Router"],
    "router.gateway_task_completion": ["🧠", "Agent Router"],
    "provider.health_changed": ["🩺", "Provider health"],
    "provider.selected": ["🩺", "Provider selected"],
    "provider.failed": ["🩺", "Provider failed"],
    "credential.rotation": ["🔑", "Credential rotation"],
    "chat.pipeline.started":  ["🚀", "Request received"],
    "chat.pipeline.assigned": ["🧭", "Agent assigned"],
    "chat.pipeline.verified": ["✅", "Response verified"],
    "chat.pipeline.finished": ["✅", "Response generated"],
    "chat.pipeline.failed":   ["❌", "Request failed"],
    "chat.pipeline.understand_failed": ["⚠️", "Request not understood"],
    "chat.pipeline.verify_error":      ["⚠️", "Verification error"],
    "browser.opened":     ["🌐", "Browser opened"],
    "browser.navigation": ["🌐", "Browser navigation"],
    "browser.action":     ["🌐", "Browser action"],
    "browser.error":      ["🌐", "Browser error"],
    // Host-terminal FALLBACK (Assistant-Chat approval + the approved host
    // command). Allow/Deny lives in the chat card, never in this log.
    "host_terminal.approval_requested": ["🔐", "Host terminal approval requested"],
    "host_terminal.approval_allowed":   ["🔓", "Host terminal approved"],
    "host_terminal.approval_denied":    ["🚫", "Host terminal denied"],
    "host_terminal.approval_expired":   ["⌛", "Host terminal approval expired"],
    "host_terminal.started":   ["🖥️", "Host command started"],
    "host_terminal.completed": ["🖥️", "Host command completed"],
    "host_terminal.failed":    ["🖥️", "Host command failed"],
    "memory.saved":    ["🧩", "Memory saved"],
    "memory.recalled": ["🧩", "Memory recalled"],
    "experience.learned": ["🧩", "Experience learned"],
    "workflow.started":   ["🔧", "Workflow started"],
    "workflow.completed": ["🔧", "Workflow completed"],
    "workflow.failed":    ["🔧", "Workflow failed"],
    "task.created":   ["📋", "Task created"],
    "task.started":   ["📋", "Task started"],
    "task.completed": ["📋", "Task completed"],
    "task.done":      ["📋", "Task done"],
    "task.failed":    ["📋", "Task failed"],
    "scheduler.tick": ["⏱️", "Scheduler tick"],
    "web3.transaction.prepared":  ["⛓️", "Transaction prepared"],
    "web3.transaction.submitted": ["⛓️", "Transaction submitted"],
    "web3.transaction.broadcast": ["⛓️", "Transaction broadcast"],
    "web3.transaction.rejected":  ["⛓️", "Transaction rejected"],
    "web3.transaction.confirmed": ["⛓️", "Transaction confirmed"],
    "web3.transaction.failed":    ["⛓️", "Transaction failed"],
    "gateway.execution_completed": ["🔁", "Gateway execution"],
    "gateway.execution_recovered": ["🔁", "Gateway recovery"],
    "gateway.execution_failed":    ["🔁", "Gateway execution failed"],
    "gateway.target_cooldown":     ["🔁", "Target cooldown"],
    "gateway.recovery_target_selected": ["🔁", "Recovery target"],
    // Emitted by the backend when the app starts and finds an operation
    // that began in a previous run and can never finish — it keeps the
    // original title so the row still reads as the operation it was.
    "operation.interrupted": ["⚠️", "Interrupted"],
  };

  function titleOf(kind, data) {
    var k = String(kind == null ? "" : kind);
    var d = data || {};
    if (k === "operation.interrupted" && d.original_kind &&
        TITLES[d.original_kind]) return TITLES[d.original_kind];
    if (TITLES[k]) return TITLES[k];
    if (/^gateway\.(task_completion|supervision)\.correction_/.test(k)) {
      return ["🔁", "Result supervision"];
    }
    if (/^supervision\./.test(k)) return ["🔁", "Supervision"];
    if (/^gateway\./.test(k)) return ["🔁", "Gateway recovery"];
    if (/^agent\./.test(k)) return ["🧠", "Agent " + humanize(k.split(".").slice(1).join("."))];
    if (/^task\./.test(k)) return ["📋", "Task " + humanize(k.split(".").slice(1).join("."))];
    if (/^workflow\./.test(k)) return ["🔧", "Workflow " + humanize(k.split(".").slice(1).join("."))];
    if (/^chat\./.test(k)) return ["🚀", humanize(k.split(".").slice(1).join("."))];
    if (/^web3\./.test(k)) return ["⛓️", humanize(k.split(".").slice(1).join("."))];
    if (/^tool\./.test(k)) return ["⚡", "Tool " + humanize(k.split(".").slice(1).join("."))];
    if (/^browser\./.test(k)) return ["🌐", "Browser " + humanize(k.split(".").slice(1).join("."))];
    return ["•", humanize(k)];
  }

  function fmtMs(ms) {
    var n = Number(ms);
    if (ms == null || ms === "" || isNaN(n)) return "";
    return n >= 1000 ? (n / 1000).toFixed(2).replace(/\.?0+$/, "") + "s"
                     : Math.round(n) + "ms";
  }

  function durationOf(d) {
    if (d.duration_ms != null) return fmtMs(d.duration_ms);
    if (d.latency_ms != null) return fmtMs(d.latency_ms);
    return "";
  }

  // Backend timestamps are "YYYY-MM-DD HH:MM:SS" (local time). Parse them the
  // same way for both endpoints so a derived duration can never be affected by
  // the reader's clock or by when the DOM inserted the row.
  function parseStamp(ts) {
    var s = String(ts == null ? "" : ts).trim();
    if (!s) return NaN;
    return Date.parse(s.indexOf("T") >= 0 ? s : s.replace(" ", "T"));
  }

  function elapsedMs(a, b) {
    var ta = parseStamp(a), tb = parseStamp(b);
    if (isNaN(ta) || isNaN(tb)) return null;
    return tb - ta;
  }

  // An operation with both endpoints derives its duration from those backend
  // timestamps when the event itself carries no explicit duration_ms/latency.
  function withDuration(m) {
    if (!m || m.duration) return m;
    if (m.startTs && m.endTs && m.endTs !== m.startTs) {
      var ms = elapsedMs(m.startTs, m.endTs);
      if (ms != null && ms > 0) m.duration = fmtMs(ms);
    }
    return m;
  }

  function targetOf(d) {
    var provider = d.provider || "";
    var model = d.model || "";
    if (provider && model) return provider + " · " + model;
    return provider || model || "";
  }

  var SUBJECTS = {
    "tools":   function (d) { return d.tool || ""; },
    "ai":      function (d) { return targetOf(d) || d.tool || ""; },
    "browser": function (d) { return d.url || d.tool || ""; },
    "web3":    function (d) { return clip(d.tx || d.tx_id || "", 24); },
    "agents":  function (d) { return d.agent_name || d.workflow || d.task || targetOf(d) || ""; },
  };

  function subjectOf(kind, d) {
    var cat = categoryOf(kind);
    var fn = SUBJECTS[cat];
    return fn ? fn(d) : "";
  }

  // Which data keys become the expanded "details" grid (order matters).
  var FIELD_ORDER = ["provider", "model", "tool", "category", "status",
                     "task", "workflow", "step", "tx", "tx_id", "network",
                     "attempt", "attempts", "verdict", "reason", "error",
                     "op", "trace", "request", "route"];
  // Friendlier labels for the identifier fields (op/trace are the lifecycle
  // correlation ids; showing them makes "one operation = one row" auditable).
  var FIELD_LABELS = { op: "Operation", trace: "Trace", request: "Request",
                       tx_id: "Tx", run_id: "Run", error: "Error" };

  function fieldValue(v) {
    if (v == null || v === "") return "";
    if (typeof v === "object") {
      try { return clip(JSON.stringify(v), 200); } catch (_) { return ""; }
    }
    return clip(scrub(v), 200);
  }

  function fieldsOf(event, d) {
    var fields = [];
    var seen = {};
    function push(label, value) {
      if (!value || seen[label]) return;
      seen[label] = 1;
      fields.push([label, value]);
    }
    for (var i = 0; i < FIELD_ORDER.length; i++) {
      var key = FIELD_ORDER[i];
      if (key === "status") continue;
      push(FIELD_LABELS[key] || humanize(key), fieldValue(d[key]));
    }
    push("Agent", fieldValue(event.agent));
    return fields;
  }

  /* ------------------------------------------------------------- normalize */
  function timeOf(created) {
    var s = String(created == null ? "" : created);
    var m = s.match(/(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : "";
  }

  // The canonical event timestamp comes from the backend (`created_at`), never
  // from when the DOM happened to receive the event. Ordering compares this
  // first and the event id second (same-second events keep emission order).
  function stampOf(created) {
    return String(created == null ? "" : created).trim();
  }

  // Accepts either a raw event (created_at/id) or a normalized model
  // (sortTs/sortId) so history, live events and rendered rows all order with
  // the exact same rule.
  function chronKey(obj) {
    var o = obj || {};
    var ts = o.sortTs != null ? o.sortTs : stampOf(o.created_at);
    var id = o.sortId != null ? o.sortId : (Number(o.id) || 0);
    return { ts: ts, id: id };
  }

  function compareChron(a, b) {
    var ka = chronKey(a), kb = chronKey(b);
    if (ka.ts && kb.ts && ka.ts !== kb.ts) return ka.ts < kb.ts ? -1 : 1;
    return ka.id - kb.id;
  }

  function normalize(event) {
    var e = event || {};
    var kind = String(e.kind == null ? "" : e.kind);
    var d = e.data && typeof e.data === "object" ? e.data : {};
    var t = titleOf(kind, d);
    var status = statusOf(kind, d);
    var dur = durationOf(d);
    var startTs = stampOf(e.created_at);
    var terminal = phaseOf(kind, d) === "terminal";
    var subject = clip(scrub(subjectOf(kind, d)), 80);
    var detail;
    if (status === "err") detail = clip(scrub(d.error || d.reason || "failed"), 120);
    else if (status === "warn" && (d.reason || d.error || d.status))
      detail = clip(scrub(d.reason || d.error || d.status), 120);
    else if (dur) detail = dur;
    else if (d.status) detail = clip(scrub(d.status), 60);
    else detail = "";

    var input = typeof d.input === "string" && d.input ? clip(scrub(d.input), 280) : "";
    var output = typeof d.output === "string" && d.output ? clip(scrub(d.output), 280) : "";

    var out = {
      id: e.id,
      kind: kind,
      agent: e.agent || "",
      time: timeOf(e.created_at),
      endTime: terminal ? timeOf(e.created_at) : "",
      startTs: startTs,
      endTs: terminal ? startTs : "",
      sortTs: startTs,               // chronological position = START time
      sortId: Number(e.id) || 0,
      category: categoryOf(kind),
      status: status,
      icon: t[0],
      title: t[1],
      subject: subject,
      detail: detail,
      duration: dur,
      fields: fieldsOf(e, d),
      input: input,
      output: output,
    };
    out.search = [out.kind, out.agent, out.title, out.subject, out.detail,
                  out.category].join(" ").toLowerCase();
    return withDuration(out);
  }

  /* ------------------------------------------------- live-scroll behaviour */
  function distanceFromBottom(m) {
    var mm = m || {};
    return (mm.scrollHeight || 0) - (mm.scrollTop || 0) - (mm.clientHeight || 0);
  }
  function isNearBottom(m, slack) {
    var s = slack == null ? NEAR_BOTTOM_PX : slack;
    return distanceFromBottom(m) <= s;
  }

  // Called on a real user scroll. Snap back to follow when they reach the
  // bottom again; otherwise remember they are reading history.
  function onScroll(state, m) {
    if (isNearBottom(m)) {
      state.follow = true;
      state.unread = 0;
    } else {
      state.follow = false;
    }
    return state;
  }

  // Called when a new row is appended. Returns what the caller must do.
  function onAppend(state, m, count) {
    var n = count == null ? 1 : count;
    if (state.follow && isNearBottom(m)) {
      return { scrollToBottom: true, showIndicator: false, unread: 0 };
    }
    state.follow = false;
    state.unread += n;
    return { scrollToBottom: false, showIndicator: true, unread: state.unread };
  }

  function onJumpToLatest(state) {
    state.follow = true;
    state.unread = 0;
    return state;
  }

  // ScrollTop that keeps the viewport stable after trimming `removedHeight`
  // px from above it (when the DOM buffer overflows).
  function compensateTrim(scrollTop, removedHeight) {
    return Math.max(0, (scrollTop || 0) - (removedHeight || 0));
  }

  /* --------------------------------------------------------------- ordering */
  // History comes back newest-first from /api/events; the timeline always
  // renders oldest -> newest. Sort by the SAME canonical (created_at, id) key
  // the live feed uses, so history and live events can never disagree.
  function orderHistory(rows) {
    return (rows || []).slice().sort(function (a, b) {
      return compareChron(a, b);
    });
  }

  /* ------------------------------------------------------- state + counters */
  var EMPTY_COUNTS = function () {
    return { total: 0, agents: 0, ai: 0, tools: 0,
             browser: 0, web3: 0, errors: 0 };
  };
  // Keep the in-memory bookkeeping bounded too — the rendered-id set and the
  // paused queue would otherwise grow forever on a long-lived panel.
  var RENDERED_MAX = 1000;
  var PENDING_MAX = 500;

  /* ------------------------------------------------------------- lifecycle
   * A lifecycle event carries an operation id (`op`) — or, for web3, a tx id —
   * so a start and its terminal event resolve the SAME row instead of leaving
   * a stale "running" row behind. `trace` links a child operation to the
   * higher-level request/run that owns it, so a request that ends can resolve
   * any child it left running. */
  var START_SUFFIX = /\.(started|request)$/;
  var TERMINAL_SUFFIX = /\.(completed|succeeded|failed|error|timeout|cancelled|rejected|confirmed|done|finished|exhausted)$/;
  var WEB3_TERMINAL = /^web3\.transaction\.(confirmed|failed|rejected)$/;
  var UPDATE_KINDS = /^(router\.(retry|fallback|gateway_task_completion|gateway_supervision)|credential\.rotation|gateway\.(target_cooldown|execution_completed|execution_recovered|execution_failed))$/;

  function phaseOf(kind, d) {
    var k = String(kind == null ? "" : kind);
    var data = d || {};
    if (data.terminal === true) return "terminal";
    if (data.terminal === false || data.retrying === true) return "update";
    if (WEB3_TERMINAL.test(k)) return "terminal";
    if (START_SUFFIX.test(k)) return "start";
    if (TERMINAL_SUFFIX.test(k)) return "terminal";
    if (UPDATE_KINDS.test(k) || /correction_requested$/.test(k)) return "update";
    return "event";
  }

  // The reconciliation key for an event, or "" when it is a standalone row.
  function lifecycleOf(event, model) {
    var e = event || {};
    var d = (e.data && typeof e.data === "object") ? e.data : {};
    var kind = String(e.kind || "");
    var key = "";
    if (d.op) key = "op:" + d.op;
    else if (kind.indexOf("web3.") === 0 && d.tx && d.tx !== "*") key = "tx:" + d.tx;
    else if (kind.indexOf("task.") === 0 && d.task_id != null && d.task_id !== "")
      key = "task:" + d.task_id;
    else if (kind.indexOf("workflow.") === 0 && d.run_id != null && d.run_id !== "")
      key = "wf:" + d.run_id;
    var phase = phaseOf(kind, d);
    // An operation stays tracked (so its later terminal event resolves this
    // SAME row) while it is starting, mid-flight, or retrying. A non-terminal
    // failure (retrying=true) is an update, not the end of the operation.
    var status = model ? model.status : "";
    var running = phase !== "terminal" &&
      (phase === "start" || phase === "update" || status === "running");
    return { key: key, phase: phase, running: running,
             trace: d.trace ? String(d.trace) : "",
             request: d.request ? String(d.request) : "",
             run_id: (d.run_id == null ? "" : String(d.run_id)) };
  }

  // Decide how an arriving event affects the rendered timeline. Pure: the
  // caller applies DOM changes, then calls commit().
  function planRender(state, event, model) {
    var lc = lifecycleOf(event, model);
    var existing = lc.key && state.active.has(lc.key)
      ? state.active.get(lc.key) : null;
    var closeKeys = [];
    if (lc.phase === "terminal") {
      var req = lc.request || (lc.key.indexOf("req:") === 0 ? lc.key.slice(4) : "");
      var run = lc.run_id;
      // The run's OWN row (`op:wf:<run>`) is an ancestor, not a child — a
      // step's terminal must never close it (doing so orphans the run row and
      // makes the later workflow.completed append a duplicate).
      var runKey = run ? "op:wf:" + run : "";
      state.active.forEach(function (info, k) {
        if (k === lc.key) return;
        if (runKey && k === runKey) return;
        var sameReq = req && (info.request === req || info.trace === req);
        var sameRun = run && (info.run_id === run || info.trace === "wf:" + run);
        if (sameReq || sameRun) closeKeys.push(k);
      });
    }
    return { action: existing ? "update" : "append", key: lc.key,
             phase: lc.phase, running: lc.running, closeKeys: closeKeys,
             trace: lc.trace, request: lc.request, run_id: lc.run_id };
  }

  // Apply a plan to the state's bookkeeping (the DOM element is stored on the
  // same info object by the caller and preserved here).
  function commit(state, plan, model) {
    if (!plan.key) return state;
    if (plan.phase === "terminal") {
      state.active.delete(plan.key);
    } else if (plan.running || state.active.has(plan.key)) {
      var info = state.active.get(plan.key) || {};
      info.request = plan.request;
      info.trace = plan.trace;
      info.run_id = plan.run_id;
      info.category = model ? model.category : "";
      state.active.set(plan.key, info);
    } else {
      state.active.delete(plan.key);
    }
    plan.closeKeys.forEach(function (k) { state.active.delete(k); });
    return state;
  }

  // Adjust category/error counters when a row changes status (running -> ok/err).
  function recount(state, oldModel, newModel) {
    if (oldModel) {
      if (state.counts[oldModel.category] != null) state.counts[oldModel.category]--;
      if (oldModel.status === "err") state.counts.errors--;
    }
    if (state.counts[newModel.category] != null) state.counts[newModel.category]++;
    if (newModel.status === "err") state.counts.errors++;
    return state.counts;
  }

  // A lifecycle continuation (started -> completed/failed/retrying) refines the
  // SAME operation row. Its timeline identity — id, START time and the
  // (timestamp, id) sort key — stays pinned to the original start event, so a
  // completion never makes the row jump. The terminal event contributes the END
  // timestamp (+ duration), so the row can show "start → end" without losing
  // its chronological position.
  //
  // Duration belongs to the OPERATION, not to whichever event borrowed its
  // lifecycle key: a terminal event's duration is its own start -> terminal
  // span (an explicit `duration_ms` on that terminal still wins), and an
  // intermediate/progress event (e.g. a per-tool `duration_ms` on the tool
  // loop's key) never sets — or keeps — a duration for a running operation.
  // Without this, a 3ms tool result leaked into a tool-loop row that actually
  // spanned ~108s, so the finished row read "COMPLETE · 3ms".
  function mergeLifecycle(oldModel, newModel) {
    var merged = Object.assign({}, newModel);
    if (oldModel) {
      merged.id = oldModel.id;
      merged.time = oldModel.time;
      merged.startTs = oldModel.startTs || oldModel.sortTs;
      merged.sortTs = oldModel.sortTs;
      merged.sortId = oldModel.sortId;
      if (newModel.endTs) {
        // Terminal: the operation ended here. Keep the END timestamp; the
        // duration is the whole span, so a duration recorded by an earlier
        // progress event must be dropped unless this very event carries one.
        merged.endTs = newModel.endTs;
        merged.endTime = newModel.endTime || "";
        if (!newModel.duration) merged.duration = "";
      } else {
        // intermediate/retry refinement: keep any end already recorded
        merged.endTs = oldModel.endTs || "";
        merged.endTime = oldModel.endTime || "";
        // A refinement of a still-running operation never gives it a
        // duration — only a terminal event can.
        if (!merged.endTs) merged.duration = "";
        else if (!newModel.duration) merged.duration = oldModel.duration || "";
      }
    }
    return withDuration(merged);
  }

  // Displayed time span: the start time always; the terminal time too once the
  // operation has ended (a same-second completion collapses to one stamp).
  function timeRangeOf(m) {
    var mm = m || {};
    var start = mm.time || "";
    var end = mm.endTime || "";
    if (!end || end === start) end = "";
    return { start: start, end: end };
  }

  function statusLabel(m) {
    var mm = m || {};
    if (mm.endTs) {
      if (mm.status === "err") return "FAILED";
      if (mm.status === "warn") return "WARNING";
      if (mm.status === "ok") return "COMPLETE";
    }
    return String(mm.status || "").toUpperCase();
  }

  // Canonical reason shared with astra.js, so the DOM and the tests can never
  // disagree about why a child row was resolved.
  var INTERRUPTED_REASON = "interrupted when the request ended";

  // The model a child operation gets when its request/run ends while the
  // child itself never reported a terminal: it is marked interrupted
  // (warning) and keeps its icon and its timeline identity. Returns the SAME
  // model object when there is nothing to do, so callers can cheaply tell
  // whether the row needs re-rendering.
  function interruptedModel(m, reason) {
    if (!m || m.status !== "running") return m;
    var why = reason || INTERRUPTED_REASON;
    return Object.assign({}, m, {
      status: "warn", detail: why, icon: m.icon,
      search: (m.search + " " + why).toLowerCase(),
    });
  }

  function createState() {
    return {
      filter: "all", query: "", paused: false,
      counts: EMPTY_COUNTS(),
      follow: true, unread: 0,
      rendered: new Set(),   // event ids already shown (dedupe)
      pending: [],           // events buffered while paused
      active: new Map(),     // lifecycle key -> {el, request, trace, run_id}
    };
  }

  // Decide what to do with an arriving event: skip (noise / duplicate),
  // buffer (paused) or render.
  function admit(state, event) {
    if (!event || event.id == null) return "skip";
    if (!isMeaningful(event)) return "skip";
    if (state.rendered.has(String(event.id))) return "skip";
    return state.paused ? "buffer" : "render";
  }

  function markRendered(state, event) {
    if (!event || event.id == null) return;
    state.rendered.add(String(event.id));
    while (state.rendered.size > RENDERED_MAX) {
      state.rendered.delete(state.rendered.values().next().value);
    }
  }

  function buffer(state, event) {
    state.pending.push(event);
    while (state.pending.length > PENDING_MAX) state.pending.shift();
    return state.pending.length;
  }

  function count(state, model) {
    state.counts.total++;
    if (state.counts[model.category] != null) state.counts[model.category]++;
    if (model.status === "err") state.counts.errors++;
    return state.counts;
  }

  function reset(state) {
    state.counts = EMPTY_COUNTS();
    state.rendered = new Set();
    state.pending = [];
    state.active = new Map();
    state.unread = 0;
    state.follow = true;
    return state;
  }

  // Filter-chip + search predicate for one rendered row.
  function matchesRow(cats, text, filter, query) {
    const list = String(cats || "").split(",");
    const okFilter = filter === "all" || list.indexOf(filter) !== -1;
    const q = String(query || "").trim().toLowerCase();
    return okFilter && (!q || String(text || "").toLowerCase().indexOf(q) !== -1);
  }

  return {
    NEAR_BOTTOM_PX: NEAR_BOTTOM_PX,
    categoryOf: categoryOf,
    isMeaningful: isMeaningful,
    statusOf: statusOf,
    normalize: normalize,
    titleOf: titleOf,
    fmtMs: fmtMs,
    scrub: scrub,
    isSecretKey: isSecretKey,
    orderHistory: orderHistory,
    createState: createState,
    admit: admit,
    markRendered: markRendered,
    buffer: buffer,
    count: count,
    recount: recount,
    mergeLifecycle: mergeLifecycle,
    interruptedModel: interruptedModel,
    INTERRUPTED_REASON: INTERRUPTED_REASON,
    timeRangeOf: timeRangeOf,
    statusLabel: statusLabel,
    elapsedMs: elapsedMs,
    reset: reset,
    matchesRow: matchesRow,
    compareChron: compareChron,
    chronKey: chronKey,
    phaseOf: phaseOf,
    lifecycleOf: lifecycleOf,
    planRender: planRender,
    commit: commit,
    RENDERED_MAX: RENDERED_MAX,
    PENDING_MAX: PENDING_MAX,
    distanceFromBottom: distanceFromBottom,
    isNearBottom: isNearBottom,
    onScroll: onScroll,
    onAppend: onAppend,
    onJumpToLatest: onJumpToLatest,
    compensateTrim: compensateTrim,
  };
});
