"""End-to-end acceptance for the Assistant chat execution status.

This runs a REAL chat turn on the REAL stack (the same `astra.bootstrap.build()`
`run.py` uses — real ToolRegistry, real TerminalManager, real AgentToolLoop,
real ChatPipeline; only the two network edges — the Gateway's own AI call and
the Provider's model call — are scripted), captures the lifecycle events the
turn actually emitted, and then feeds those events, in order, through the REAL
frontend status model (static/js/chat_status.js, executed by node).

So the assertion is not on a mock: it is "the events a real turn produces make
the chat status visibly move through the right human-readable states".

Acceptance sequence asserted here (Gateway -> Provider -> terminal.started ->
terminal.completed -> Provider -> final response):

    Understanding your request -> <provider working> -> Running terminal
    command -> Terminal command completed -> <provider continues> ->
    ✓ Completed · N steps · <duration>

plus: a failing command must NOT end the turn (the agent fixes it), raw tool
output/ids/secrets must never reach the status line, and a root failure must
show "✕ Failed · <one human reason>".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests.test_gateway_provider_tool_architecture import (
    Harness, final, tool_call, understand, verdict)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_MODEL = os.path.join(ROOT, "static", "js", "chat_status.js")
LOG_MODEL = os.path.join(ROOT, "static", "js", "log_model.js")

DRIVER = """
"use strict";
const S = require(%(model)s);
const fs = require("fs");
const events = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const t = S.createTracker();
t.begin();
const lines = [];
const lineStates = [];
for (const e of events) {
  t.apply(e);
  const s = t.snapshot();
  lines.push(s.current || s.summary);
  lineStates.push(s.state);
}
const s = t.snapshot();
console.log(JSON.stringify({lines: lines, states: lineStates, summary: s.summary,
                            state: s.state, steps: s.steps,
                            failure: s.failure}));
