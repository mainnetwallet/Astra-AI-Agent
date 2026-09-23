/* Astra Agent Workflow — pure editor model.
 *
 * DOM-free on purpose (same split as log_model.js / astra.js) so the parts
 * that decide *what is sent to the backend* can be unit-tested under node:
 *
 *   draft            the editable copy of ONE workflow (never shared with
 *                    another workflow's draft, never shared with the object
 *                    returned by the API — see `draftFromDefinition`)
 *   definitionFromDraft  draft -> the exact JSON body POST/PATCH sends
 *   canvasNodes/canvasEdges  the visual graph derived from real steps
 *
 * The backend representation is a list of steps
 * ({id, tool, params, depends_on, if}) — the canvas is a VIEW over that, so
 * nothing here may invent a field the engine cannot execute.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AstraWorkflowModel = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var CATEGORY_LABELS = {
    ai: "AI / Agent", browser: "Browser", terminal: "Terminal",
    web3: "Web3", memory: "Memory", tasks: "Tasks", files: "Files",
    research: "Web / API", system: "System", wallet: "Wallet",
  };
  var CATEGORY_ICONS = {
    ai: "🧠", browser: "🌐", terminal: "⌨️", web3: "⛓️", memory: "🧩",
    tasks: "✅", files: "📄", research: "🔎", system: "🩺", wallet: "👛",
  };
  var NODE_W = 232;
  var NODE_H = 84;
  var COL_GAP = 300;
  var ROW_GAP = 132;

  function categoryLabel(cat) {
    var key = String(cat == null ? "" : cat).toLowerCase();
    return CATEGORY_LABELS[key] || (key ? key.charAt(0).toUpperCase() + key.slice(1) : "Tool");
  }
  function categoryIcon(cat) {
    var key = String(cat == null ? "" : cat).toLowerCase();
    return CATEGORY_ICONS[key] || "🔧";
  }

  function clone(value) {
    if (value === undefined || value === null) return value === null ? null : undefined;
    return JSON.parse(JSON.stringify(value));
  }

  var _uid = 0;
  function nextUid() { _uid += 1; return "d" + _uid; }

  /* ------------------------------------------------------------ draft state */
  function blankDraft() {
    return {
      uid: nextUid(),
      mode: "new",          // "new" => POST (create) | "edit" => PATCH (update)
      id: null,             // server id; null until the backend has created it
      name: "",
      description: "",
      steps: [],
      layout: {},
      dirty: true,
    };
  }

  /* A draft owns its own deep copy of every field. The definition object the
   * API returned is never referenced, so editing a draft can never reach
   * back into another workflow's data (the bug class where "New Workflow"
   * silently edited the previously selected one). */
  function draftFromDefinition(def) {
    var d = blankDraft();
    var src = def || {};
    d.mode = "edit";
    d.id = (src.id == null ? null : Number(src.id));
    d.name = String(src.name == null ? "" : src.name);
    d.description = String(src.description == null ? "" : src.description);
    d.steps = clone(Array.isArray(src.steps) ? src.steps : []) || [];
    d.layout = clone(src.layout && typeof src.layout === "object" ? src.layout : {}) || {};
    d.dirty = false;
    return d;
  }

  function definitionFromDraft(draft) {
    var d = draft || {};
    return {
      name: String(d.name == null ? "" : d.name).trim() || "Untitled Workflow",
      description: String(d.description == null ? "" : d.description),
      steps: clone(Array.isArray(d.steps) ? d.steps : []) || [],
      layout: clone(d.layout && typeof d.layout === "object" ? d.layout : {}) || {},
    };
  }

  function isBlankDraft(draft) {
    if (!draft) return true;
    if (draft.mode !== "new") return false;
    return !(draft.steps || []).length && !String(draft.name || "").trim();
  }

  /* --------------------------------------------------------------- steps */
  function stepIds(steps) {
    return (steps || []).map(function (s) { return s && s.id; })
                        .filter(function (id) { return typeof id === "string" && id; });
  }

  function nextNodeId(steps) {
    var used = {};
    stepIds(steps).forEach(function (id) { used[id] = 1; });
    var n = 1;
    while (used["s" + n]) n += 1;
    return "s" + n;
  }

  function findStep(draft, id) {
    var steps = (draft && draft.steps) || [];
    for (var i = 0; i < steps.length; i += 1) {
      if (steps[i] && steps[i].id === id) return steps[i];
    }
    return null;
  }

  /* Adding a node never touches an existing step's object: the new step is
   * appended, and only the `depends_on` array of the NEW step is written. */
  function addStep(draft, toolName, opts) {
    if (!draft) throw new Error("addStep: no draft");
    var o = opts || {};
    var id = nextNodeId(draft.steps);
    var step = {
      id: id,
      name: o.name || "",
      tool: String(toolName || "get_health"),
      params: o.params ? clone(o.params) : {},
      depends_on: [],
      if: null,
    };
    if (o.dependsOn && o.dependsOn.length && !wouldCycle(draft.steps, o.dependsOn, id)) {
      step.depends_on = o.dependsOn.slice();
    }
    draft.steps = (draft.steps || []).concat([step]);
    draft.dirty = true;
    return step;
  }

  function removeStep(draft, id) {
    if (!draft) return false;
    var before = (draft.steps || []).length;
    draft.steps = (draft.steps || []).filter(function (s) { return s && s.id !== id; });
    // Dependencies AND conditions that pointed at the removed step go with
    // it — a dangling reference would be rejected by the engine.
    draft.steps.forEach(function (s) {
      s.depends_on = (s.depends_on || []).filter(function (d) { return d !== id; });
      if (s.if && s.if.step === id) s.if = null;
    });
    if (draft.layout) delete draft.layout[id];
    draft.dirty = draft.dirty || draft.steps.length !== before;
    return draft.steps.length !== before;
  }

  function setDependsOn(draft, id, deps) {
    var step = findStep(draft, id);
    if (!step) return false;
    var keep = (deps || []).filter(function (d) { return d && d !== id; });
    if (wouldCycle(draft.steps, keep, id)) return false;
    step.depends_on = keep;
    draft.dirty = true;
    return true;
  }

  /* True when making `to` depend on `from` (or on any of `froms`) would
   * create a cycle. The engine refuses cyclic definitions, so the editor
   * must refuse to draw them. */
  function wouldCycle(steps, froms, to) {
    var list = froms || [];
    if (typeof list === "string") list = [list];
    var deps = {};
    (steps || []).forEach(function (s) {
      if (s && s.id) deps[s.id] = (s.depends_on || []).slice();
    });
    deps[to] = list.slice();
    // walk the candidate dependencies: if any path leads back to `to`, the
    // edge would close a cycle the engine refuses to execute.
    var seen = {};
    var stack = list.slice();
    while (stack.length) {
      var cur = stack.pop();
      if (cur === to) return true;
      if (seen[cur]) continue;
      seen[cur] = 1;
      (deps[cur] || []).forEach(function (d) { stack.push(d); });
    }
    return false;
  }

  function topoOrder(steps) {
    var list = (steps || []).filter(function (s) { return s && s.id; });
    var byId = {};
    list.forEach(function (s) { byId[s.id] = s; });
    var ordered = [];
    var done = {};
    var pending = true;
    while (pending && ordered.length < list.length) {
      pending = false;
      list.forEach(function (s) {
        if (done[s.id]) return;
        var deps = (s.depends_on || []).filter(function (d) { return byId[d] && !done[d]; });
        if (!deps.length) { ordered.push(s); done[s.id] = 1; pending = true; }
      });
    }
    return ordered;
  }

  function hasCycle(steps) {
    return topoOrder(steps).length < (steps || []).length;
  }

  /* ------------------------------------------------------------ conditions */
  /* Astra's engine expresses a branch as `if: {step, op}` ON the gated step.
   * The canvas shows a Condition node as well; it is a pure view of those
   * fields (id "cond:<step>:<op>") and compiling it back writes exactly the
   * same `if` — no extra backend concept is invented. */
  function conditionKey(step, op) { return "cond:" + step + ":" + (op || "ok"); }

  function parseConditionId(id) {
    var parts = String(id == null ? "" : id).split(":");
    if (parts.length !== 3 || parts[0] !== "cond") return null;
    return { step: parts[1], op: parts[2] || "ok" };
  }

  function conditionNodes(draft) {
    var steps = (draft && draft.steps) || [];
    var byKey = {};
    var order = [];
    steps.forEach(function (s) {
      if (!s || !s.if || !s.if.step) return;
      var op = s.if.op || "ok";
      var key = conditionKey(s.if.step, op);
      if (!byKey[key]) {
        byKey[key] = { id: key, kind: "condition", step: s.if.step, op: op, gates: [] };
        order.push(key);
      }
      byKey[key].gates.push(s.id);
    });
    return order.map(function (k) { return byKey[k]; });
  }

  function upsertCondition(draft, condId, opts) {
    var o = opts || {};
    var step = o.step;
    var op = o.op === "not_ok" ? "not_ok" : "ok";
    var gates = (o.gates || []).slice();
    var newId = conditionKey(step, op);
    var known = step && findStep(draft, step) && (o.gates || []).every(function (g) {
      return !!findStep(draft, g) && g !== step;
    });
    if (!known) return null;
    // clear the old condition's gates when the target/op changed
    if (condId && condId !== newId) clearCondition(draft, condId);
    (draft.steps || []).forEach(function (s) {
      if (!s) return;
      if (gates.indexOf(s.id) >= 0) {
        s.if = { step: step, op: op };
      } else if (s.if && s.if.step === step && (s.if.op || "ok") === op) {
        s.if = null;
      }
    });
    draft.dirty = true;
    return newId;
  }

  function clearCondition(draft, condId) {
    var parsed = parseConditionId(condId);
    if (!parsed) return false;
    var touched = false;
    (draft.steps || []).forEach(function (s) {
      if (s && s.if && s.if.step === parsed.step &&
          (s.if.op || "ok") === parsed.op) {
        s.if = null;
        touched = true;
      }
    });
    if (draft.layout) delete draft.layout[condId];
    if (touched) draft.dirty = true;
    return touched;
  }

  /* ------------------------------------------------------- tool schemas */
  /* Two shapes exist on the live registry (see astra/ai/agent_tool_loop.py):
   * a flat {"arg": {"type": ..., "required": ...}} map and a full JSON-Schema
   * object {"type":"object","properties":{...},"required":[...]}. The node
   * inspector renders fields from whichever the tool actually declares, so
   * it can never drift from the backend schema. */
  function toolFields(tool) {
    if (!tool) return [];
    var s = tool.input_schema || {};
    var required = {};
    if (Array.isArray(s.required)) s.required.forEach(function (r) { required[r] = 1; });
    var props = (s.type === "object" && s.properties) ? s.properties : s;
    if (!props || typeof props !== "object") return [];
    return Object.keys(props).map(function (name) {
      var spec = props[name];
      if (!spec || typeof spec !== "object") spec = {};
      return {
        name: name,
        type: typeof spec.type === "string" ? spec.type : "string",
        required: required[name] === 1 || spec.required === true,
        description: typeof spec.description === "string" ? spec.description : "",
      };
    });
  }

  /* ----------------------------------------------------------- canvas view */
  function toolMapFrom(list) {
    var map = {};
    (list || []).forEach(function (t) { if (t && t.name) map[t.name] = t; });
    return map;
  }

  function nodeKind(step, toolMap) {
    var tool = (toolMap || {})[step && step.tool];
    var cat = tool && tool.category ? String(tool.category) : "";
    return cat === "ai" ? "ai" : "tool";
  }

  function canvasNodes(draft, toolMap) {
    var d = draft || {};
    var nodes = [];
    nodes.push({
      id: "__trigger__",
      kind: "trigger",
      name: "Trigger",
      subtitle: d.scheduleLabel || "Manual run",
      icon: "▶",
    });
    topoOrder(d.steps || []).forEach(function (s) {
      var tool = (toolMap || {})[s.tool];
      nodes.push({
        id: s.id,
        kind: nodeKind(s, toolMap),
        step: s,
        name: s.name || (tool && tool.name) || s.tool || "step",
        toolLabel: s.tool,
        category: (tool && tool.category) || "",
        icon: categoryIcon(tool && tool.category),
        subtitle: summaryOf(s, toolMap),
        cond: s.if || null,
        deps: (s.depends_on || []).slice(),
      });
    });
    conditionNodes(d).forEach(function (c) {
      nodes.push({
        id: c.id,
        kind: "condition",
        cond: { step: c.step, op: c.op },
        gates: c.gates.slice(),
        name: "Condition",
        subtitle: shortId(c.step) + (c.op === "not_ok" ? " did not succeed" : " succeeded"),
        icon: "⚑",
      });
    });
    return nodes;
  }

  function shortId(id) { return String(id == null ? "" : id); }

  function summaryOf(step, toolMap) {
    var tool = (toolMap || {})[step.tool];
    var params = step.params || {};
    var keys = Object.keys(params).filter(function (k) {
      var v = params[k];
      return v !== "" && v !== null && v !== undefined;
    });
    if (step.tool === "ai_generate") {
      return [params.provider || "auto", params.model || ""].filter(Boolean).join(" · ") || "auto model";
    }
    if (keys.length) {
      var head = keys.slice(0, 2).map(function (k) {
        return k + "=" + String(params[k]).slice(0, 18);
      }).join(", ");
      return keys.length > 2 ? head + " +" + (keys.length - 2) : head;
    }
    return (tool && tool.category) || "no parameters";
  }

  function canvasEdges(draft, toolMap) {
    var edges = [];
    var ids = {};
    canvasNodes(draft, toolMap).forEach(function (n) { ids[n.id] = 1; });
    (draft && draft.steps ? draft.steps : []).forEach(function (s) {
      (s.depends_on || []).forEach(function (dep) {
        if (ids[dep] && ids[s.id]) edges.push({ from: dep, to: s.id, kind: "dep" });
      });
    });
    conditionNodes(draft).forEach(function (c) {
      if (ids[c.step]) edges.push({ from: c.step, to: c.id, kind: "cond" });
      c.gates.forEach(function (g) {
        if (ids[g]) edges.push({ from: c.id, to: g, kind: "gate" });
      });
    });
    // the trigger is the entry point of every node nothing else depends on
    (draft && draft.steps ? draft.steps : []).forEach(function (s) {
      if (!(s.depends_on || []).length && ids[s.id]) {
        edges.push({ from: "__trigger__", to: s.id, kind: "start" });
      }
    });
    return edges;
  }

  /* Positions live in `layout` (persisted with the definition), keyed by
   * node id; anything missing gets a deterministic column position so a
   * fresh workflow still looks like a flow. */
  function withPositions(nodes, layout) {
    var lay = layout || {};
    var seen = [];
    (nodes || []).forEach(function (n, i) {
      var pos = lay[n.id];
      var x, y;
      if (pos && typeof pos.x === "number" && typeof pos.y === "number") {
        x = pos.x; y = pos.y;
      } else if (n.id === "__trigger__") {
        x = 40; y = 40;
      } else {
        var col = seen.length;
        x = 40 + COL_GAP * (1 + (col % 2));
        y = 40 + Math.floor(col / 2) * ROW_GAP;
      }
      if (n.id !== "__trigger__") seen.push(n.id);
      n.x = x; n.y = y; n.w = NODE_W; n.h = NODE_H;
    });
    return nodes;
  }

  /* --------------------------------------------------------- graph -> engine */
  function centerOf(node) {
    return { x: node.x + node.w / 2, y: node.y + node.h / 2 };
  }
  function edgePath(a, b) {
    var p = centerOf(a), q = centerOf(b);
    var y1 = a.y + a.h, y2 = b.y;
    if (q.y >= y1 - 4) {
      // straight down (the common stacked case): a smooth vertical S-curve
      var mid = (y1 + y2) / 2;
      return "M" + p.x + " " + y1 + " C" + p.x + " " + mid + " " + q.x + " " +
             mid + " " + q.x + " " + y2;
    }
    return "M" + (a.x < b.x ? a.x + a.w : a.x) + " " + (a.y + a.h / 2) +
           " C" + ((a.x + b.x) / 2 + a.w / 2) + " " + (a.y + a.h / 2) + " " +
           ((a.x + b.x) / 2 + b.w / 2) + " " + (b.y + b.h / 2) + " " +
           b.x + " " + (b.y + b.h / 2);
  }

  /* ------------------------------------------------------------ validation */
  function validateDraft(draft, toolMap) {
    var errors = [];
    var d = draft || {};
    var steps = d.steps || [];
    if (!String(d.name == null ? "" : d.name).trim()) {
      errors.push("Give the workflow a name.");
    }
    var ids = {};
    steps.forEach(function (s, i) {
      var label = "step " + (i + 1);
      if (!s || typeof s !== "object") { errors.push(label + " is not a step."); return; }
      if (!s.id) errors.push(label + " has no id.");
      else if (ids[s.id]) errors.push("duplicate step id '" + s.id + "'.");
      else ids[s.id] = 1;
      if (!s.tool) errors.push((s.id || label) + ": no tool selected.");
      else if (toolMap && !toolMap[s.tool]) {
        errors.push((s.id || label) + ": unknown tool '" + s.tool + "'.");
      }
    });
    steps.forEach(function (s) {
      if (!s) return;
      (s.depends_on || []).forEach(function (dep) {
        if (!ids[dep]) errors.push((s.id || "?") + " depends on unknown step '" + dep + "'.");
      });
      if (s.if && s.if.step && !ids[s.if.step]) {
        errors.push((s.id || "?") + " has a condition on unknown step '" + s.if.step + "'.");
      }
      if (s.if && s.if.step === s.id) {
        errors.push(s.id + " cannot be conditioned on itself.");
      }
    });
    if (hasCycle(steps)) errors.push("dependency cycle between steps.");
    return { ok: errors.length === 0, errors: errors };
  }

  /* -------------------------------------------------------- run inspection */
  function nodeStatus(run, stepId) {
    if (!run) return "idle";
    var status = String(run.status || "");
    var results = run.results || {};
    var res = results[stepId];
    if (!res) {
      if (status === "running" && run.current_step === stepId) return "running";
      return status === "running" ? "pending" : "idle";
    }
    if (res.skipped) return "skipped";
    if (res.blocked) return "blocked";
    if (res.error || res.ok === false) return "failed";
    if (res.ok) return "success";
    return "idle";
  }

  function runCounts(run) {
    var out = { success: 0, failed: 0, skipped: 0, blocked: 0, pending: 0 };
    if (!run) return out;
    var results = run.results || {};
    var steps = run.steps || [];
    (steps.length ? steps.map(function (s) { return s.id; })
                  : Object.keys(results)).forEach(function (id) {
      var st = nodeStatus(run, id);
      if (out[st] === undefined) out[st] = 0;
      out[st] += 1;
    });
    return out;
  }

  function runHasErrors(run) {
    if (!run) return false;
    return runCounts(run).failed > 0;
  }

  function parseStamp(text) {
    if (!text) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/.exec(String(text));
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]),
                    Number(m[4]), Number(m[5]), Number(m[6]));
  }

  function durationMs(started, completed) {
    var a = parseStamp(started), b = parseStamp(completed);
    if (!a || !b) return null;
    var ms = b.getTime() - a.getTime();
    return ms >= 0 ? ms : null;
  }

  function humanDuration(ms) {
    if (ms === null || ms === undefined) return "";
    if (ms < 1000) return ms + "ms";
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60);
    return m + "m " + Math.round(s - m * 60) + "s";
  }

  function runSummary(run) {
    if (!run) return null;
    var counts = runCounts(run);
    return {
      id: run.id,
      status: run.status,
      started_at: run.started_at,
      completed_at: run.completed_at,
      current_step: run.current_step,
      error: run.error || "",
      duration: humanDuration(durationMs(run.started_at, run.completed_at)),
      failed: counts.failed,
      skipped: counts.skipped,
      total: Object.keys(run.results || {}).length,
    };
  }

  /* ---------------------------------------------------------- event filter */
  /* The execution log is built from the SAME event bus the Activity Log
   * reads (/api/events + /api/events/stream) — filtered to one run. */
  function isRunEvent(ev, runId) {
    if (!ev || runId === null || runId === undefined) return false;
    var d = ev.data || {};
    return d.run_id !== undefined && d.run_id !== null &&
           Number(d.run_id) === Number(runId);
  }

  function eventRow(ev) {
    var d = (ev && ev.data) || {};
    var kind = String((ev && ev.kind) || "");
    var short = kind.indexOf("workflow.") === 0 ? kind.slice(9) : kind;
    if (kind.indexOf("task.") === 0) short = "step " + kind.slice(5);
    return {
      id: (ev && ev.id) || 0,
      at: (ev && ev.created_at) || "",
      kind: kind,
      text: short,
      step: d.step || "",
      tool: d.tool || "",
      workflow: d.workflow || "",
      duration_ms: d.duration_ms,
      error: d.error || d.reason || "",
      terminal: !!d.terminal,
    };
  }

  /* --------------------------------------------------------- name handling */
  /* Mirrors the engine's deterministic uniquifying so the editor can show
   * the name it is about to get ("Untitled Workflow 2") before it is saved. */
  function uniqueName(taken, base) {
    var wanted = String(base == null ? "" : base).trim() || "Untitled Workflow";
    var set = {};
    (taken || []).forEach(function (n) { set[String(n)] = 1; });
    if (!set[wanted]) return wanted;
    var n = 2;
    while (set[wanted + " " + n]) n += 1;
    return wanted + " " + n;
  }

  return {
    CATEGORY_LABELS: CATEGORY_LABELS,
    NODE_W: NODE_W,
    NODE_H: NODE_H,
    clone: clone,
    nextUid: nextUid,
    categoryLabel: categoryLabel,
    categoryIcon: categoryIcon,
    blankDraft: blankDraft,
    draftFromDefinition: draftFromDefinition,
    definitionFromDraft: definitionFromDraft,
    isBlankDraft: isBlankDraft,
    stepIds: stepIds,
    findStep: findStep,
    nextNodeId: nextNodeId,
    addStep: addStep,
    removeStep: removeStep,
    setDependsOn: setDependsOn,
    wouldCycle: wouldCycle,
    topoOrder: topoOrder,
    hasCycle: hasCycle,
    conditionNodes: conditionNodes,
    conditionKey: conditionKey,
    parseConditionId: parseConditionId,
    upsertCondition: upsertCondition,
    clearCondition: clearCondition,
    toolFields: toolFields,
    toolMapFrom: toolMapFrom,
    nodeKind: nodeKind,
    canvasNodes: canvasNodes,
    canvasEdges: canvasEdges,
    withPositions: withPositions,
    edgePath: edgePath,
    validateDraft: validateDraft,
    nodeStatus: nodeStatus,
    runCounts: runCounts,
    runHasErrors: runHasErrors,
    runSummary: runSummary,
    durationMs: durationMs,
    humanDuration: humanDuration,
    isRunEvent: isRunEvent,
    eventRow: eventRow,
    uniqueName: uniqueName,
  };
});
