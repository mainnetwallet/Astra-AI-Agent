/* Unit tests for the Assistant chat execution-status model
 * (static/js/chat_status.js). Pure logic, no DOM — run with
 * `node --test tests/js/chat_status.test.js` or via tests/test_chat_status_ui.py.
 *
 * The model is a mapping over the REAL lifecycle events the Activity Log
 * receives, so these tests feed the same event shapes the backend emits
 * (astra/core/events.py + the emitters in astra/ai, astra/terminal, ...).
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");

// the chat model reuses the Activity Log's redaction + noise filter
globalThis.AstraLog = require("../../static/js/log_model.js");
const Status = require("../../static/js/chat_status.js");

let NEXT_ID = 1;
let CLOCK = 0;

function ev(kind, data, opts) {
  CLOCK += 1000;                       // 1s per event by default
  const o = opts || {};
  const ms = o.ms === undefined ? CLOCK : o.ms;
  return {
    id: o.id === undefined ? NEXT_ID++ : o.id,
    kind, agent: o.agent || "",
    data: data || {},
    created_at: o.created_at || stamp(ms),
  };
}
function stamp(ms) {
  const d = new Date(Date.UTC(2026, 8, 23, 10, 0, 0) + ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
         `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}
function turn(extra) { return Object.assign({ request: "req-1", trace: "req-1" }, extra || {}); }

/* ------------------------------------------------------------ basic shape */

test("request started: working with the real first operation", () => {
  const t = Status.createTracker();
  t.begin();
  let snap = t.snapshot();
  assert.strictEqual(snap.state, Status.STATE.WORKING);
  assert.strictEqual(snap.summary, "Working…");
  assert.strictEqual(snap.current, "");          // no invented operation

  snap = t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  assert.strictEqual(snap.state, "working");
  assert.strictEqual(snap.current, "Understanding your request");
  assert.strictEqual(snap.steps.length, 1);
  assert.strictEqual(snap.steps[0].state, Status.STEP.ACTIVE);
  assert.strictEqual(snap.steps[0].label, "Understanding request");
});

test("gateway + provider lifecycle map to human status", () => {
  const t = Status.createTracker();
  t.begin();
  const seq = [];
  const push = (k, d, o) => { t.apply(ev(k, turn(d), o)); seq.push(t.snapshot().current); };
  push("chat.pipeline.started", { op: "chat:req-1" });
  push("astra_gateway.request", { provider: "", model: "" });   // gateway started
  push("chat.pipeline.assigned", { provider: "openrouter" });
  push("ai.started", { provider: "openrouter" });               // provider started
  push("ai.completed", { provider: "openrouter" });             // provider completed
  assert.deepStrictEqual(seq, [
    "Understanding your request",
    "Selecting AI provider",
    "Selecting AI provider",
    "Thinking with OpenRouter",
    "Reviewing provider response",
  ]);
});

test("provider: no raw provider payload ever reaches the status line", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  const snap = t.apply(ev("ai.started", {
    provider: "openrouter", model: "openai/gpt-4o-mini",
    request_id: "rid-123", trace: "req-1",
    messages: [{ role: "system", content: "SECRET PROMPT" }],
    api_key: "sk-abcdefghijklmnopqrstuvwxyz",
  }));
  assert.strictEqual(snap.current, "Thinking with OpenRouter");
  assert.ok(!JSON.stringify(snap).includes("SECRET PROMPT"));
  assert.ok(!JSON.stringify(snap).includes("sk-abcdefghijklmnopqrstuvwxyz"));
  assert.ok(!JSON.stringify(snap).includes("rid-123"));
});

/* ---------------------------------------------------------------- terminal */

