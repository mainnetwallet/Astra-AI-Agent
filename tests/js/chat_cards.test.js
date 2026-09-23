/* Unit tests for the Assistant chat EXECUTION-CARD model
 * (static/js/chat_status.js, createCardTracker). Pure logic, no DOM.
 *
 * These feed the SAME real lifecycle shapes the backend emits (tool.started /
 * tool.completed from astra/tools/registry.py, terminal.* from
 * astra/terminal/session.py), and pin the behavioural contract:
 *   * one terminal execution -> exactly one card,
 *   * three terminal executions -> three separate cards, in order,
 *   * terminal.output updates only its own card,
 *   * completed/failed/timeout/stopped land on the SAME card,
 *   * different tool kinds never merge,
 *   * a stale/out-of-order event can never create or overwrite a card,
 *   * secrets never reach a card's title/command/preview.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");

globalThis.AstraLog = require("../../static/js/log_model.js");
const Status = require("../../static/js/chat_status.js");

let NEXT_ID = 1;
test.beforeEach(() => { NEXT_ID = 1; });
function ev(kind, data, opts) {
  const o = opts || {};
  return {
    id: o.id === undefined ? NEXT_ID++ : o.id,
    kind, agent: o.agent || "",
    data: data || {},
    created_at: o.created_at || "2026-09-23 10:00:00",
  };
}
function startTurn(t, extra) {
  t.apply(ev("chat.pipeline.started",
             Object.assign({ op: "chat:req-1", request: "req-1", trace: "req-1" },
                           extra || {})));
}
function tool(t, op, name, extra) {
  t.apply(ev("tool.started",
             Object.assign({ op, tool: name, trace: "req-1" }, extra || {})));
}
function toolDone(t, op, name, extra) {
  t.apply(ev("tool.completed",
             Object.assign({ op, tool: name, duration_ms: 10 }, extra || {})));
}
function termStart(t, pid, command, extra) {
  t.apply(ev("terminal.started", Object.assign({
    op: "term:" + pid, process_id: pid, command: command, cwd: "/repo",
    stdout_blob_id: "blob-out-" + pid, stderr_blob_id: "blob-err-" + pid,
  }, extra || {})));
}
function termDone(t, pid, kind, extra) {
  t.apply(ev(kind || "terminal.completed", Object.assign({
    op: "term:" + pid, process_id: pid, command: "pytest", cwd: "/repo",
    status: "completed", exit_code: 0, duration: 61800, terminal: true,
  }, extra || {})));
}
function fresh() { return Status.createCardTracker(); }
function ids(snaps) { return snaps.map((c) => c.id); }
function titles(snaps) { return snaps.map((c) => c.title); }

/* ------------------------------------------------------------- one -> one */
test("one terminal execution renders exactly one card", () => {
  const t = fresh();
  startTurn(t);
  tool(t, "tool-1", "terminal_exec");
  termStart(t, "p1", "pytest -q");
  const running = t.list();
  assert.strictEqual(running.length, 1, "one terminal_exec -> one card");
  assert.strictEqual(running[0].state, "running");
  assert.strictEqual(running[0].title, "Run tests");
  assert.strictEqual(running[0].command, "pytest -q");
  termDone(t, "p1");
  const done = t.list();
  assert.strictEqual(done.length, 1, "completion must not add a card");
  assert.strictEqual(done[0].state, "completed");
  assert.strictEqual(done[0].duration, "61.8s");
  assert.strictEqual(done[0].exitCode, 0);
});

test("three terminal_exec calls render three separate cards, in order", () => {
  const t = fresh();
  startTurn(t);
  const cmds = ["pytest -q", "cat tests/test_security.py", "sed -i s/x/y/ astra/security.py"];
  cmds.forEach((cmd, i) => {
    const op = "tool-" + i, pid = "p" + i;
    tool(t, op, "terminal_exec");
    termStart(t, pid, cmd);
    termDone(t, pid, "terminal.completed", { duration: 100 * (i + 1) });
    toolDone(t, op, "terminal_exec");
  });
  const cards = t.list();
  assert.strictEqual(cards.length, 3, "three calls -> three cards, never merged");
  assert.deepStrictEqual(titles(cards),
                         ["Run tests", "Read test_security.py", "Edit security.py"]);
  assert.deepStrictEqual(cards.map((c) => c.processId), ["p0", "p1", "p2"]);
  assert.deepStrictEqual(cards.map((c) => c.state),
                         ["completed", "completed", "completed"]);
  assert.deepStrictEqual(cards.map((c) => c.duration), ["100ms", "200ms", "300ms"]);
});

