/* Astra Agent Workflow — pure presentation model.
 *
 * Deliberately DOM-free so the node-graph layout, the real WorkflowEngine
 * semantics and the live-event reduction can be unit-tested under node
 * without a browser (see tests/js/workflow_model.test.js). astra.js consumes
 * this through `window.AstraWorkflow`; it is a *view model* over the existing
 * astra/workflows/engine.py + /api/events SSE feed, never a second engine.
 *
 * Everything here is derived from the real repository:
 *  - step shape, auto ids, `depends_on`, `{{step.param}}` refs and `if`
 *    conditions come from astra/workflows/engine.py (_topo/_resolve/_lookup/
 *    _condition).
 *  - run statuses come from astra/core/state.py WORKFLOW_STATUSES.
 *  - step result states come from what engine.run() actually writes into
 *    `results` ({ok, output} | {ok:false, error} | {skipped} | {blocked}).
 *  - pipeline stages are the real ChatPipeline turn, each labelled with its
 *    real module/function; the stage of an event is decided from the kinds
 *    chat_pipeline.py / router.py / agent_tool_loop.py / gateway.py emit.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AstraWorkflow = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* astra/core/state.py :: WORKFLOW_STATUSES */
  var WORKFLOW_STATUSES = ["created", "running", "paused", "completed",
                           "failed", "cancelled"];
  /* What a *step* can actually be. `running`/`completed`/`failed` come from
   * the task.started/task.completed/task.failed events engine.run() emits;
   * `pending` is "not reached yet"; `skipped` and `blocked` are the literal
   * engine result shapes. Workflow steps never emit "retrying" — that state
   * belongs to ToolRegistry/router retries, so it is not invented here. */
  var STEP_STATES = ["pending", "running", "completed", "failed",
                     "skipped", "blocked"];

  var STEP_STATE_LABEL = {
    pending: "pending", running: "running", completed: "completed",
    failed: "failed", skipped: "skipped", blocked: "blocked",
  };
  /* Maps a step state onto the Activity Log's status vocabulary so the same
   * status dots / colours are reused. */
  var STEP_STATE_STATUS = {
    pending: "info", running: "running", completed: "ok",
    failed: "err", skipped: "warn", blocked: "warn",
  };

  var RUN_STATUS_STATUS = {
    created: "info", running: "running", paused: "warn",
    completed: "ok", failed: "err", cancelled: "warn",
  };

  /* ------------------------------------------------ definitions + layout */

  function clip(value, max) {
    var s = value == null ? "" : String(value);
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  function isPlainObject(v) {
    return !!v && typeof v === "object" && !Array.isArray(v);
  }

  /* engine.run(): "tolerate steps without explicit ids: s1, s2, …" — the
   * engine actually writes `step<i>`, so mirror that exactly. */
  function normalizeSteps(steps) {
    var list = Array.isArray(steps) ? steps : [];
    return list.map(function (s, i) {
      var step = isPlainObject(s) ? s : {};
      var id = typeof step.id === "string" && step.id ? step.id
                                                      : "step" + (i + 1);
      return {
        id: id,
        tool: typeof step.tool === "string" ? step.tool : "",
        name: typeof step.name === "string" ? step.name : "",
        params: isPlainObject(step.params) ? step.params : {},
        depends_on: Array.isArray(step.depends_on)
          ? step.depends_on.filter(function (d) { return typeof d === "string"; })
          : [],
        if: isPlainObject(step.if) ? step.if : null,
      };
    });
  }

  /* engine._resolve()/`_lookup()`: any string param may contain
   * `{{step_id.param}}`; the first segment names a previous step id or a run
   * param. Returns the raw paths (without braces), for the data-flow edges. */
  var REF_RE = /\{\{\s*([\w.]+)\s*\}\}/g;
  function extractRefs(params) {
    var out = [];
    if (!isPlainObject(params)) return out;
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (typeof v !== "string" || v.indexOf("{{") === -1) return;
      var m;
      REF_RE.lastIndex = 0;
      while ((m = REF_RE.exec(v)) !== null) out.push(m[1]);
    });
    return out;
  }

  function refRoot(path) { return String(path || "").split(".")[0]; }

  /* Longest-path layering over `depends_on` — the same dependency order
   * engine._topo() produces, exposed as (column, row) so the UI can draw a
   * stable left-to-right graph. Cycles degrade to column 0 instead of
   * hanging (the engine also just leaves un-orderable steps out of _topo). */
  function layerSteps(steps) {
    var norm = normalizeSteps(steps);
    var byId = {};
    norm.forEach(function (s) { byId[s.id] = s; });
    var depth = {}, visiting = {};
    function resolve(id) {
      if (depth[id] != null) return depth[id];
      if (visiting[id]) return 0;            // cycle guard
      visiting[id] = true;
      var s = byId[id];
      var deps = (s ? s.depends_on : []).filter(function (d) {
        return Object.prototype.hasOwnProperty.call(byId, d) && d !== id;
      });
      var d = 0;
      deps.forEach(function (dep) { d = Math.max(d, resolve(dep) + 1); });
      visiting[id] = false;
      depth[id] = d;
      return d;
    }
    norm.forEach(function (s) { resolve(s.id); });
    var layers = [];
    norm.forEach(function (s) {
      var col = depth[s.id] || 0;
      (layers[col] = layers[col] || []).push(s.id);
    });
    var out = [];
    for (var i = 0; i < layers.length; i++) out.push(layers[i] || []);
    return { order: norm.map(function (s) { return s.id; }),
             depth: depth, layers: out };
  }

  /* Dependency edges (`depends_on`) and data-flow edges (`{{ref}}`), both
   * real engine semantics. A ref to a run param (not a step id) is dropped
   * from the graph but reported so the inspector can show it as such. */
  function graphEdges(steps) {
    var norm = normalizeSteps(steps);
    var ids = {};
    norm.forEach(function (s) { ids[s.id] = true; });
    var edges = [], runParams = [];
    norm.forEach(function (s) {
      s.depends_on.forEach(function (d) {
        if (d !== s.id && ids[d]) edges.push({ from: d, to: s.id, kind: "dep" });
      });
      extractRefs(s.params).forEach(function (path) {
        var src = refRoot(path);
        if (src === s.id) return;
        if (ids[src]) {
          edges.push({ from: src, to: s.id, kind: "data", via: path });
        } else if (runParams.indexOf(src) === -1) {
          runParams.push(src);
        }
      });
    });
    return { edges: edges, run_params: runParams };
  }

  function buildGraph(steps) {
    var norm = normalizeSteps(steps);
    var lay = layerSteps(norm);
    var ed = graphEdges(norm);
    var nodes = norm.map(function (s) {
      var deps = s.depends_on.slice();
      var refs = extractRefs(s.params);
      var dependents = norm.filter(function (o) {
        return o.depends_on.indexOf(s.id) !== -1;
      }).map(function (o) { return o.id; });
      return {
        id: s.id, tool: s.tool, name: s.name, params: s.params,
        depends_on: deps, refs: refs, dependents: dependents,
        condition: s.if ? { step: s.if.step || "", op: s.if.op || "" } : null,
        depth: lay.depth[s.id] || 0,
      };
    });
    return { nodes: nodes, depth: lay.depth, layers: lay.layers,
             edges: ed.edges, run_params: ed.run_params };
  }

  /* engine.run() writes one of these shapes per step into run.results. */
  function stepStateFromResult(result) {
    if (!isPlainObject(result)) return "pending";
    if (result.skipped === true) return "skipped";
    if (result.blocked === true) return "blocked";
    if (result.ok === true) return "completed";
    if (result.ok === false) return "failed";
    return "pending";
  }

  function stepError(result) {
    if (!isPlainObject(result)) return "";
    if (result.error) return String(result.error);
    if (result.reason) return String(result.reason);
    return "";
  }

  /* Historical replay: a finished (or paused) run's results painted onto the
   * definition's steps. `current_step` marks the step a paused/interrupted
   * run stopped at. */
  function runStates(run, steps) {
    var norm = normalizeSteps(steps);
    var results = (run && isPlainObject(run.results)) ? run.results : {};
    var status = (run && run.status) || "";
    var current = (run && run.current_step) || "";
    var out = {};
    norm.forEach(function (s) {
      var has = Object.prototype.hasOwnProperty.call(results, s.id);
      var state = has ? stepStateFromResult(results[s.id]) : "pending";
      if (!has && status === "running" && current === s.id) state = "running";
      if (!has && status === "paused" && current === s.id) state = "pending";
      out[s.id] = state;
    });
    return out;
  }

  /* ------------------------------------------------ live event reduction */

  /* A workflow run's events are correlated by `op`:
   *   workflow.started / workflow.completed → op "wf:<run_id>"
   *   task.started/completed/failed         → op "wf:<run_id>:<step_id>"
   * plus run_id/step on the payload (astra/workflows/engine.py). */
  function runIdOf(data) {
    var d = isPlainObject(data) ? data : {};
    if (d.run_id != null && d.run_id !== "") return String(d.run_id);
    var op = String(d.op || "");
    if (op.indexOf("wf:") === 0) return op.split(":")[1] || "";
    return "";
  }

  function stepIdOf(data) {
    var d = isPlainObject(data) ? data : {};
    if (d.step) return String(d.step);
    var op = String(d.op || "");
    var parts = op.split(":");
    return parts.length >= 3 ? parts.slice(2).join(":") : "";
  }

  function emptyRun() {
    return { run_id: "", status: "", current_step: "", steps: {},
             started_at: "", completed_at: "", error: "", events: 0 };
  }

  /* Pure reducer: fold one real event into a live run snapshot. Unknown
   * events are ignored (returns the same object shape, unchanged). */
  function reduceRun(state, event) {
    var st = state || emptyRun();
    var e = event || {};
    var kind = String(e.kind || "");
    var d = isPlainObject(e.data) ? e.data : {};
    var runId = runIdOf(d);
    if (!runId) return st;
    if (kind.indexOf("workflow.") === 0) {
      st.run_id = runId;
      st.events += 1;
      if (kind === "workflow.started") {
        st.status = "running";
        st.started_at = e.created_at || st.started_at;
        st.current_step = "";
      } else if (kind === "workflow.completed") {
        st.status = "completed";
        st.completed_at = e.created_at || "";
        st.current_step = "";
      } else if (kind === "workflow.failed") {
        st.status = "failed";
        st.error = String(d.error || st.error || "");
        st.completed_at = e.created_at || "";
      }
      return st;
    }
    if (kind.indexOf("task.") !== 0) return st;
    var sid = stepIdOf(d);
    if (!sid) return st;
    st.events += 1;
    var step = st.steps[sid] || { state: "pending", started_at: "", ended_at: "",
                                  error: "", duration_ms: 0 };
    if (kind === "task.started") {
      step.state = "running";
      step.started_at = e.created_at || "";
      st.current_step = sid;
      if (!st.status) st.status = "running";
    } else if (kind === "task.completed") {
      step.state = "completed";
      step.ended_at = e.created_at || "";
      if (d.duration_ms) step.duration_ms = d.duration_ms;
    } else if (kind === "task.failed") {
      step.state = "failed";
      step.ended_at = e.created_at || "";
      step.error = String(d.error || "");
      if (d.duration_ms) step.duration_ms = d.duration_ms;
    } else if (kind === "task.skipped") {
      step.state = "skipped";
      step.ended_at = e.created_at || "";
    }
    st.steps[sid] = step;
    return st;
  }

  /* ------------------------------------------------------- runtime pipeline */

  /* The real end-to-end path of one chat turn, in order. Every stage names
   * the module + symbol it is a view of; astra.js renders these directly so
   * the page can never drift from the code it claims to show. */
  var PIPELINE_STAGES = [
    { id: "request", label: "Request", icon: "📨",
      file: "astra/ai/chat_pipeline.py", symbol: "ChatPipeline.run()",
      note: "POST /api/chat → astra/agent.py Agent.handle()" },
    { id: "understand", label: "Gateway · understand", icon: "🧭",
      file: "astra/ai/gateway.py", symbol: "AstraAIGateway.chat()",
      note: "live capability catalog: astra/ai/capability_context.py" },
    { id: "route", label: "Router · select model", icon: "🧠",
      file: "astra/ai/router.py", symbol: "AstraRouter.route_request()",
      note: "scoring + health + credential rotation + fallback" },
    { id: "execute", label: "Agent · execute", icon: "🛠️",
      file: "astra/ai/agent_tool_loop.py", symbol: "AgentToolLoop.run()",
      note: "provider/model calls + iterative tool steps" },
    { id: "tools", label: "Registry · tools", icon: "⚙️",
      file: "astra/tools/registry.py", symbol: "ToolRegistry.execute()",
      note: "permission gate + schema + retry + audit" },
    { id: "verify", label: "Gateway · verify", icon: "✅",
      file: "astra/ai/gateway_task_completion.py",
      symbol: "verify_task_completion()",
      note: "bounded verify → correct → re-verify loop" },
    { id: "memory", label: "Memory · context", icon: "🧩",
      file: "astra/memory/memory.py", symbol: "MemorySystem",
      note: "conversation context: astra/ai/conversation_context.py" },
    { id: "reply", label: "Reply", icon: "💬",
      file: "astra/ai/response_boundary.py",
      symbol: "sanitize_final_response()",
      note: "chat.pipeline.finished / chat.pipeline.failed" },
  ];

  var STAGE_BY_ID = {};
  PIPELINE_STAGES.forEach(function (s) { STAGE_BY_ID[s.id] = s; });

  /* Which stage a real event belongs to. `phase` disambiguates the Gateway's
   * two calls (understand before `chat.pipeline.assigned`, verify after) —
   * both emit astra_gateway.*, which is why the payload alone can't say. */
  function pipelineStageOf(kind, data, phase) {
    var k = String(kind || "");
    if (k === "chat.pipeline.started") return "request";
    if (k === "chat.pipeline.assigned" ||
        k === "chat.pipeline.understand_failed") return "understand";
    if (k.indexOf("astra_gateway.") === 0) {
      return phase === "verify" ? "verify" : "understand";
    }
    if (k.indexOf("router.") === 0 || k.indexOf("provider.") === 0 ||
        k.indexOf("credential.") === 0) {
      return k.indexOf("router.gateway_") === 0 ? "verify" : "route";
    }
    if (k.indexOf("ai.") === 0 || k.indexOf("agent.tool_loop") === 0 ||
        k === "agent.tool_call" || k === "agent.tool_result") return "execute";
    if (k.indexOf("tool.") === 0 || k.indexOf("terminal.") === 0 ||
        k.indexOf("browser.") === 0 || k.indexOf("web3.") === 0) return "tools";
    if (k.indexOf("gateway.task_completion") === 0 ||
        k.indexOf("gateway.supervision") === 0 ||
        k.indexOf("supervision.") === 0) return "verify";
    if (k.indexOf("memory.") === 0 || k.indexOf("experience.") === 0) return "memory";
    if (k === "chat.pipeline.verified" || k === "chat.pipeline.verify_error")
      return "verify";
    if (k === "chat.pipeline.finished" || k === "chat.pipeline.failed" ||
        k === "chat.pipeline.tool_loop_error") return "reply";
    return "";
  }

  /* One chat turn's correlation key. chat.pipeline.* carry `op=chat:<req>`
   * and `request=<req>`; router/ai/tool events carry `trace=<req>`. */
  function turnKeyOf(event) {
    var e = event || {};
    var d = isPlainObject(e.data) ? e.data : {};
    if (d.request) return String(d.request);
    if (d.trace) return String(d.trace);
    var op = String(d.op || "");
    if (op.indexOf("chat:") === 0) return op.slice(5);
    return "";
  }

  function newTurn(key, event, id) {
    var stages = {};
    PIPELINE_STAGES.forEach(function (s) {
      stages[s.id] = { status: "info", detail: "", at: "", count: 0 };
    });
    return { key: key, first_id: id || 0, last_id: id || 0, label: "",
             status: "running", stages: stages, events: [], phase: "understand",
             model: "", provider: "", tools: [], verdict: "" };
  }

  /* Reuse the Activity Log's real status vocabulary for consistency. */
  function statusFromKind(kind, data, fallback) {
    var k = String(kind || "");
    var d = isPlainObject(data) ? data : {};
    if (d.retrying === true || d.terminal === false) return "warn";
    if (/(^|[._])(error|failed)$/.test(k)) return "err";
    if (/correction_(failed|exhausted)$/.test(k)) return "err";
    if (k === "correction_requested" || /correction_requested$/.test(k)) return "warn";
    if (k === "router.fallback" || k === "router.retry" ||
        k === "credential.rotation" || k === "gateway.target_cooldown") return "warn";
    if (k === "provider.health_changed") return d.healthy === false ? "err" : "ok";
    if (/\.(started|running|requested)$/.test(k)) return "running";
    if (k === "router.request" || k === "astra_gateway.request" ||
        k === "chat.pipeline.started") return "running";
    return fallback || "ok";
  }

  /* Fold one event into a turn (pure). Mutates and returns `turn`. */
  function reduceTurn(turn, event) {
    var t = turn || newTurn("", event, 0);
    var e = event || {};
    var kind = String(e.kind || "");
    var d = isPlainObject(e.data) ? e.data : {};
    var stage = pipelineStageOf(kind, d, t.phase);
    t.last_id = e.id || t.last_id;
    if (!t.label) {
      var lbl = d.workflow || d.task || d.task_type || d.tool || "";
      if (lbl) t.label = String(lbl);
    }
    if (kind === "router.decision") {
      t.provider = String(d.provider || t.provider || "");
      t.model = String(d.model || t.model || "");
    }
    if (kind === "ai.started" || kind === "ai.completed" || kind === "ai.failed") {
      if (d.provider) t.provider = String(d.provider);
      if (d.model) t.model = String(d.model);
    }
    if (kind === "chat.pipeline.verified") {
      t.verdict = String(d.verdict || "verified");
    }
    if (stage === "tools" &&
        (kind.indexOf("tool.") === 0 || kind.indexOf("terminal.") === 0)) {
      var name = String(d.tool || d.command || "");
      if (name && t.tools.indexOf(name) === -1) t.tools.push(name);
    }
    if (stage) {
      var s = t.stages[stage];
      s.count += 1;
      s.status = statusFromKind(kind, d, s.status === "info" ? "ok" : s.status);
      s.at = e.created_at || s.at;
      if (kind === "chat.pipeline.assigned") {
        t.phase = "verify";
        s.detail = [d.provider, d.model, d.capability].filter(Boolean).join(" · ");
      } else if (kind === "router.decision" || kind === "router.fallback") {
        s.detail = [d.provider, d.model].filter(Boolean).join(" · ");
        if (d.fallback) s.status = "warn";
      } else if (kind === "tool.completed" || kind === "tool.failed") {
        s.detail = String(d.tool || "");
      } else if (kind === "ai.completed" || kind === "ai.failed") {
        s.detail = [d.provider, d.model].filter(Boolean).join(" · ");
      } else if (kind === "chat.pipeline.assigned") {
        /* already set above */
      } else if (!s.detail && d.error) {
        s.detail = clip(d.error, 80);
      }
      if (stage === "reply" &&
          (kind === "chat.pipeline.finished" || kind === "chat.pipeline.failed" ||
           kind === "chat.pipeline.tool_loop_error")) {
        // The turn ended: nothing upstream is still in flight, so any stage
        // left "running" closes (a failed turn leaves its open stages as
        // warnings, not fake successes).
        if (kind === "chat.pipeline.finished") {
          // ChatPipeline emits status = TaskVerificationOutcome.status
          // (COMPLETE|INCOMPLETE|FAILED|UNCERTAIN) or "skipped" when the
          // Gateway could not verify at all (gateway_task_completion.py).
          var vs = String(d.status || "");
          if (vs === "FAILED") t.status = "err";
          else if (vs === "" || vs === "COMPLETE" || vs === "ok") t.status = "ok";
          else t.status = "warn";   // skipped | INCOMPLETE | UNCERTAIN
        } else {
          t.status = "err";
        }
        PIPELINE_STAGES.forEach(function (ps) {
          if (ps.id === "reply") return;
          if (t.stages[ps.id].status === "running") {
            t.stages[ps.id].status = t.status === "err" ? "warn" : "ok";
          }
        });
      } else if (t.status === "running" && (s.status === "err")) {
        t.status = "err";
      }
    }
    t.events.push(e);
    if (t.events.length > 400) t.events.shift();
    return t;
  }

  /* Group a flat event list into turns (newest last), bounded. */
  function pipelineTurns(events) {
    var turns = {}, order = [];
    (events || []).forEach(function (e) {
      var key = turnKeyOf(e);
      if (!key || !e.kind) return;
      var kind = String(e.kind || "");
      var cat = kind.split(".")[0];
      if (["memory", "experience"].indexOf(cat) !== -1) return;
      if (!turns[key]) { turns[key] = newTurn(key, e, e.id); order.push(key); }
      reduceTurn(turns[key], e);
    });
    return order.map(function (k) { return turns[k]; });
  }

  /* ------------------------------------------------------- definitions UX */

  /* Validation matching what the backend actually enforces:
   *   - POST /api/workflows requires a non-empty name and steps to be a list
   *     (web.py), and WorkflowEngine.define raises on an empty name.
   *   - engine.run() raises KeyError for an unknown tool (surfaced as a
   *     failed step), and a duplicate step id silently loses the earlier one,
   *     so both are reported here instead of silently accepted. */
  function validateDefinition(name, steps, knownTools) {
    var errors = [], warnings = [];
    if (!String(name == null ? "" : name).trim()) {
      errors.push("name is required (workflow_definitions.name is NOT NULL UNIQUE)");
    }
    var norm = normalizeSteps(steps);
    if (!norm.length) errors.push("at least one step is required to run");
    var seen = {}, ids = {};
    norm.forEach(function (s) { ids[s.id] = s; });
    var tools = knownTools || null;
    norm.forEach(function (s) {
      if (!s.tool) errors.push("step " + s.id + ": tool is required");
      else if (tools && !tools[s.tool]) {
        errors.push("step " + s.id + ": unknown tool '" + s.tool +
                    "' (registry.execute raises KeyError)");
      }
      if (seen[s.id]) errors.push("duplicate step id '" + s.id + "'");
      seen[s.id] = true;
      s.depends_on.forEach(function (d) {
        if (!ids[d]) errors.push("step " + s.id + ": depends_on '" + d +
                                 "' is not a step in this workflow");
        if (d === s.id) errors.push("step " + s.id + ": depends_on itself");
      });
      if (s.if && s.if.step && !ids[s.if.step]) {
        warnings.push("step " + s.id + ": condition references unknown step '" +
                      s.if.step + "'");
      }
      if (s.if && s.if.op && s.if.op !== "ok") {
        warnings.push("step " + s.id + ": condition op '" + s.if.op +
                      "' — engine._condition only acts on 'ok' (anything else is always true)");
      }
    });
    return { ok: errors.length === 0, errors: errors, warnings: warnings };
  }

  /* A brand-new step for the selected real tool. Required args are prefilled
   * from the tool's own input_schema so the builder starts from a valid call. */
  function newStepTemplate(tool, existingSteps) {
    var existing = normalizeSteps(existingSteps);
    var n = existing.length + 1, id = "step" + n;
    while (existing.some(function (s) { return s.id === id; })) {
      n += 1; id = "step" + n;
    }
    var params = {};
    var schema = (tool && tool.input_schema) || {};
    var props = isPlainObject(schema) ? (schema.properties || {}) : {};
    Object.keys(props).forEach(function (k) {
      var spec = props[k] || {};
      if (spec.required || spec["required"]) {
        params[k] = spec.type === "int" || spec.type === "number" ? 0 : "";
      }
    });
    var deps = existing.length ? [existing[existing.length - 1].id] : [];
    return { id: id, tool: (tool && tool.name) || "", params: params,
             depends_on: deps, name: (tool && tool.description || "").slice(0, 60) };
  }

  /* Params are a JSON object in the definition and in POST /api/workflows;
   * the run dialog takes the same shape. Never eval. */
  function parseParamsJson(text) {
    var raw = String(text == null ? "" : text).trim();
    if (!raw) return { ok: true, value: {} };
    var parsed;
    try { parsed = JSON.parse(raw); }
    catch (err) { return { ok: false, error: String(err.message || err) }; }
    if (!isPlainObject(parsed)) {
      return { ok: false, error: "params must be a JSON object" };
    }
    return { ok: true, value: parsed };
  }

  /* Substitute `{{step.param}}` the way engine._lookup() does, for the
   * inspector's "resolved input" preview (pure, best-effort). */
  function resolveParams(params, results, runParams) {
    var out = {};
    if (!isPlainObject(params)) return out;
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (typeof v === "string" && v.indexOf("{{") !== -1) {
        out[k] = v.replace(REF_RE, function (_m, path) {
          var parts = path.split(".");
          var cur;
          if (Object.prototype.hasOwnProperty.call(results || {}, parts[0])) {
            cur = results[parts[0]];
            for (var i = 1; i < parts.length; i++) {
              cur = isPlainObject(cur) ? (cur[parts[i]] == null ? "" : cur[parts[i]]) : "";
            }
          } else if (isPlainObject(runParams) &&
                     Object.prototype.hasOwnProperty.call(runParams, parts[0])) {
            cur = runParams[parts[0]];
          } else { cur = ""; }
          return typeof cur === "string" ? cur : JSON.stringify(cur);
        });
      } else { out[k] = v; }
    });
    return out;
  }

  return {
    WORKFLOW_STATUSES: WORKFLOW_STATUSES,
    STEP_STATES: STEP_STATES,
    STEP_STATE_LABEL: STEP_STATE_LABEL,
    STEP_STATE_STATUS: STEP_STATE_STATUS,
    RUN_STATUS_STATUS: RUN_STATUS_STATUS,
    clip: clip,
    isPlainObject: isPlainObject,
    normalizeSteps: normalizeSteps,
    extractRefs: extractRefs,
    layerSteps: layerSteps,
    graphEdges: graphEdges,
    buildGraph: buildGraph,
    stepStateFromResult: stepStateFromResult,
    stepError: stepError,
    runStates: runStates,
    runIdOf: runIdOf,
    stepIdOf: stepIdOf,
    emptyRun: emptyRun,
    reduceRun: reduceRun,
    PIPELINE_STAGES: PIPELINE_STAGES,
    pipelineStageOf: pipelineStageOf,
    turnKeyOf: turnKeyOf,
    reduceTurn: reduceTurn,
    pipelineTurns: pipelineTurns,
    newTurn: newTurn,
    statusFromKind: statusFromKind,
    validateDefinition: validateDefinition,
    newStepTemplate: newStepTemplate,
    parseParamsJson: parseParamsJson,
    resolveParams: resolveParams,
  };
});