test("terminal started -> completed changes the current operation", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  let snap = t.apply(ev("terminal.started", { op: "term-1", process_id: "p1",
    command: "pytest -q" }));
  assert.strictEqual(snap.current, "Running terminal command");
  assert.strictEqual(snap.steps[snap.steps.length - 1].label, "Running terminal command");
  assert.strictEqual(snap.steps[snap.steps.length - 1].state, "active");

  snap = t.apply(ev("terminal.completed", { op: "term-1", process_id: "p1",
    duration: 5400, duration_ms: 5400, status: "completed", terminal: true, exit_code: 0 }));
  assert.strictEqual(snap.current, "Terminal command completed");
  const step = snap.steps.find((s) => s.label === "Running terminal command");
  assert.strictEqual(step.state, "done");
  assert.strictEqual(step.duration, "5.4s");
  // the pending "next step" hint is derived from the running operation only
  assert.ok(!snap.steps.some((s) => s.state === "pending"));
});

test("terminal failed marks the step failed but keeps the turn running", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(ev("terminal.started", { op: "term-1", process_id: "p1", command: "pytest" }));
  const snap = t.apply(ev("terminal.failed", { op: "term-1", process_id: "p1",
    exit_code: 1, duration_ms: 1200, status: "failed", terminal: true,
    stderr: "AssertionError: boom" }));
  assert.strictEqual(snap.state, "working");          // the agent keeps working
  assert.strictEqual(snap.current, "Terminal command failed");
  assert.strictEqual(snap.steps.find((s) => s.label === "Running terminal command").state,
                     "failed");
  assert.ok(!JSON.stringify(snap).includes("AssertionError"));   // no raw output
});

test("terminal timeout is a failure with a human label", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(ev("terminal.started", { op: "term-1", process_id: "p1", command: "sleep 300" }));
  const snap = t.apply(ev("terminal.timeout", { op: "term-1", process_id: "p1",
    status: "timeout", terminal: true }));
  assert.strictEqual(snap.current, "Terminal command timed out");
});

test("terminal events without a request id still update the live turn", () => {
  // terminal.* carry no request/trace (see astra/terminal/session.py); they
  // must still drive the status of the chat turn that is running.
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  const started = ev("terminal.started", { op: "term-9", process_id: "p9", command: "ls" });
  assert.strictEqual(started.data.request, undefined);
  assert.strictEqual(t.apply(started).current, "Running terminal command");
});

/* -------------------------------------------------------- file / browser / web3 */

test("file tool operations read as file work", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  let snap = t.apply(ev("tool.started", turn({ op: "tool-1", tool: "read_file",
    category: "files" })));
  assert.strictEqual(snap.current, "Reading file");
  snap = t.apply(ev("tool.completed", turn({ op: "tool-1", tool: "read_file",
    duration_ms: 12, terminal: true, output: '{"content":"x"}' })));
  assert.strictEqual(snap.current, "Reviewing results");
  snap = t.apply(ev("tool.started", turn({ op: "tool-2", tool: "write_file" })));
  assert.strictEqual(snap.current, "Editing file");
  assert.ok(!JSON.stringify(snap).includes('"content"'));
});

test("browser operations read as webpage work", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  assert.strictEqual(t.apply(ev("tool.started", turn({ op: "t1",
    tool: "browser_open" }))).current, "Opening webpage");
  assert.strictEqual(t.apply(ev("tool.started", turn({ op: "t2",
    tool: "browser_content_read" }))).current, "Reading webpage");
  assert.strictEqual(t.apply(ev("browser.error", { error: "net::ERR" })).current,
                     "Webpage could not be read");
});

test("web3 operations read as blockchain work", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  assert.strictEqual(t.apply(ev("web3.transaction.prepared",
    { tx: "0xabc", network: "base" })).current, "Checking blockchain data");
  assert.strictEqual(t.apply(ev("web3.transaction.submitted",
    { tx: "0xabc" })).current, "Checking blockchain data");
  const snap = t.apply(ev("web3.transaction.confirmed", { tx: "0xabc", terminal: true }));
  assert.strictEqual(snap.current, "Transaction confirmed");
  assert.strictEqual(snap.state, "working");     // the chat turn continues
});

test("unknown tools fall back to their own human name, never an internal id", () => {
  assert.strictEqual(Status.labelForTool("frobnicate_widget"),
                     "Using Frobnicate Widget tool");
  assert.strictEqual(Status.labelForTool("terminal_exec"), "Running terminal command");
});

/* -------------------------------------------------------- current operation */