test("two terminal calls for the same command stay two cards", () => {
  const t = fresh();
  startTurn(t);
  tool(t, "tool-1", "terminal_exec");
  termStart(t, "p1", "pytest -q");
  termDone(t, "p1");
  toolDone(t, "tool-1", "terminal_exec");
  tool(t, "tool-2", "terminal_exec");
  termStart(t, "p2", "pytest -q");
  termDone(t, "p2");
  toolDone(t, "tool-2", "terminal_exec");
  const cards = t.list();
  assert.strictEqual(cards.length, 2);
  assert.notStrictEqual(cards[0].id, cards[1].id);
});

/* ------------------------------------------------------- state transitions */
test("terminal.started opens a running card and completion closes THAT card", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "sleep 5");
  const card = t.list()[0];
  assert.strictEqual(card.state, "running");
  assert.strictEqual(card.stateText, "Running");
  assert.strictEqual(card.duration, "", "no invented duration while running");
  termDone(t, "p1", "terminal.completed", { duration: 5300 });
  assert.strictEqual(t.list()[0].id, card.id, "same card, not a new one");
  assert.strictEqual(t.list()[0].state, "completed");
  assert.strictEqual(t.list()[0].duration, "5.3s");
});

test("terminal.failed / timeout / stopped mark the same card", () => {
  for (const [kind, state, status, text] of [
    ["terminal.failed", "failed", "failed", "Failed"],
    ["terminal.timeout", "failed", "timeout", "Timed out"],
    ["terminal.stopped", "stopped", "stopped", "Stopped"],
  ]) {
    const t = fresh();
    startTurn(t);
    termStart(t, "p1", "make build");
    const id = t.list()[0].id;
    termDone(t, "p1", kind, {
      status: status, exit_code: 1, duration: 900,
    });
    const card = t.list()[0];
    assert.strictEqual(card.id, id, kind + " must land on the same card");
    assert.strictEqual(card.state, state, kind + " state");
    assert.strictEqual(card.stateText, text, kind + " label");
  }
});

test("terminal.output updates only its own card", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "pytest");
  termStart(t, "p2", "npm test");
  t.apply(ev("terminal.output", { op: "term:p2", process_id: "p2",
                                  stream: "stdout", chars: 12,
                                  snippet: "2 passing", status: "running" }));
  const cards = t.list();
  assert.strictEqual(cards.length, 2);
  assert.strictEqual(cards[0].preview, "");
  assert.strictEqual(cards[1].preview, "2 passing");
  assert.strictEqual(cards[0].state, "running");
  assert.strictEqual(cards[1].state, "running");
});

/* --------------------------------------------------- different tool kinds */
test("browser, file, web, web3 and terminal cards never merge", () => {
  const t = fresh();
  startTurn(t);
  tool(t, "tool-t", "terminal_exec");
  termStart(t, "p1", "pytest");
  tool(t, "tool-b", "browser_open");
  tool(t, "tool-f", "read_file");
  tool(t, "tool-w", "search_web");
  tool(t, "tool-c", "token_balance");
  const kinds = t.list().map((c) => c.kind);
  assert.deepStrictEqual(kinds,
    ["terminal", "browser", "file", "web", "web3"]);
  assert.strictEqual(t.list().length, 5);
});

test("tool completions close their own card only", () => {
  const t = fresh();
  startTurn(t);
  tool(t, "tool-b", "browser_open");
  tool(t, "tool-f", "read_file");
  toolDone(t, "tool-f", "read_file", { duration_ms: 200 });
  const cards = t.list();
  assert.strictEqual(cards[0].state, "running");
  assert.strictEqual(cards[1].state, "completed");
  assert.strictEqual(cards[1].duration, "200ms");
});