"""

# The execution-card model over the same real events: one card per real tool
# execution, in the order the turn ran them.
CARDS_DRIVER = """
"use strict";
const S = require(%(model)s);
const fs = require("fs");
const events = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const t = S.createCardTracker();
for (const e of events) t.apply(e);
console.log(JSON.stringify({cards: t.list()}));
"""


class RecordingHarness(Harness):
    """Harness + the full event records (id/created_at) the turn emitted."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        events = self.stack["events"]
        inner = events.emit
        self.records = []

        def emit(kind, agent="", **data):
            rec = inner(kind, agent=agent, **data)
            self.records.append(rec)
            return rec

        events.emit = emit

    def feed_to_status_model(self):
        """Run the REAL frontend model over the REAL emitted events."""
        return self._run_model(DRIVER)

    def feed_to_cards_model(self):
        """Run the REAL frontend CARD model over the REAL emitted events."""
        return self._run_model(CARDS_DRIVER)

    def _run_model(self, template):
        driver_src = template % {"model": json.dumps(STATUS_MODEL)}
        # the model reuses the Activity Log's redaction/noise filter, and the
        # browser loads log_model.js before it — mirror that load order here.
        prelude_src = ("globalThis.AstraLog = require(%s);\n"
                       % json.dumps(LOG_MODEL))
        tmp = tempfile.mkdtemp(prefix="astra-chat-status-")
        try:
            driver = os.path.join(tmp, "driver.js")
            prelude = os.path.join(tmp, "prelude.js")
            payload = os.path.join(tmp, "events.json")
            with open(driver, "w", encoding="utf-8") as fh:
                fh.write(driver_src)
            with open(prelude, "w", encoding="utf-8") as fh:
                fh.write(prelude_src)
            with open(payload, "w", encoding="utf-8") as fh:
                json.dump(self.records, fh)
            proc = subprocess.run(
                ["node", "-r", prelude, driver, payload],
                cwd=ROOT, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise AssertionError(
                    f"node status model failed:\n{proc.stdout}\n{proc.stderr}")
            return json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def kinds(self):
        return [r["kind"] for r in self.records]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ChatStatusAcceptanceTests(unittest.TestCase):
    def test_gateway_provider_terminal_provider_final_sequence(self):
        h = RecordingHarness(
            [understand("Clone the repo.", required=True,
                        capability="terminal", intent="clone the repo"),
             verdict("complete")],
            [tool_call("echo astra-clone-ok"),
             final("Cloned: astra-clone-ok")])
        out = h.run("GitHub repo clone koro")
        self.assertTrue(out["ok"])

        # the turn really ran the terminal capability
        self.assertIn("terminal.started", h.kinds())
        self.assertIn("terminal.completed", h.kinds())

        status = h.feed_to_status_model()
        lines = status["lines"]
        self.assertEqual(lines[0], "Understanding your request")
        self.assertEqual(status["state"], "completed")
        self.assertRegex(status["summary"], r"^✓ Completed · \d+ steps · \S+$")

        def idx(text):
            for i, line in enumerate(lines):
                if text in line:
                    return i
            raise AssertionError(f"{text!r} never appeared in {lines}")

        i_terminal = idx("Running terminal command")
        i_done = idx("Terminal command completed")
        self.assertLess(idx("Understanding your request"), i_terminal)
        self.assertLess(i_terminal, i_done)
        # the provider (or the tool loop) is working again after the command
        self.assertGreater(idx("Reviewing results"), i_done)
        # ...and the last visible line is the completion summary, not a step
        self.assertEqual(lines[-1], status["summary"])
        self.assertTrue(lines[-1].startswith("✓ Completed"))

        # completed steps carry a duration, and the failed step never exists
        labels = [s["label"] for s in status["steps"]]
        self.assertIn("Running terminal command", labels)
        terminal_step = next(s for s in status["steps"]
                             if s["label"] == "Running terminal command")
        self.assertEqual(terminal_step["state"], "done")
        self.assertTrue(terminal_step["duration"], "completed step shows a duration")

        # no raw tool output, ids or payloads in the user-visible lines
        for line in lines:
            self.assertNotIn("astra-clone-ok", line)
            self.assertNotIn("{", line)
            self.assertNotRegex(line, r"req-|op:|trace:|process_id")
            self.assertLessEqual(len(line), 140)

    def test_multiple_terminal_calls_render_separate_cards(self):
        """A real turn that runs three commands produces three cards."""
        sample = os.path.join(tempfile.mkdtemp(prefix="astra-cards-"),
                              "sample.txt")
        with open(sample, "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        h = RecordingHarness(
            [understand("Run three commands.", required=True,
                        capability="terminal", intent="run three commands"),
             verdict("complete")],
            [tool_call("echo astra-one"),
             tool_call("cat " + sample),
             tool_call("echo astra-three"),
             final("All three ran.")])
        out = h.run("Run three commands and tell me about it")
        self.assertTrue(out["ok"])
        self.assertEqual(len([k for k in h.kinds() if k == "terminal.started"]), 3,
                         "the turn really ran three terminal executions")

        cards = h.feed_to_cards_model()["cards"]
        self.assertEqual(len(cards), 3, "one card per terminal execution")
        self.assertTrue(all(c["kind"] == "terminal" for c in cards))
        self.assertTrue(all(c["state"] == "completed" for c in cards),
                        [c["state"] for c in cards])
        self.assertEqual([c["title"] for c in cards],
                         ["Run command", "Read sample.txt", "Run command"])
        # every card is one distinct real execution
        self.assertEqual(len({c["processId"] for c in cards}), 3)
        self.assertEqual(len({c["id"] for c in cards}), 3)
        # and each card carries the handle for its own full output
        self.assertTrue(all(c["blobId"] for c in cards))
        # the card carries a SHORT single-line preview, never the raw stream
        for c in cards:
            self.assertLessEqual(len(c["preview"]), 140)
            self.assertNotIn("\n", c["preview"])
        blob = json.dumps(cards)
        self.assertNotIn('"stdout"', blob)
        self.assertNotIn('"args"', blob)

    def test_failing_command_does_not_end_the_turn(self):
        h = RecordingHarness(
            [understand("Run the tests and fix the failure.", required=True,
                        capability="terminal", intent="fix the failing tests"),
             verdict("complete")],
            [tool_call("exit 7"),
             tool_call("echo fixed"),
             final("Fixed the failing test.")])
        out = h.run("Run the tests, inspect the failure and fix it.")
        self.assertTrue(out["ok"])
        status = h.feed_to_status_model()
        lines = status["lines"]

        # the failed command shows as a failure with its own human label ...
        i_failed = next(i for i, l in enumerate(lines)
                        if "Terminal command failed" in l)
        # ... and the turn keeps working afterwards
        self.assertGreater(len(lines) - 1, i_failed)
        self.assertIn("Running terminal command", lines[i_failed + 1:])
        self.assertEqual(status["state"], "completed")
        failed_steps = [s for s in status["steps"] if s["state"] == "failed"]
        self.assertTrue(failed_steps, "the failing command stays visible as failed")
        done_steps = [s for s in status["steps"] if s["state"] == "done"]
        self.assertTrue(done_steps, "the retried command is recorded as done")
        self.assertRegex(status["summary"], r"^✓ Completed · \d+ steps · \S+$")

    def test_root_failure_shows_one_human_reason(self):
        from astra.core.exceptions import ProviderError

        h = RecordingHarness(
            [understand("Do it.", required=True, capability="terminal",
                        intent="do it"),
             verdict("complete")],
            [ProviderError("groq: no healthy credential configured")])
        out = h.run("Run it")
        self.assertFalse(out["ok"])
        status = h.feed_to_status_model()
        self.assertEqual(status["state"], "failed")
        self.assertRegex(status["summary"], r"^✕ Failed · \S")
        self.assertNotIn("{", status["summary"])
        self.assertLessEqual(len(status["summary"]), 145)


if __name__ == "__main__":
    unittest.main()