test("the current operation follows the newest real start", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  const seen = [];
  const feed = (k, d) => seen.push(t.apply(ev(k, turn(d))).current);
  feed("astra_gateway.request", { provider: "" });
  feed("ai.started", { provider: "groq" });
  feed("tool.started", { op: "t1", tool: "terminal_exec" });
  feed("terminal.started", { op: "term-1", process_id: "p1", command: "pytest" });
  feed("terminal.completed", { op: "term-1", process_id: "p1", duration_ms: 42000,
                               status: "completed", terminal: true });
  feed("ai.started", { provider: "groq" });       // provider continues
  feed("chat.pipeline.finished", { op: "chat:req-1", status: "COMPLETE", terminal: true });
  assert.deepStrictEqual(seen, [
    "Selecting AI provider",
    "Thinking with Groq",
    "Running terminal command",
    "Running terminal command",
    "Terminal command completed",
    "Thinking with Groq",     // no stale "Running terminal command" left over
    "",                        // the turn is finished; nothing is "current"
  ]);
  assert.strictEqual(t.snapshot().state, "completed");
});

test("multi-step task: run -> inspect failure -> edit -> run again", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(ev("tool.started", turn({ op: "t1", tool: "terminal_exec" })));
  t.apply(ev("terminal.started", { op: "term-1", process_id: "p1", command: "pytest" }));
  t.apply(ev("terminal.failed", { op: "term-1", process_id: "p1", exit_code: 1,
    duration_ms: 8000, status: "failed", terminal: true }));
  t.apply(ev("tool.completed", turn({ op: "t1", tool: "terminal_exec",
    duration_ms: 8100, terminal: true })));
  t.apply(ev("agent.tool_result", turn({ op: "loop-1", step: 0, tool: "terminal_exec",
    ok: false })));
  let snap = t.snapshot();
  assert.strictEqual(snap.current, "Reviewing results");

  t.apply(ev("tool.started", turn({ op: "t2", tool: "read_file" })));
  assert.strictEqual(t.snapshot().current, "Reading file");
  t.apply(ev("tool.completed", turn({ op: "t2", tool: "read_file", terminal: true })));
  t.apply(ev("tool.started", turn({ op: "t3", tool: "write_file" })));
  assert.strictEqual(t.snapshot().current, "Editing file");
  t.apply(ev("tool.completed", turn({ op: "t3", tool: "write_file", terminal: true })));
  t.apply(ev("tool.started", turn({ op: "t4", tool: "terminal_exec" })));
  t.apply(ev("terminal.started", { op: "term-2", process_id: "p2", command: "pytest" }));
  assert.strictEqual(t.snapshot().current, "Running terminal command");
  // a pending "next step" only exists while a real operation is running
  snap = t.snapshot();
  assert.strictEqual(snap.steps[snap.steps.length - 1].state, "pending");
  assert.strictEqual(snap.steps[snap.steps.length - 1].label, "Reviewing terminal output");

  t.apply(ev("terminal.completed", { op: "term-2", process_id: "p2", duration_ms: 900,
                                     status: "completed", terminal: true }));
  t.apply(ev("chat.pipeline.finished", turn({ op: "chat:req-1", terminal: true })));
  snap = t.snapshot();
  assert.strictEqual(snap.state, "completed");
  assert.match(snap.summary, /^✓ Completed · \d+ steps/);
  // one step per real operation: terminal, read, write, terminal again
  const labels = t.snapshot().steps.map((s) => s.label);
  assert.deepStrictEqual(labels.slice(1), [
    "Running terminal command", "Reading file", "Editing file",
    "Running terminal command",
  ]);
});

/* ------------------------------------------------------------ terminal ends */