test("bookkeeping tools never create a card", () => {
  const t = fresh();
  startTurn(t);
  ["recall", "remember", "create_task", "list_tasks", "get_health",
   "terminal_status", "terminal_output_read", "execution_history_read"]
    .forEach((name, i) => tool(t, "tool-" + i, name));
  assert.strictEqual(t.list().length, 0);
});

/* -------------------------------------------------------- stale / ordering */
test("a stale (lower-id) terminal event cannot create or overwrite a card", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "sleep 60");
  const card = t.list()[0];
  // an old execution replayed late, with a LOWER id than everything applied
  t.apply(ev("terminal.started", { op: "term:old", process_id: "pOLD",
                                   command: "rm -rf /" }, { id: 1 }));
  assert.strictEqual(t.list().length, 1, "stale start must not add a card");
  t.apply(ev("terminal.completed", { op: "term:old", process_id: "pOLD",
                                     status: "completed", duration: 10 },
             { id: 2 }));
  assert.strictEqual(t.list().length, 1);
  assert.strictEqual(t.list()[0].id, card.id);
  assert.strictEqual(t.list()[0].state, "running",
                     "stale completion must not finish another card");
});

test("an out-of-order event only ever touches the card it really owns", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "pytest");
  termStart(t, "p2", "npm test");
  // an old execution's completion arrives late, below every id we applied
  t.apply(ev("terminal.completed", { op: "term:old", process_id: "pOLD",
                                     status: "completed", duration: 100 },
             { id: 1 }));
  assert.deepStrictEqual(t.list().map((c) => c.state),
                         ["running", "running"]);
  // a replay of p1's own start must never fork a second card
  t.apply(ev("terminal.started", { op: "term:p1", process_id: "p1",
                                   command: "pytest" }, { id: 2 }));
  assert.strictEqual(t.list().length, 2);
  // and p1's real completion closes p1 only
  termDone(t, "p1", "terminal.completed", { duration: 100 });
  assert.deepStrictEqual(t.list().map((c) => c.state),
                         ["completed", "running"]);
});

test("events outside a turn never create cards", () => {
  const t = fresh();
  termStart(t, "p1", "pytest");           // no chat.pipeline.started yet
  assert.strictEqual(t.list().length, 0);
  startTurn(t);
  assert.strictEqual(t.list().length, 0, "the turn starts with an empty list");
  termStart(t, "p2", "pytest");
  assert.strictEqual(t.list().length, 1);
  t.apply(ev("chat.pipeline.finished", { op: "chat:req-1", terminal: true }));
  termStart(t, "p3", "pytest");           // after the turn ended
  assert.strictEqual(t.list().length, 1, "post-turn events are ignored");
});

test("duplicate deliveries of the same event are applied once", () => {
  const t = fresh();
  startTurn(t);
  const start = ev("terminal.started", { op: "term:p1", process_id: "p1",
                                         command: "pytest" });
  t.apply(start);
  t.apply(start);
  assert.strictEqual(t.list().length, 1);
});

/* ------------------------------------------------------------ turn ending */
test("a card still running when the turn ends is marked Ended, with no fake duration", () => {
  const t = fresh();
  startTurn(t);
  tool(t, "tool-1", "terminal_exec");
  termStart(t, "p1", "sleep 100");
  t.apply(ev("chat.pipeline.finished", { op: "chat:req-1", terminal: true }));
  const card = t.list()[0];
  assert.strictEqual(card.state, "completed");
  assert.strictEqual(card.stateText, "Ended");
  assert.strictEqual(card.duration, "", "never invent a duration");
});

