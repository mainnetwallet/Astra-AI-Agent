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
  var NOISE_KINDS = { "ai.token": 1, "scheduler.tick": 1 };

  function isMeaningful(event) {
    var e = event || {};
    var kind = String(e.kind == null ? "" : e.kind);
    if (!kind) return false;
    if (NOISE_KINDS[kind]) return false;
    // The Gateway's own connections also emit ai.* while streaming, but every
    // Gateway call is already reported (with provider+model) by the
    // astra_gateway.* wrapper — showing both would double-count one call.
    if (kind.indexOf("ai.") === 0 && e.agent === "gateway") return false;
    if (categoryOf(kind) === "system" && !/(failed|error)$/.test(kind)) return false;
    return true;
  }

  /* ----------------------------------------------------------------- status */
  function statusOf(kind, data) {
    var k = String(kind == null ? "" : kind);
    var d = data || {};
    if (/correction_(failed|exhausted)$/.test(k)) return "err";
    if (/(^|\.)(error|failed)$/.test(k) || /\.failed$/.test(k)) return "err";
    if (k === "astra_gateway.stream_interrupted") return "err";
    if (k === "provider.health_changed") return d.healthy === false ? "err" : "ok";
    if (k === "correction_requested" || /correction_requested$/.test(k)) return "warn";
    if (k === "router.fallback" || k === "router.retry" ||
        k === "credential.rotation" || k === "gateway.target_cooldown") return "warn";
    if (k === "task.cancelled" || k === "task.skipped") return "warn";
    if (k === "astra_gateway.request" || k === "router.request" ||
        k === "chat.pipeline.started") return "running";
    if (/\.(started|running|requested)$/.test(k)) return "running";
    if (/\.(completed|succeeded|confirmed|broadcast|submitted|saved|recalled|learned|done)$/.test(k)) return "ok";
    if (k === "astra_gateway.success" || k === "chat.pipeline.verified" ||
        k === "chat.pipeline.finished" || k === "gateway.execution_recovered" ||
        k === "gateway.execution_completed" || k === "provider.selected") return "ok";
    if (k === "gateway.execution_failed") return "err";
    return "info";
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
    "chat.pipeline.assigned": ["🧠", "Agent Router"],
    "chat.pipeline.verified": ["✅", "Response verified"],
    "chat.pipeline.finished": ["✅", "Response generated"],
    "chat.pipeline.failed":   ["❌", "Request failed"],
    "chat.pipeline.understand_failed": ["⚠️", "Request not understood"],
    "chat.pipeline.verify_error":      ["⚠️", "Verification error"],
    "browser.opened":     ["🌐", "Browser opened"],
    "browser.navigation": ["🌐", "Browser navigation"],
    "browser.action":     ["🌐", "Browser action"],
    "browser.error":      ["🌐", "Browser error"],
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
  };

  function titleOf(kind) {
    var k = String(kind == null ? "" : kind);
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
                     "attempt", "attempts", "verdict", "reason", "route"];

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
      push(humanize(key), fieldValue(d[key]));
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

  function normalize(event) {
    var e = event || {};
    var kind = String(e.kind == null ? "" : e.kind);
    var d = e.data && typeof e.data === "object" ? e.data : {};
    var t = titleOf(kind);
    var status = statusOf(kind, d);
    var dur = durationOf(d);
    var subject = clip(scrub(subjectOf(kind, d)), 80);
    var detail;
    if (status === "err") detail = clip(scrub(d.error || d.reason || "failed"), 120);
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
    return out;
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
  // renders oldest -> newest with live events appended at the bottom. Sort by
  // id so the order is correct no matter what the endpoint returned.
  function orderHistory(rows) {
    return (rows || []).slice().sort(function (a, b) {
      return ((a && a.id) || 0) - ((b && b.id) || 0);
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

  function createState() {
    return {
      filter: "all", query: "", paused: false,
      counts: EMPTY_COUNTS(),
      follow: true, unread: 0,
      rendered: new Set(),   // event ids already shown (dedupe)
      pending: [],           // events buffered while paused
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
    reset: reset,
    matchesRow: matchesRow,
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
