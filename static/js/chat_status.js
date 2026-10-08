/* Astra Assistant — chat execution-status model.
 *
 * The Assistant chat shows ONE compact, human-readable line for whatever
 * Astra is doing right now (plus an expandable step timeline). This module is
 * the mapping layer that turns the REAL lifecycle events Astra already emits
 * into that line:
 *
 *   lifecycle event -> human-readable status -> compact status -> step state
 *
 * It is deliberately NOT a second event system:
 *   - it consumes the SAME events the Activity Log consumes (the /api/events
 *     history + SSE stream, fed by astra.js through receiveEvent()),
 *   - it reuses AstraLog's noise filter (isMeaningful) and its redaction
 *     (scrub) instead of re-implementing either,
 *   - it never invents progress: no timers, no rotation, no simulated steps.
 *     An event that never arrives never changes the status; the step timeline
 *     only ever contains operations that really started.
 *
 * It also owns the EXECUTION-CARD model (createCardTracker): ONE card per
 * REAL tool execution, so a turn that runs three terminal commands shows
 * three separate cards underneath the compact status (a separate card
 * collection, never a restyled single timeline). Same events, same
 * correlation, same stale/ordering rules.
 *
 * DOM-free on purpose (like log_model.js) so the mapping, the
 * current-operation tracking (including out-of-order/stale events), the card
 * correlation and the completion/failure summaries are unit-testable under
 * node — see tests/js/chat_status.test.js, tests/js/chat_cards.test.js,
 * tests/js/chat_status_ui.test.js, tests/js/chat_cards_ui.test.js,
 * tests/test_chat_status_ui.py and tests/test_chat_cards_ui.py.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(root);
  else root.AstraChatStatus = factory(root);
})(typeof self !== "undefined" ? self : this, function (root) {
  "use strict";

  /* ------------------------------------------------------------------ state */
  var STATE = { IDLE: "idle", WORKING: "working",
                COMPLETED: "completed", FAILED: "failed" };
  var STEP = { DONE: "done", ACTIVE: "active", FAILED: "failed",
               PENDING: "pending" };

  var MAX_STEPS = 40;        // bounded: a long tool loop must not grow forever
  var SEEN_MAX = 600;        // bounded de-dupe set (event ids already applied)
  var REASON_MAX = 140;      // one short human reason, never a payload dump

  /* ------------------------------------------------- existing infra reuse --
   * Redaction + noise filtering belong to the Activity Log model; the chat
   * status must not grow its own copy. Resolved lazily so load order (and the
   * node test harness) can never matter. */
  function globals() {
    return (typeof globalThis !== "undefined" ? globalThis : root) || {};
  }
  // The Activity Log model may live on the global object (browser: window;
  // node tests: globalThis) rather than on this module's root, so look in
  // both places — lazily, so load order can never matter.
  function logModel() {
    var g = globals();
    return g.AstraLog || root.AstraLog || null;
  }

  function scrub(value) {
    var lm = logModel();
    if (lm && lm.scrub) return lm.scrub(value);
    return value === null || value === undefined ? "" : String(value);
  }
  // Kinds the Activity Log's category heuristic files under "system" (and
  // therefore drops as noise) but which ARE real chat operations: the shared
  // Terminal capability emits terminal.started/output/completed/failed, and
  // the chat turn itself is the root of everything below it. Same events,
  // same mapping — only the shared noise filter is bypassed for them.
  var LOG_NOISE_EXEMPT = /^(terminal\.|chat\.pipeline\.)/;

  function isMeaningful(event) {
    var lm = logModel();
    if (!lm || !lm.isMeaningful) return true;   // no model available: keep all
    if (lm.isMeaningful(event)) return true;
    var kind = String(event && event.kind ? event.kind : "");
    return LOG_NOISE_EXEMPT.test(kind);
  }
  function fmtMs(ms) {
    var lm = logModel();
    if (lm && lm.fmtMs) return lm.fmtMs(ms);
    var n = Number(ms);
    if (ms === null || ms === undefined || ms === "" || isNaN(n)) return "";
    return n >= 1000 ? (n / 1000).toFixed(1) + "s" : Math.round(n) + "ms";
  }
  function elapsedMs(a, b) {
    var lm = logModel();
    if (lm && lm.elapsedMs) return lm.elapsedMs(a, b);
    return null;
  }

  /* ---------------------------------------------------------------- text ---
   * Everything that reaches the status line is a *string* pulled from a small
   * whitelist of fields, scrubbed and collapsed to one line. Raw payloads
   * (JSON tool output, stdout/stderr, request ids) are never rendered. */
  function clip(value, max) {
    var s = value === null || value === undefined ? "" : String(value);
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }
  function oneLine(value, max) {
    var s = scrub(value).replace(/\s+/g, " ").trim();
    // A reason is often "Error: {...}" — keep the human head, drop the payload.
    var brace = s.indexOf("{");
    if (brace > 0) s = s.slice(0, brace).trim();
    return clip(s, max || REASON_MAX).trim();
  }
  // The provider ids come from the adapters (see astra/ai/adapters/*.py);
  // the chat line shows their real brand, never the raw config key.
  var BRAND = {
    openrouter: "OpenRouter", zai: "ZAI", sambanova: "SambaNova",
    groq: "Groq", gemini: "Gemini", bedrock: "Bedrock",
    cloudflare: "Cloudflare", cerebras: "Cerebras", cohere: "Cohere",
    mistral: "Mistral", openai: "OpenAI", anthropic: "Anthropic",
    ollama: "Ollama", local: "the local model",
  };

  function branded(name) {
    var s = String(name === null || name === undefined ? "" : name).trim();
    if (!s) return "";
    if (BRAND[s.toLowerCase()]) return BRAND[s.toLowerCase()];
    if (/[A-Z]/.test(s)) return s;                  // already branded
    return s.split(/[-_.\s]+/).map(function (w) {
      return w ? w.charAt(0).toUpperCase() + w.slice(1) : "";
    }).join(" ");
  }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  /* ------------------------------------------------------- tool -> status --
   * The Agent Tool Loop decides which tool to call; this only says what the
   * call IS in human words. Unknown tools fall back to their own name — never
   * to an internal protocol id. */
  var TOOL_LABELS = {
    read_file: "Reading file",
    list_files: "Listing files",
    search_files: "Searching files",
    write_file: "Editing file",
    generate_document: "Writing document",
    search_web: "Searching the web",
    fetch_url: "Reading webpage",
    remember: "Saving to memory",
    recall: "Checking memory",
    search_memory: "Checking memory",
    create_task: "Creating task",
    list_tasks: "Listing tasks",
    ai_generate: "Asking another model",
    execution_history_read: "Reviewing past actions",
  };
  var TOOL_PREFIX = [
    [/^terminal_(exec|start|status|stop|kill|history|sessions|close|output_read)/, "Running terminal command"],
    [/^runtime_command$/, "Running terminal command"],
    [/^runtime_(status|start|stop|restart|reset|destroy)$/, "Managing the Agent Runtime"],
    [/^browser_(open|navigate)/, "Opening webpage"],
    [/^browser_(observe|read|content_read|extract|screenshot|action|close)/, "Reading webpage"],
    [/^tx_|^token_balance$|^chain_status$|^rpc_status$|^wallet_balances$/, "Checking blockchain data"],
    [/^(read|list|search)_file/, "Reading file"],
    [/^(write|edit|apply|patch)/, "Editing file"],
    [/^git_/, "Working with git"],
    [/^workflow_/, "Running workflow"],
    [/^memory_/, "Checking memory"],
  ];

  function labelForTool(tool) {
    var name = String(tool === null || tool === undefined ? "" : tool).trim();
    if (!name) return "Using a tool";
    if (TOOL_LABELS[name]) return TOOL_LABELS[name];
    for (var i = 0; i < TOOL_PREFIX.length; i++) {
      if (TOOL_PREFIX[i][0].test(name)) return TOOL_PREFIX[i][1];
    }
    return "Using " + branded(name.replace(/[._]+/g, " ")) + " tool";
  }

  /* ------------------------------------------------------------ mapping ----
   * One row per event kind: the human status line, the timeline step it
   * belongs to, its phase and the correlation key its start/terminal pair
   * shares. `root` marks the events that open/close the whole chat turn;
   * `failure` marks an operation that failed (a failing terminal command does
   * NOT end the turn — the agent loop normally keeps going and fixes it, so
   * only a root failure turns the whole status into "Failed"). */
  var M = {
    /* request lifecycle -------------------------------------------------- */
    "chat.pipeline.started": { text: "Understanding your request",
      step: "Understanding request", phase: "start", key: "op", seize: true },
    "chat.pipeline.assigned": { text: "Selecting AI provider", phase: "event",
      key: "op" },
    "chat.pipeline.verified": { text: "Reviewing results", phase: "event",
      key: "op" },
    "chat.pipeline.verify_error": { text: "Reviewing results",
      phase: "terminal", key: "op", root: true },
    "chat.pipeline.tool_loop_error": { text: "Reviewing results",
      phase: "update", key: "op" },
    "chat.pipeline.understand_failed": { text: "Selecting AI provider",
      phase: "event", key: "op" },
    "chat.pipeline.finished": { text: "", phase: "terminal", key: "op",
      root: true },
    "chat.pipeline.failed": { text: "Request failed", phase: "terminal",
      key: "op", root: true, failure: true, reason: "error" },

    /* gateway lifecycle -------------------------------------------------- */
    "astra_gateway.request": { text: "Selecting AI provider",
      step: "Selecting AI provider", phase: "start", key: "trace" },
    "astra_gateway.success": { text: "Provider response ready",
      phase: "terminal", key: "trace" },
    "astra_gateway.error": { text: "Provider call failed", phase: "terminal",
      key: "trace", failure: true },
    "astra_gateway.stream_interrupted": { text: "Provider call interrupted",
      phase: "terminal", key: "trace", failure: true },
    "provider.selected": { text: "Selecting AI provider",
      step: "Selecting AI provider", phase: "start", key: "op" },
    "provider.health_changed": { text: "Checking provider health",
      phase: "update", key: "op" },
    "router.request": { text: "Selecting AI provider",
      step: "Selecting AI provider", phase: "start", key: "op" },
    "router.decision": { text: "Selecting AI provider", phase: "terminal",
      key: "op" },
    "router.retry": { text: "Trying another AI provider", phase: "event",
      key: "op" },
    "router.fallback": { text: "Trying another AI provider", phase: "event",
      key: "op" },
    "credential.rotation": { text: "Switching to another API key",
      phase: "update", key: "op" },
    "router.gateway_supervision": { text: "Reviewing results", phase: "event",
      key: "op" },
    "router.gateway_task_completion": { text: "Reviewing results",
      phase: "event", key: "op" },

    /* provider lifecycle (the model call itself) -------------------------- */
    "ai.started": { text: "Thinking with {provider}", phase: "start",
      key: "op", dynamic: "provider_thinking" },
    "ai.completed": { text: "Reviewing provider response", phase: "terminal",
      key: "op" },
    "ai.failed": { text: "Provider call failed", phase: "terminal",
      key: "op", failure: true },
    "ai.token": null,

    /* supervision / correction (Gateway verifying a provider's answer) ---- */
    "supervision.correction_requested": { text: "Reviewing results",
      phase: "event", key: "op" },
    "supervision.correction_succeeded": { text: "Reviewing results",
      phase: "update", key: "op" },
    "supervision.validation_failed": { text: "Reviewing results",
      phase: "update", key: "op" },
    "supervision.correction_exhausted": { text: "Verification failed",
      phase: "terminal", key: "op", failure: true },
    "gateway.supervision.correction_requested": { text: "Reviewing results",
      phase: "event", key: "op" },
    "gateway.supervision.correction_succeeded": { text: "Reviewing results",
      phase: "update", key: "op" },
    "gateway.supervision.correction_failed": { text: "Verification failed",
      phase: "terminal", key: "op", failure: true },
    "gateway.supervision.correction_exhausted": { text: "Verification failed",
      phase: "terminal", key: "op", failure: true },
    "gateway.task_completion.correction_requested": { text: "Reviewing results",
      phase: "event", key: "op" },
    "gateway.task_completion.correction_succeeded": { text: "Reviewing results",
      phase: "update", key: "op" },
    "gateway.task_completion.correction_failed": { text: "Verification failed",
      phase: "terminal", key: "op", failure: true },
    "gateway.task_completion.correction_exhausted": { text: "Verification failed",
      phase: "terminal", key: "op", failure: true },
    "gateway.execution_completed": { text: "Reviewing results",
      phase: "update", key: "op" },
    "gateway.execution_recovered": { text: "Trying another AI provider",
      phase: "update", key: "op" },
    "gateway.execution_failed": { text: "Provider call failed",
      phase: "terminal", key: "op", failure: true },
    "gateway.target_cooldown": { text: "Trying another AI provider",
      phase: "update", key: "op" },

    /* agent tool loop ----------------------------------------------------- */
    "agent.tool_loop.started": { text: "Working on your request",
      step: "Working on your request", phase: "start", key: "op" },
    "agent.tool_call": { text: "", phase: "progress", key: "op_step",
      dynamic: "tool_call" },
    "agent.tool_result": { text: "Reviewing results", phase: "progress",
      key: "op_step" },
    "agent.tool_loop.step": { text: "Reviewing results", phase: "progress",
      key: "op_step" },
    "agent.tool_loop.finished": { text: "Reviewing results", phase: "update",
      key: "op" },
    "agent.tool_loop.failed": { text: "Working on your request failed",
      phase: "terminal", key: "op", failure: true },
    "agent.started": { text: "Working on your request", phase: "start",
      key: "op" },
    "agent.thinking": { text: "Understanding your request", phase: "update",
      key: "op" },
    "agent.planning": { text: "Planning the steps", phase: "update",
      key: "op" },
    "agent.completed": { text: "Reviewing results", phase: "terminal",
      key: "op" },
    "agent.failed": { text: "Working on your request failed",
      phase: "terminal", key: "op", failure: true },

    /* tool / file / browser / terminal lifecycle -------------------------- */
    "tool.started": { text: "", phase: "start", key: "op", dynamic: "tool" },
    "tool.completed": { text: "Reviewing results", phase: "terminal",
      key: "op" },
    "tool.failed": { text: "Tool failed", phase: "terminal", key: "op",
      failure: true },
    "terminal.started": { text: "Running terminal command", phase: "start",
      key: "proc" },
    "terminal.output": { text: "Running terminal command", phase: "progress",
      key: "proc" },
    "terminal.completed": { text: "Terminal command completed",
      phase: "terminal", key: "proc" },
    "terminal.failed": { text: "Terminal command failed", phase: "terminal",
      key: "proc", failure: true },
    "terminal.timeout": { text: "Terminal command timed out",
      phase: "terminal", key: "proc", failure: true },
    "terminal.stopped": { text: "Terminal command stopped", phase: "terminal",
      key: "proc" },
    "browser.opened": { text: "Opening webpage", phase: "start", key: "op" },
    "browser.navigation": { text: "Opening webpage", phase: "start",
      key: "op" },
    "browser.action": { text: "Reading webpage", phase: "progress", key: "op" },
    "browser.error": { text: "Webpage could not be read", phase: "terminal",
      key: "op", failure: true },

    /* web3 lifecycle ------------------------------------------------------ */
    "web3.transaction.prepared": { text: "Checking blockchain data",
      phase: "start", key: "tx" },
    "web3.transaction.submitted": { text: "Checking blockchain data",
      phase: "progress", key: "tx" },
    "web3.transaction.broadcast": { text: "Checking blockchain data",
      phase: "progress", key: "tx" },
    "web3.transaction.confirmed": { text: "Transaction confirmed",
      phase: "terminal", key: "tx" },
    "web3.transaction.rejected": { text: "Transaction rejected",
      phase: "terminal", key: "tx", failure: true },
    "web3.transaction.failed": { text: "Transaction failed", phase: "terminal",
      key: "tx", failure: true },

    /* workflow / task lifecycle ------------------------------------------ */
    "workflow.started": { text: "Running workflow", phase: "start",
      key: "run" },
    "workflow.completed": { text: "Workflow completed", phase: "terminal",
      key: "run" },
    "workflow.failed": { text: "Workflow failed", phase: "terminal",
      key: "run", failure: true },
    "task.started": { text: "Running workflow step", phase: "start",
      key: "task" },
    "task.completed": { text: "Workflow step completed", phase: "terminal",
      key: "task" },
    "task.done": { text: "Workflow step completed", phase: "terminal",
      key: "task" },
    "task.failed": { text: "Workflow step failed", phase: "terminal",
      key: "task", failure: true },
    "task.cancelled": { text: "Workflow step cancelled", phase: "terminal",
      key: "task" },
    "agent.step.started": { text: "Working on your request", phase: "start",
      key: "op_step" },
    "agent.step.completed": { text: "Reviewing results", phase: "terminal",
      key: "op_step" },
    "agent.step.failed": { text: "Step failed", phase: "terminal",
      key: "op_step", failure: true },

    /* deliberately NOT part of the compact chat status -------------------- */
    "scheduler.tick": null,
    "memory.saved": null,
    "memory.recalled": null,
    "experience.learned": null,
    "plugin.loaded": null,
    "plugin.failed": null,
    "plugin.disabled": null,
    "operation.interrupted": null,
  };

  /* What the engine does next, when (and only when) it is deterministic from
   * the operation that is currently running — this is the "○ next step" row.
   * It is never a timer and never guessed for an unknown operation. */
  var NEXT_STEP = [
    [/^terminal_/, "Reviewing terminal output"],
    [/^tx_|web3/, "Reviewing result"],
    [/^browser_/, "Reviewing webpage"],
    [/^(read|list|search)_file/, "Reviewing result"],
  ];

  function nextStepFor(s) {
    if (s.state !== STATE.WORKING) return "";
    var active = null;
    for (var i = 0; i < s.steps.length; i++) {
      if (s.steps[i].state === STEP.ACTIVE) active = s.steps[i];
    }
    if (!active || !active.tool) return "";
    for (var j = 0; j < NEXT_STEP.length; j++) {
      if (NEXT_STEP[j][0].test(active.tool)) return NEXT_STEP[j][1];
    }
    return "";
  }

  /* ------------------------------------------------------- correlation ----- */
  function idsOf(data) {
    var d = data || {};
    return {
      op: d.op ? String(d.op) : "",
      trace: d.trace ? String(d.trace) : "",
      request: d.request ? String(d.request) : "",
      run_id: d.run_id === null || d.run_id === undefined ? "" : String(d.run_id),
      tx: d.tx ? String(d.tx) : "",
      process_id: d.process_id ? String(d.process_id) : "",
      tool: d.tool ? String(d.tool) : "",
      step: d.step === null || d.step === undefined ? "" : String(d.step),
    };
  }

  function keyOf(spec, ids, kind) {
    var op = ids.op;
    switch (spec.key) {
      case "op_step": return (op || ids.trace) + ":" + ids.step;
      case "proc": return ids.process_id ? "proc:" + ids.process_id
                                         : (op ? "op:" + op : "kind:" + kind);
      case "tx": return ids.tx ? "tx:" + ids.tx
                               : (op ? "op:" + op : "kind:" + kind);
      case "tool": return "tool:" + ids.tool + (op ? "@" + op : "");
      case "trace": return ids.trace ? "trace:" + ids.trace : "kind:" + kind;
      case "run": return ids.run_id ? "run:" + ids.run_id : "kind:" + kind;
      case "task": return ids.run_id ? "task:" + ids.run_id : "kind:" + kind;
      default: return op ? "op:" + op
                         : (ids.trace ? "trace:" + ids.trace : "kind:" + kind);
    }
  }

  // Does this event belong to the turn the tracker is currently showing?
  //   true  -> yes (correlated by request/trace/op/run_id)
  //   false -> no (correlated to a DIFFERENT turn: stale, ignore)
  //   null  -> no correlation ids at all (terminal/browser/web3 events today
  //            carry their own op or none) — the caller applies the recency
  //            guard instead.
  function ownedBy(s, ids) {
    var req = ids.request || ids.trace;
    if (!req && !ids.op && !ids.run_id) return null;
    if (!s.request) {
      if (req) { s.request = req; return true; }      // adopt on first sight
      if (ids.op && ids.op.indexOf("chat:") === 0) {
        s.request = ids.op.slice(5);
        return true;
      }
      return null;
    }
    if (req && req === s.request) return true;
    if (ids.op === "chat:" + s.request) return true;
    if (ids.run_id && s.run_id && ids.run_id === s.run_id) return true;
    if (s.trace && ids.trace && ids.trace === s.trace) return true;
    if (!req && ids.op && ids.op.indexOf("chat:") === 0) return false;
    if (req) return false;                            // another turn entirely
    return null;
  }

  /* ------------------------------------------------------------- tracker --- */
  function newState() {
    return {
      state: STATE.IDLE,
      request: "", trace: "", run_id: "",
      current: "",
      steps: [],
      failure: "",
      startedTs: "",          // first backend timestamp seen this turn
      endedTs: "",            // backend timestamp of the root terminal
      startedAt: 0,           // real clock fallback for the duration only
      endedAt: 0,
      lastId: null,
      seen: {},
      seenCount: 0,
    };
  }

  function lastStep(s) { return s.steps.length ? s.steps[s.steps.length - 1] : null; }

  function activeStepByLabel(s, label) {
    if (!label) return null;
    for (var i = s.steps.length - 1; i >= 0; i--) {
      var st = s.steps[i];
      if (st.state === STEP.ACTIVE && st.label === label) return st;
    }
    return null;
  }

  function activeStep(s, key) {
    if (!key) return null;
    for (var i = s.steps.length - 1; i >= 0; i--) {
      var st = s.steps[i];
      if (st.state !== STEP.ACTIVE) continue;
      if (st.keys.indexOf(key) >= 0) return st;
    }
    return null;
  }

  // Lazily attach another lifecycle key to an already-active step: the same
  // real operation reports itself twice (tool.started with the registry op,
  // then terminal.started with the process id) and must stay ONE step.
  function mergeStepKey(step, key) {
    if (key && step.keys.indexOf(key) < 0) step.keys.push(key);
    return step;
  }

  function stepDuration(step, ts) {
    // An explicit duration on the terminal event is authoritative (the same
    // rule the Activity Log uses); the backend timestamps are only second
    // resolution, so they are the fallback, then the real clock.
    if (step.durationMs !== null && step.durationMs !== undefined && step.durationMs !== "") {
      return fmtMs(step.durationMs);
    }
    if (step.doneTs && step.startTs) {
      var ms = elapsedMs(step.startTs, step.doneTs);
      if (ms !== null && ms >= 0) return fmtMs(ms);
    }
    if (step.startAt && step.doneAt) {
      return fmtMs(Math.max(0, step.doneAt - step.startAt));
    }
    return "";
  }

  function pushStep(s, step) {
    // Same label still running (e.g. two views of one terminal command) is
    // the same step, not a second one.
    var last = lastStep(s);
    if (last && last.state === STEP.ACTIVE && last.label === step.label) {
      mergeStepKey(last, step.keys[0]);
      if (step.tool) last.tool = step.tool;
      return last;
    }
    s.steps.push(step);
    if (s.steps.length > MAX_STEPS) s.steps.splice(0, s.steps.length - MAX_STEPS);
    return step;
  }

  function closeSteps(s, ts, at) {
    s.steps.forEach(function (st) {
      if (st.state === STEP.ACTIVE) {
        st.state = STEP.DONE;
        st.doneTs = ts || "";
        st.doneAt = at || Date.now();
        st.duration = stepDuration(st, ts);
      }
    });
  }

  function durationText(s) {
    if (s.startedTs && s.endedTs && s.endedTs !== s.startedTs) {
      var ms = elapsedMs(s.startedTs, s.endedTs);
      if (ms !== null && ms >= 0) return fmtMs(ms);
    }
    if (s.startedAt && s.endedAt) return fmtMs(Math.max(0, s.endedAt - s.startedAt));
    return "";
  }

  function stepCount(s) { return s.steps.length; }

  function summaryFor(s) {
    if (s.state === STATE.WORKING) return "Working…";
    if (s.state === STATE.COMPLETED) {
      var parts = ["✓ Completed", plural(stepCount(s), "step")];
      var d = durationText(s);
      if (d) parts.push(d);
      return parts.join(" · ");
    }
    if (s.state === STATE.FAILED) {
      return s.failure ? "✕ Failed · " + s.failure : "✕ Failed";
    }
    return "";
  }

  function snapshotOf(s) {
    var steps = s.steps.map(function (st) {
      return { label: st.label, state: st.state, duration: st.duration || "" };
    });
    var nxt = nextStepFor(s);
    if (nxt) steps.push({ label: nxt, state: STEP.PENDING, key: "", duration: "" });
    return {
      state: s.state,
      summary: summaryFor(s),
      current: s.state === STATE.FAILED && s.failure ? s.failure : s.current,
      steps: steps,
      failure: s.failure,
      stepCount: stepCount(s),
      duration: durationText(s),
      request: s.request,
      panelTitle: s.state === STATE.WORKING ? "Astra is working"
                                            : "Execution steps",
      updated: s.lastId,
    };
  }

  function createTracker() {
    var s = newState();

    function reset() { s = newState(); return s; }

    /* The composer opens the indicator the moment the user hits send: from
     * then on the line reads only "Working…" until the first real lifecycle
     * event names the operation. No invented operation, no rotation. */
    function begin() {
      if (s.state !== STATE.WORKING) {
        s = newState();
        s.state = STATE.WORKING;
        s.startedAt = Date.now();
      }
      return snapshotOf(s);
    }

    /* The reply arrived over HTTP (the turn really finished); the SSE
     * terminal may still be in flight, so close the status out here using the
     * steps that were really observed. */
    function complete() {
      if (s.state === STATE.WORKING) {
        s.state = STATE.COMPLETED;
        s.endedAt = Date.now();
        closeSteps(s, "", s.endedAt);
        s.current = "";
      }
      return snapshotOf(s);
    }

    function fail(reason) {
      if (s.state !== STATE.FAILED) {
        s.state = STATE.FAILED;
        s.endedAt = Date.now();
        closeSteps(s, "", s.endedAt);
      }
      var why = oneLine(reason, REASON_MAX);
      if (why) s.failure = why;
      return snapshotOf(s);
    }

    /* Feed one live event (the same object the Activity Log receives). */
    function apply(event) {
      if (!event || event.id === null || event.id === undefined) return snapshotOf(s);
      var id = String(event.id);
      if (s.seen[id]) return snapshotOf(s);
      if (!isMeaningful(event)) return snapshotOf(s);

      var kind = String(event.kind === null || event.kind === undefined ? "" : event.kind);
      var data = (event.data && typeof event.data === "object") ? event.data : {};
      var spec = Object.prototype.hasOwnProperty.call(M, kind) ? M[kind] : undefined;
      if (spec === null) return snapshotOf(s);                 // explicitly ignored
      if (!spec) spec = fallbackSpec(kind, data);               // unknown kind
      if (!spec) return snapshotOf(s);

      markSeen(s, id);

      var ids = idsOf(data);
      var phase = spec.phase || "event";
      var key = keyOf(spec, ids, kind);

      // A root start opens a fresh turn (seizing the tracker from whatever
      // was displayed before).
      if (spec.root && spec.seize) {
        var req = ids.request || ids.trace ||
                  (ids.op.indexOf("chat:") === 0 ? ids.op.slice(5) : "");
        var seen = s.seen, seenCount = s.seenCount;
        s = newState();
        s.seen = seen;              // keep de-dupe across a turn reset
        s.seenCount = seenCount;
        s.state = STATE.WORKING;
        s.request = req;
        s.trace = ids.trace || req;
        s.startedAt = Date.now();
        s.startedTs = stampOf(event);
        s.run_id = ids.run_id;
      } else if (s.state === STATE.IDLE) {
        return snapshotOf(s);            // nothing is running: ignore
      } else if (s.state !== STATE.WORKING) {
        // already finished: only a new turn (root start, handled above) or a
        // correlated terminal for the SAME turn may touch the status.
        if (!(spec.root || ownerTrue(s, ids))) return snapshotOf(s);
      }

      var owned = ownedBy(s, ids);
      if (owned === false) return snapshotOf(s);        // stale: another turn
      if (owned === null) {
        // Uncorrelated (terminal.*/browser.*/web3.* carry no request id).
        // Accept only while this turn is still running AND strictly newer
        // than everything already applied — an out-of-order replay of an
        // older terminal event can therefore never overwrite the current
        // operation or resurrect a finished one.
        if (s.state !== STATE.WORKING) return snapshotOf(s);
        if (s.lastId !== null && event.id < s.lastId) return snapshotOf(s);
      }

      if (ids.run_id) s.run_id = ids.run_id;
      if (spec.root && spec.seize) s.request = ids.request || ids.trace || s.request;
      if (!s.startedTs) s.startedTs = stampOf(event);

      var at = Date.now();
      var ts = stampOf(event);
      var text = specText(spec, data, ids);

      if (phase === "start") {
        var label = spec.step || text;
        var active = activeStep(s, key);
        if (active) {
          mergeStepKey(active, key);      // same operation reporting again
          if (label) active.label = label;
          if (ids.tool) active.tool = ids.tool;
        } else {
          // The SAME real operation often reports itself twice (tool.started
          // with the registry op, then terminal.started with the process id):
          // one operation stays one step, so a still-running step with this
          // label absorbs the new key instead of forking a duplicate.
          var same = activeStepByLabel(s, label);
          if (same) {
            mergeStepKey(same, key);
            if (ids.tool) same.tool = ids.tool;
          } else if (label) {
            // A new operation starts: the one before it is finished. Only a
            // real start creates a step — a completion/progress event can
            // never invent one.
            closeSteps(s, ts, at);
            pushStep(s, {
              keys: [key], label: label, tool: ids.tool || "",
              state: STEP.ACTIVE, startTs: ts, startAt: at,
              doneTs: "", doneAt: 0, durationMs: data.duration_ms,
              duration: "",
            });
          }
        }
        if (text) s.current = text;
      } else if (phase === "progress") {
        if (text) s.current = text;
      } else if (phase === "terminal") {
        var step = activeStep(s, key);
        if (!step && s.lastId !== null && event.id < s.lastId) {
          return snapshotOf(s);          // stale terminal for an older op
        }
        if (step) {
          step.state = spec.failure ? STEP.FAILED : STEP.DONE;
          step.doneTs = ts;
          step.doneAt = at;
          if (data.duration_ms !== null && data.duration_ms !== undefined)
            step.durationMs = data.duration_ms;
          step.duration = stepDuration(step, ts);
        }
        if (spec.failure) {
          var why = oneLine(reasonOf(spec, data), REASON_MAX);
          if (why) s.failure = why;
          if (!text) text = s.failure;
        } else if (!spec.root) {
          s.failure = "";                 // the failure was recovered from
        }
        if (text) s.current = text;
        if (spec.root) {
          s.state = spec.failure ? STATE.FAILED : STATE.COMPLETED;
          s.endedAt = at;
          s.endedTs = ts;
          if (spec.failure && !s.failure) s.failure = text || "something went wrong";
          closeSteps(s, ts, at);
          s.current = "";
        }
      } else {
        // event/update: refine the live line only. The step timeline is driven
        // exclusively by real start events, never by a completion's wording.
        if (text) s.current = text;
      }

      s.lastId = s.lastId === null ? event.id : Math.max(s.lastId, event.id);
      s.updatedAt = at;
      return snapshotOf(s);
    }

    function ownerTrue(ss, ids) {
      var req = ids.request || ids.trace;
      return !!(ss.request && req && req === ss.request);
    }

    return {
      begin: begin,
      apply: apply,
      complete: complete,
      fail: fail,
      reset: reset,
      snapshot: function () { return snapshotOf(s); },
      STATE: STATE,
      STEP: STEP,
    };
  }

  /* ------------------------------------------------------- execution cards --
   * Claude-style execution cards: ONE card per REAL tool execution, rendered
   * underneath the compact live status. A terminal command is exactly one
   * card — `terminal.started` opens it, `terminal.output` only ever updates
   * that same card, and `terminal.completed`/`failed`/`timeout`/`stopped`
   * closes it. Several `terminal_exec` calls are several cards, never merged
   * into one timeline row.
   *
   * Like the status model this is DOM-free: astra.js paints the cards from
   * these snapshots, so the correlation, ordering, stale-event and redaction
   * rules are unit-testable under node. It consumes the SAME lifecycle events
   * (no second event system, no timers, no simulated progress). */

  var CARD = { RUNNING: "running", DONE: "completed", FAILED: "failed",
               STOPPED: "stopped" };
  var CARD_KIND = { TERMINAL: "terminal", BROWSER: "browser", FILE: "file",
                    WEB: "web", WEB3: "web3", TOOL: "tool" };

  var MAX_CARDS = 50;         // bounded: a long tool loop must not grow forever
  var PREVIEW_MAX = 140;      // one short, scrubbed line — never raw output
  var CMD_MAX = 200;

  // Tools whose execution is real but whose card would only add noise (they
  // introspect other operations, or are pure bookkeeping). Terminal EXECUTION
  // tools (exec/start) are cards; terminal *status* tools are not — the card
  // for the process they name already exists.
  // `host_terminal_request` executes NOTHING — it only records an approval
  // request (the Allow/Deny card is rendered by the chat, not a terminal
  // execution card), so it must not open one.
  var CARD_SKIP = /^(recall|remember|search_memory|create_task|list_tasks|update_task|get_health|list_tools|check_health|execution_history_read|terminal_status|terminal_history|terminal_history_read|terminal_sessions|terminal_output_read|terminal_stop|terminal_kill|runtime_status|runtime_package_manager_detect|list_workflows|ai_generate|host_terminal_request)$/;
  // Only the tools that really spawn a command open a "pending" card that the
  // following terminal.started attaches to. `runtime_command` is the Agent's
  // shell surface — it runs a real command in the isolated Agent Runtime and
  // emits the same terminal.started/output/completed lifecycle, so it must
  // fold into ONE card exactly like the legacy host terminal_exec did.
  var TERMINAL_PENDING = /^(terminal_(exec|start)|runtime_command)$/;
  // Same events the status model exempts from the Activity Log's noise filter.
  var CARD_NOISE_EXEMPT = /^(terminal\.|tool\.|chat\.pipeline\.)/;

  /* Human words, never an internal id or a protocol payload. */
  function plain(value, max) {
    var s = scrub(value).replace(/\s+/g, " ").trim();
    return clip(s, max || PREVIEW_MAX).trim();
  }
  function basenameOf(value) {
    var s = String(value || "").replace(/["']/g, "");
    var parts = s.split(/[\\/]/);
    for (var i = parts.length - 1; i >= 0; i--) {
      if (parts[i]) return parts[i];
    }
    return "";
  }
  function fileArgOf(tokens) {
    for (var i = tokens.length - 1; i >= 1; i--) {
      var t = tokens[i];
      if (!t || t.charAt(0) === "-") continue;
      if (/[\\/.]/.test(t)) return basenameOf(t);
    }
    return "";
  }
  function kindForTool(tool) {
    var name = String(tool || "");
    // `runtime_*` shell/lifecycle work is the Agent Runtime's terminal
    // surface — it renders as a terminal execution card, the same as the
    // legacy host `terminal_*` tools always did.
    if (/^terminal_/.test(name) || /^runtime_(command|start|stop|kill|status|restart)$/.test(name)) return CARD_KIND.TERMINAL;
    if (/^browser_/.test(name)) return CARD_KIND.BROWSER;
    if (/^(read|list|search|write|edit|apply|patch)_file|^generate_document/.test(name)) return CARD_KIND.FILE;
    if (/^search_web$|^fetch_url$/.test(name)) return CARD_KIND.WEB;
    if (/^tx_|^token_balance$|^chain_status$|^rpc_status$|^wallet_balances$/.test(name)) return CARD_KIND.WEB3;
    return CARD_KIND.TOOL;
  }

  /* A terminal command, said in human words. Deterministic: derived only from
   * the real command string — never a guess, never a rotation. */
  var TEST_CMD = /\b(pytest|py\.test|jest|vitest|mocha|rspec|tox|unittest|nose2|npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|bun\s+test|cargo\s+test|go\s+test|make\s+(test|check)|gradle\s+test|mvn\s+test|ctest)\b/i;
  function terminalAction(command) {
    var cmd = plain(command, CMD_MAX);
    if (!cmd) return "Running terminal command";
    var head = cmd.replace(/^(?:sudo|env)\s+/, "");
    var first = head.split(/\s*(?:&&|\|\||[;|]|\n)\s*/)[0].trim();
    var tokens = first.split(/\s+/).filter(function (t) { return t; });
    if (!tokens.length) return "Running terminal command";
    if (TEST_CMD.test(first)) return "Run tests";
    var bin = basenameOf(tokens[0]);
    if (/^(cat|head|tail|less|more|bat|nl|wc|type)$/.test(bin)) {
      var f = fileArgOf(tokens);
      return f ? "Read " + f : "Read file";
    }
    if (/^(sed|awk)$/.test(bin)) {
      var sf = fileArgOf(tokens);
      var editing = tokens.indexOf("-i") >= 0 ||
                    /^-i/.test(tokens.slice(1).join(" "));
      if (editing) return sf ? "Edit " + sf : "Edit file";
      return sf ? "Read " + sf : "Read file";
    }
    if (/^(ls|ll|dir|tree|find|fd|du|df)$/.test(bin)) return "List files";
    if (/^(grep|rg|ag|ack|egrep|fgrep)$/.test(bin)) return "Search files";
    if (bin === "git") {
      var sub = tokens[1] || "";
      if (sub === "status") return "Check git status";
      if (sub === "diff" || sub === "show") return "Review git diff";
      if (sub === "log") return "Review git log";
      if (sub === "add" || sub === "commit" || sub === "stash") return "Commit changes";
      if (sub === "push" || sub === "pull" || sub === "fetch") return "Sync with remote";
      if (sub === "checkout" || sub === "switch" || sub === "branch") return "Switch branch";
      return "Run git command";
    }
    if (/^(pip|pip3|npm|yarn|pnpm|bun|apt|apt-get|pkg|cargo|go|gem|composer|poetry)$/.test(bin) &&
        /^(install|add|get|upgrade|update|ci)$/.test(tokens[1] || "")) {
      return "Install packages";
    }
    if (/^(curl|wget|http|https)$/.test(bin)) return "Fetch URL";
    if (/^(rm|rmdir|unlink)$/.test(bin)) {
      var rf = fileArgOf(tokens);
      return rf ? "Delete " + rf : "Delete file";
    }
    if (/^(mkdir|touch|cp|mv|ln)$/.test(bin)) {
      var cf = fileArgOf(tokens);
      return cf ? "Create " + cf : "Create file";
    }
    if (/^(make|ninja)$/.test(bin)) return "Build project";
    if (/^(python3?|node|deno|bun|ruby|perl|php|java|go|rustc|bash|sh|zsh)$/.test(bin)) {
      var script = fileArgOf(tokens);
      return script ? "Run " + script : "Run script";
    }
    if (/^(echo|printf|tee)$/.test(bin) && first.indexOf(">") >= 0) return "Write file";
    return "Run command";
  }

  function cardStateText(c) {
    if (c.state === CARD.RUNNING) return "Running";
    if (c.state === CARD.FAILED) {
      return c.status === "timeout" ? "Timed out" : "Failed";
    }
    if (c.state === CARD.STOPPED) return "Stopped";
    if (c.status === "ended") return "Ended";
    return "Completed";
  }

  function cardSnapshot(c) {
    return {
      id: c.id,
      kind: c.kind,
      title: c.title,
      command: c.command,
      cwd: c.cwd,
      state: c.state,
      status: c.status,
      stateText: cardStateText(c),
      exitCode: c.exitCode,
      durationMs: c.durationMs,
      duration: c.state === CARD.RUNNING ? "" : fmtMs(c.durationMs),
      preview: c.preview,
      tool: c.tool,
      op: c.op,
      processId: c.process_id,
      blobId: c.blob_out,
      blobErr: c.blob_err,
      startedId: c.startId,
      updatedId: c.updateId,
    };
  }

  function createCardTracker() {
    var cards = [];
    var byKey = {};
    var pending = [];
    var seen = {};
    var seenCount = 0;
    var lastId = null;
    var active = false;
    var seq = 0;

    function reset() {
      cards = []; byKey = {}; pending = []; lastId = null;
      active = false; seq = 0;
      return list();
    }

    function list() { return cards.map(cardSnapshot); }

    function index(c) {
      byKey[c.id] = c;
      if (c.op) byKey["op:" + c.op] = c;
      if (c.process_id) byKey["proc:" + c.process_id] = c;
      if (c.toolOp) byKey["tool:" + c.toolOp] = c;
    }

    function newCard(kind, title) {
      seq += 1;
      var c = {
        id: "c" + seq, kind: kind, title: title || "",
        command: "", cwd: "", state: CARD.RUNNING, status: "running",
        exitCode: null, durationMs: null, preview: "", tool: "", op: "",
        process_id: "", blob_out: "", blob_err: "", toolOp: "",
        startId: null, updateId: null,
      };
      cards.push(c);
      if (cards.length > MAX_CARDS) cards.shift();
      index(c);
      return c;
    }

    function dropPending(c) {
      var i = pending.indexOf(c);
      if (i >= 0) pending.splice(i, 1);
    }

    /* The command that just finished reports its real duration in `duration`
     * (ms) — that is the authoritative value the card shows. */
    function durationOf(data) {
      if (data.duration_ms !== undefined && data.duration_ms !== null) {
        return data.duration_ms;
      }
      if (data.duration !== undefined && data.duration !== null) {
        return data.duration;
      }
      return null;
    }

    function applyTool(kind, data, event) {
      var tool = String(data.tool || "");
      var op = String(data.op || "");
      if (kind === "tool.started") {
        if (!tool || CARD_SKIP.test(tool)) return false;
        if (op && byKey["tool:" + op]) return true;      // replayed start
        var card = newCard(kindForTool(tool), labelForTool(tool));
        card.tool = tool;
        card.toolOp = op;
        card.op = op;
        card.startId = event.id;
        card.updateId = event.id;
        index(card);
        if (TERMINAL_PENDING.test(tool)) pending.push(card);
        return true;
      }
      var target = op ? byKey["tool:" + op] : null;
      if (!target) return false;
      if (target.updateId !== null && event.id < target.updateId) return false;
      target.updateId = event.id;
      if (kind === "tool.completed") {
        if (target.state === CARD.RUNNING) {
          target.state = CARD.DONE;
          target.status = "completed";
          var ms = durationOf(data);
          if (ms !== null) target.durationMs = ms;
        }
      } else if (target.state === CARD.RUNNING) {
        target.state = CARD.FAILED;
        target.status = "failed";
        var why = plain(data.error, PREVIEW_MAX);
        if (why) target.preview = why;
        var fms = durationOf(data);
        if (fms !== null) target.durationMs = fms;
      }
      dropPending(target);
      return true;
    }

    function applyTerminal(kind, data, event) {
      var pid = String(data.process_id || "");
      var op = String(data.op || "");
      if (kind === "terminal.started") {
        // A replay of a start we already applied must never reopen a finished
        // execution (or fork a second card for the same process id).
        if ((pid && byKey["proc:" + pid]) || (op && byKey["op:" + op])) {
          return true;
        }
        // Attach to the execution card its tool.started opened; if there is
        // none (a terminal used directly by a workflow/tool), the process id
        // alone is still a real execution and gets its own card.
        var card = pending.length ? pending.shift() : null;
        if (card) dropPending(card);
        if (!card) {
          card = newCard(CARD_KIND.TERMINAL, terminalAction(data.command));
          card.startId = event.id;
        }
        card.process_id = pid || card.process_id;
        card.op = op || card.op;
        var cmd = plain(data.command, CMD_MAX);
        if (cmd) {
          card.command = cmd;
          card.title = terminalAction(cmd);
        }
        var cwd = plain(data.cwd, CMD_MAX);
        if (cwd) card.cwd = cwd;
        if (data.stdout_blob_id) card.blob_out = String(data.stdout_blob_id);
        if (data.stderr_blob_id) card.blob_err = String(data.stderr_blob_id);
        card.state = CARD.RUNNING;
        card.status = "running";
        card.updateId = event.id;
        if (card.startId === null) card.startId = event.id;
        index(card);
        return true;
      }
      var target = (pid && byKey["proc:" + pid]) ||
                   (op && byKey["op:" + op]) || null;
      if (!target) return false;
      if (target.updateId !== null && event.id < target.updateId) return false;
      target.updateId = event.id;
      if (kind === "terminal.output") {
        var snip = plain(data.snippet, PREVIEW_MAX);
        if (snip) target.preview = snip;
        return true;
      }
      if (data.stdout_blob_id) target.blob_out = String(data.stdout_blob_id);
      if (data.stderr_blob_id) target.blob_err = String(data.stderr_blob_id);
      var ms = durationOf(data);
      if (ms !== null) target.durationMs = ms;
      if (data.exit_code !== undefined) target.exitCode = data.exit_code;
      var status = String(data.status || "");
      if (kind === "terminal.completed") {
        target.state = CARD.DONE;
        target.status = status || "completed";
      } else if (kind === "terminal.stopped") {
        target.state = CARD.STOPPED;
        target.status = "stopped";
      } else if (kind === "terminal.timeout") {
        target.state = CARD.FAILED;
        target.status = "timeout";
      } else {
        target.state = CARD.FAILED;
        target.status = status || "failed";
      }
      dropPending(target);
      return true;
    }

    /* The turn is over: nothing may still be "running" forever. A card that
     * never got its terminal event is marked Ended WITHOUT inventing a
     * duration; a later real terminal event may still refine it. */
    function endTurn() {
      cards.forEach(function (c) {
        if (c.state === CARD.RUNNING) {
          c.state = CARD.DONE;
          c.status = "ended";
        }
      });
      pending = [];
    }

    function markSeen(id) {
      seen[id] = 1;
      seenCount++;
      if (seenCount > SEEN_MAX) {
        var keys = Object.keys(seen);
        for (var i = 0; i < keys.length / 2; i++) delete seen[keys[i]];
        seenCount = Object.keys(seen).length;
      }
    }

    function isCardEvent(event) {
      if (isMeaningful(event)) return true;
      var kind = String(event && event.kind ? event.kind : "");
      return CARD_NOISE_EXEMPT.test(kind);
    }

    function apply(event) {
      if (!event || event.id === null || event.id === undefined) return list();
      var id = String(event.id);
      if (seen[id]) return list();
      var kind = String(event.kind === null || event.kind === undefined
                        ? "" : event.kind);
      var data = (event.data && typeof event.data === "object") ? event.data : {};

      if (kind === "chat.pipeline.started") {
        markSeen(id);
        lastId = event.id;
        active = true;
        cards = []; byKey = {}; pending = []; seq = 0;
        return list();
      }
      if (kind === "chat.pipeline.finished" || kind === "chat.pipeline.failed" ||
          kind === "chat.pipeline.verify_error") {
        markSeen(id);
        lastId = event.id;
        active = false;
        endTurn();
        return list();
      }
      if (!isCardEvent(event)) return list();
      if (!active) return list();          // stale: another turn owns the cards
      if (lastId !== null && event.id < lastId) {
        // An out-of-order replay may still REFINE a card it owns, but it can
        // never open a new one or resurrect a finished execution.
        var pid = String(data.process_id || "");
        var op = String(data.op || "");
        var known = (pid && byKey["proc:" + pid]) || (op && byKey["op:" + op]) ||
                    (op && byKey["tool:" + op]);
        if (!known) return list();
      }
      var handled = false;
      if (/^tool\.(started|completed|failed)$/.test(kind)) {
        handled = applyTool(kind, data, event);
      } else if (/^terminal\./.test(kind)) {
        handled = applyTerminal(kind, data, event);
      }
      if (handled) {
        markSeen(id);
        lastId = lastId === null ? event.id : Math.max(lastId, event.id);
      }
      return list();
    }

    return {
      reset: reset,
      apply: apply,
      list: list,
      endTurn: endTurn,
      size: function () { return cards.length; },
      CARD: CARD,
    };
  }

  /* --------------------------------------------------------------- helpers */
  function stampOf(event) {
    return String(event && event.created_at ? event.created_at : "").trim();
  }

  function markSeen(s, id) {
    s.seen[id] = 1;
    s.seenCount++;
    if (s.seenCount > SEEN_MAX) {
      var keys = Object.keys(s.seen);
      for (var i = 0; i < keys.length / 2; i++) delete s.seen[keys[i]];
      s.seenCount = Object.keys(s.seen).length;
    }
  }

  function specText(spec, data, ids) {
    if (spec.dynamic === "provider_thinking") {
      var p = branded(data.provider || data.gateway || "");
      return p ? "Thinking with " + p : "Thinking with the AI provider";
    }
    if (spec.dynamic === "tool_call") {
      return labelForTool(data.tool);
    }
    if (spec.dynamic === "tool") {
      return labelForTool(data.tool);
    }
    var t = spec.text || "";
    if (t.indexOf("{provider}") >= 0) {
      var name = branded(data.provider || data.model || "");
      t = t.replace("{provider}", name || "the AI provider");
    }
    return t;
  }

  function reasonOf(spec, data) {
    if (spec.reason && data[spec.reason]) return data[spec.reason];
    return data.error || data.reason || data.message || data.status || "";
  }

  /* Event kinds the backend may add without a mapping yet: keep the chat
   * line honest by naming the subsystem instead of showing an internal id —
   * or ignore it entirely when it is not a lifecycle event. */
  var PREFIX_RULES = [
    [/^chat\.pipeline\./, "Understanding your request", "Understanding your request"],
    [/^astra_gateway\.|^router\./, "Selecting AI provider", "Selecting AI provider"],
    [/^provider\.|^ai\./, "Thinking with the AI provider", ""],
    [/^terminal\./, "Running terminal command", "Running terminal command"],
    [/^browser\./, "Opening webpage", "Opening webpage"],
    [/^web3\./, "Checking blockchain data", ""],
    [/^workflow\./, "Running workflow", "Running workflow"],
    [/^task\./, "Running workflow step", ""],
    [/^agent\./, "Working on your request", ""],
    [/^tool\./, "", ""],
  ];

  function fallbackSpec(kind, data) {
    var k = String(kind || "");
    if (!k) return null;
    // Anything with a lifecycle suffix but no mapping is still an operation.
    var start = /\.(started|request|opened)$/.test(k);
    var terminal = /\.(completed|succeeded|failed|error|timeout|cancelled|rejected|confirmed|done|finished|exhausted)$/.test(k);
    if (!start && !terminal) return null;
    for (var i = 0; i < PREFIX_RULES.length; i++) {
      if (PREFIX_RULES[i][0].test(k)) {
        var text = PREFIX_RULES[i][1];
        if (!text && data && data.tool) text = labelForTool(data.tool);
        if (!text) text = "Working on your request";
        return {
          text: start ? text : (terminal ? "Reviewing results" : text),
          step: start ? (PREFIX_RULES[i][2] || text) : "",  // start only
          phase: start ? "start" : "terminal",
          key: "op",
          failure: /(failed|error|timeout|rejected|exhausted)$/.test(k),
        };
      }
    }
    return null;
  }

  return {
    STATE: STATE,
    STEP: STEP,
    createTracker: createTracker,
    labelForTool: labelForTool,
    branded: branded,
    summaryFor: summaryFor,
    nextStepFor: nextStepFor,
    createCardTracker: createCardTracker,
    terminalAction: terminalAction,
    kindForTool: kindForTool,
    CARD: CARD,
    CARD_KIND: CARD_KIND,
    MAX_STEPS: MAX_STEPS,
    MAX_CARDS: MAX_CARDS,
    MAPPING: M,
  };
});