/* ------------------------------------------------------------- redaction */
test("secrets in a command/output preview never reach the card", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "curl -H 'Authorization: Bearer ghp_0123456789abcdefghijklmnopqrstuvwxyz' https://x.test");
  t.apply(ev("terminal.output", { op: "term:p1", process_id: "p1",
                                  stream: "stdout", chars: 60,
                                  snippet: "api_key=sk-abcdefghijklmnopqrstuvwxyz done" }));
  const card = t.list()[0];
  const blob = JSON.stringify(card);
  assert.ok(!blob.includes("ghp_"), "github token must be redacted");
  assert.ok(!blob.includes("sk-abcdefghijklmnopqrstuvwxyz"), "api key must be redacted");
  assert.ok(blob.includes("***redacted***"));
});

test("the card preview is one short line, never a raw output dump", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "cat dump.json");
  t.apply(ev("terminal.output", { op: "term:p1", process_id: "p1",
                                  stream: "stdout", chars: 9000,
                                  snippet: "x".repeat(4000) }));
  const card = t.list()[0];
  assert.ok(card.preview.length <= 140, card.preview.length);
  assert.ok(!card.preview.includes("\n"));
});

/* ------------------------------------------------------- human action map */
test("terminal commands map to deterministic human actions", () => {
  const A = Status.terminalAction;
  assert.strictEqual(A("pytest -q"), "Run tests");
  assert.strictEqual(A("python -m pytest tests/"), "Run tests");
  assert.strictEqual(A("npm test"), "Run tests");
  assert.strictEqual(A("cat tests/test_security.py"), "Read test_security.py");
  assert.strictEqual(A("sed -i 's/a/b/' astra/security.py"), "Edit security.py");
  assert.strictEqual(A("ls -la"), "List files");
  assert.strictEqual(A("git status"), "Check git status");
  assert.strictEqual(A("pip install -r requirements.txt"), "Install packages");
  assert.strictEqual(A("python3 run.py"), "Run run.py");
  assert.strictEqual(A(""), "Running terminal command");
});

test("kindForTool classifies the real tool names", () => {
  assert.strictEqual(Status.kindForTool("terminal_exec"), "terminal");
  assert.strictEqual(Status.kindForTool("browser_open"), "browser");
  assert.strictEqual(Status.kindForTool("write_file"), "file");
  assert.strictEqual(Status.kindForTool("fetch_url"), "web");
  assert.strictEqual(Status.kindForTool("tx_prepare"), "web3");
  assert.strictEqual(Status.kindForTool("unknown_thing"), "tool");
});

/* ---------------------------------------------------------- acceptance --- */
test("acceptance: terminal -> completed -> next terminal -> completed", () => {
  const t = fresh();
  startTurn(t);
  tool(t, "tool-1", "terminal_exec");
  termStart(t, "p1", "pytest -q");
  assert.strictEqual(t.list()[0].state, "running");
  termDone(t, "p1", "terminal.completed", { duration: 61800 });
  toolDone(t, "tool-1", "terminal_exec");
  assert.strictEqual(t.list()[0].state, "completed");
  assert.strictEqual(t.list()[0].duration, "61.8s");
  tool(t, "tool-2", "terminal_exec");
  termStart(t, "p2", "pytest -q");
  assert.deepStrictEqual(t.list().map((c) => c.state),
                         ["completed", "running"]);
  termDone(t, "p2", "terminal.completed", { duration: 2400 });
  assert.deepStrictEqual(t.list().map((c) => c.state),
                         ["completed", "completed"]);
  assert.strictEqual(t.list()[1].duration, "2.4s");
});

test("the card list stays bounded and chronological", () => {
  const t = fresh();
  startTurn(t);
  for (let i = 0; i < Status.MAX_CARDS + 10; i++) {
    termStart(t, "p" + i, "echo " + i);
  }
  const cards = t.list();
  assert.strictEqual(cards.length, Status.MAX_CARDS);
  assert.strictEqual(cards[cards.length - 1].processId,
                     "p" + (Status.MAX_CARDS + 9));
  const pids = cards.map((c) => Number(c.processId.slice(1)));
  for (let i = 1; i < pids.length; i++) assert.ok(pids[i] > pids[i - 1]);
});

test("reset() drops every card", () => {
  const t = fresh();
  startTurn(t);
  termStart(t, "p1", "pytest");
  assert.strictEqual(t.list().length, 1);
  t.reset();
  assert.strictEqual(t.list().length, 0);
});