test("completed turn summarises steps and duration", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" }), { ms: 0 }));
  t.apply(ev("astra_gateway.request", turn({ provider: "" }), { ms: 1000 }));
  t.apply(ev("terminal.started", { op: "term-1", process_id: "p1" }, { ms: 2000 }));
  t.apply(ev("terminal.completed", { op: "term-1", process_id: "p1",
    duration_ms: 3000, status: "completed", terminal: true }, { ms: 5000 }));
  const snap = t.apply(ev("chat.pipeline.finished", turn({ op: "chat:req-1",
    terminal: true }), { ms: 5400 }));
  assert.strictEqual(snap.state, "completed");
  assert.match(snap.summary, /^\u2713 Completed · 3 steps · \d+(\.\d+)?s$/);
  assert.match(snap.duration, /^\d+(\.\d+)?s$/);
  assert.strictEqual(snap.current, "");
  assert.ok(snap.steps.every((s) => s.state === "done"));
});

test("failed turn shows one concise reason, never the raw error payload", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  const snap = t.apply(ev("chat.pipeline.failed", turn({
    op: "chat:req-1", terminal: true,
    error: 'ProviderError: {"status":500,"body":"<html>gateway blew up</html>"} ' +
           "token=ghp_000000000000000000000000000000000000",
  })));
  assert.strictEqual(snap.state, "failed");
  assert.match(snap.summary, /^✕ Failed · ProviderError:?/);
  assert.ok(!snap.summary.includes("ghp_"));
  assert.ok(!snap.summary.includes("<html>"));
  assert.ok(!snap.summary.includes("{"));
  assert.ok(snap.summary.length <= 140);
});

test("the HTTP reply can close a turn the SSE terminal never reached", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(ev("tool.started", turn({ op: "t1", tool: "read_file" })));
  const snap = t.complete();
  assert.strictEqual(snap.state, "completed");
  assert.match(snap.summary, /^✓ Complete/);
  assert.strictEqual(snap.steps[0].state, "done");
});

test("fail() records a redacted one-line reason", () => {
  const t = Status.createTracker();
  t.begin();
  const snap = t.fail("Server e problem — `ghp_000000000000000000000000000000000000`");
  assert.strictEqual(snap.state, "failed");
  assert.match(snap.summary, /^\u2715 Failed · /);
  assert.ok(!snap.summary.includes("ghp_"));
});

/* ------------------------------------------------- out-of-order / staleness */

test("a stale terminal event cannot overwrite the running operation", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  // A long terminal command is running (newest id)...
  t.apply(ev("terminal.started", { op: "term-A", process_id: "pA", command: "sleep 60" }));
  assert.strictEqual(t.snapshot().current, "Running terminal command");
  // ...when an out-of-order replay of an OLDER terminal completion arrives.
  const stale = ev("terminal.completed", { op: "term-OLD", process_id: "pOLD",
    duration_ms: 5, status: "completed", terminal: true }, { id: 2 });
  const snap = t.apply(stale);
  assert.strictEqual(snap.current, "Running terminal command");
  assert.strictEqual(snap.steps.find((s) => s.label === "Running terminal command").state,
                     "active");
});

test("events from a previous turn never touch the current one", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(ev("terminal.started", { op: "term-1", process_id: "p1" }));
  // a delayed provider event correlated to the PREVIOUS request
  const snap = t.apply(ev("ai.started", { request: "req-0", trace: "req-0",
    provider: "gemini" }));
  assert.strictEqual(snap.current, "Running terminal command");
  assert.ok(!JSON.stringify(snap.steps).includes("Gemini"));
});

test("events arriving after the turn ended do not reopen it", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(ev("chat.pipeline.finished", turn({ op: "chat:req-1", terminal: true })));
  assert.strictEqual(t.snapshot().state, "completed");
  const summary = t.snapshot().summary;
  const snap = t.apply(ev("terminal.started", { op: "term-9", process_id: "p9" }));
  assert.strictEqual(snap.state, "completed");
  assert.strictEqual(snap.summary, summary);
  assert.strictEqual(snap.current, "");
});

test("duplicate deliveries of the same event are applied once", () => {
  const t = Status.createTracker();
  t.begin();
  const started = ev("terminal.started", { op: "term-1", process_id: "p1" });
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  t.apply(started);
  t.apply(ev("terminal.completed", { op: "term-1", process_id: "p1",
    duration_ms: 10, status: "completed", terminal: true }));
  const before = t.snapshot().steps.length;
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" }), { id: started.id }));
  t.apply(ev("terminal.completed", { op: "term-1", process_id: "p1",
    duration_ms: 10, status: "completed", terminal: true }));
  assert.strictEqual(t.snapshot().steps.length, before);
  assert.strictEqual(t.snapshot().state, "working");
});

test("heartbeats and internal mirrors never move the status", () => {
  const t = Status.createTracker();
  t.begin();
  t.apply(ev("chat.pipeline.started", turn({ op: "chat:req-1" })));
  const before = t.snapshot();
  ["scheduler.tick", "ai.token", "memory.saved"].forEach((k) =>
    t.apply(ev(k, turn({}))));
  assert.deepStrictEqual(t.snapshot(), before);
  // ...and neither does the Gateway's own ai.* mirror of its own call
  t.apply(ev("ai.started", turn({ provider: "groq" }), { agent: "gateway" }));
  assert.deepStrictEqual(t.snapshot(), before);
});

/* ----------------------------------------------------------- acceptance --- */

test("acceptance: gateway -> provider -> terminal -> provider -> final", () => {
  const t = Status.createTracker();
  t.begin();
  const seen = [];
  const feed = (k, d, o) => {
    t.apply(ev(k, d, o));
    seen.push(t.snapshot().current);
  };
  feed("chat.pipeline.started", turn({ op: "chat:req-1" }), { ms: 0 });
  feed("astra_gateway.request", turn({ provider: "" }), { ms: 200 });
  feed("chat.pipeline.assigned", turn({ provider: "openrouter" }), { ms: 400 });
  feed("ai.started", { provider: "openrouter" }, { ms: 500 });     // no ids: adapter event
  feed("ai.completed", { provider: "openrouter" }, { ms: 1800 });
  feed("tool.started", turn({ op: "tool-1", tool: "terminal_exec" }), { ms: 1900 });
  feed("terminal.started", { op: "term-1", process_id: "p1", command: "pytest -q" },
       { ms: 2000 });
  feed("terminal.completed", { op: "term-1", process_id: "p1", duration_ms: 3400,
       status: "completed", terminal: true }, { ms: 5400 });
  feed("ai.started", { provider: "openrouter" }, { ms: 5500 });
  feed("ai.completed", { provider: "openrouter" }, { ms: 6800 });
  feed("chat.pipeline.finished", turn({ op: "chat:req-1", status: "COMPLETE",
       terminal: true }), { ms: 7000 });

  assert.deepStrictEqual(seen, [
    "Understanding your request",
    "Selecting AI provider",
    "Selecting AI provider",
    "Thinking with OpenRouter",
    "Reviewing provider response",
    "Running terminal command",
    "Running terminal command",
    "Terminal command completed",
    "Thinking with OpenRouter",
    "Reviewing provider response",
    "",
  ]);
  const snap = t.snapshot();
  assert.strictEqual(snap.state, "completed");
  // one step per real operation: request, gateway, provider, terminal,
  // provider again (the second provider call after the command ran)
  assert.strictEqual(snap.summary, "✓ Completed · 5 steps · 7s");
});

test("acceptance: the visible status moves through human states, never ids", () => {
  const t = Status.createTracker();
  t.begin();
  const req = "req-77";
  const lines = [];
  const feed = (k, d) => {
    t.apply(ev(k, Object.assign({ request: req, trace: req }, d)));
    const s = t.snapshot();
    lines.push(s.current || s.summary);
  };
  feed("chat.pipeline.started", { op: "chat:" + req });
  feed("terminal.started", { op: "term-1", process_id: "p1", command: "npm test" });
  feed("terminal.completed", { op: "term-1", process_id: "p1", duration_ms: 2000,
       status: "completed", terminal: true });
  feed("chat.pipeline.finished", { op: "chat:" + req, terminal: true });
  for (const line of lines) {
    assert.ok(!/\breq-77\b/.test(line), `status leaked a request id: ${line}`);
    assert.ok(!/^(op|trace|process_id)[:=]/.test(line));
    assert.ok(line.length <= 140);
  }
});
