"""Contract for the Assistant chat execution status.

The behaviour lives in JS: the pure lifecycle -> human-status mapping in
static/js/chat_status.js (tests/js/chat_status.test.js) and the real UI wiring
in static/js/astra.js (tests/js/chat_status_ui.test.js, driven over a DOM +
EventSource shim). This test locks the structural contract the backend/static
serving, the panel markup and the stylesheet must keep, and runs both node
suites when node is available.

What it must never regress:
  * the compact status replaces the typing-only dots,
  * it is driven by the EXISTING event stream (no second event system, no
    timers, no simulated progress),
  * tapping it never executes a tool,
  * the Activity Log stays the detailed timeline.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_TESTS = [os.path.join(ROOT, "tests", "js", "chat_status.test.js"),
            os.path.join(ROOT, "tests", "js", "chat_status_ui.test.js")]


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestChatStatusUI(unittest.TestCase):
    def setUp(self):
        self.html = _read("static", "index.html")
        self.js = _read("static", "js", "astra.js")
        self.model = _read("static", "js", "chat_status.js")
        self.css = _read("static", "css", "style.css")

    # -- served / loaded ---------------------------------------------------
    def test_model_is_served_before_astra_js(self):
        self.assertIn("/static/js/chat_status.js", self.html)
        self.assertLess(self.html.index('src="/static/js/log_model.js"'),
                        self.html.index('src="/static/js/chat_status.js"'))
        self.assertLess(self.html.index('src="/static/js/chat_status.js"'),
                        self.html.index('src="/static/js/astra.js"'))

    # -- the compact status replaces typing-only dots ----------------------
    def test_typing_only_indicator_is_gone(self):
        self.assertNotIn('class="typing"', self.js)
        self.assertNotIn('<div class="typing">', self.js)
        self.assertNotIn(".typing {", self.css)
        # the compact status: avatar + "Working…" + three animated dots
        self.assertIn("chat-status-row", self.js)
        for anchor in ("chat-status-summary", "chat-status-dots",
                       "chat-status-action", "chat-steps", "chat-step-mark",
                       "chat-step-dur"):
            self.assertIn(anchor, self.js, f"astra.js is missing {anchor}")
            self.assertIn(anchor, self.css, f"style.css is missing {anchor}")

    def test_compact_status_uses_real_event_text_only(self):
        # Dynamic text goes through textContent — an event can never inject
        # markup, and raw payloads have no path into the chat line.
        self.assertIn("CHAT_STATUS.summary.textContent", self.js)
        self.assertIn("CHAT_STATUS.line.textContent", self.js)
        self.assertIn("label.textContent = step.label", self.js)
        for dangerous in ("innerHTML = ", "insertAdjacentHTML"):
            block = self.js[self.js.index("function chatStatusPaint"):]
            block = block[:block.index("function chatStatusTrack")]
            self.assertNotIn(dangerous, block,
                             f"chatStatusPaint must not build DOM from {dangerous!r}")

    # -- driven by the existing lifecycle events ---------------------------
    def test_status_is_driven_by_the_existing_event_stream(self):
        self.assertIn("chatStatusTrack(", self.js)
        # both real arrival paths feed it: the SSE frame handler and the
        # polling fallback
        sse = self.js[self.js.index("function ensureEventStream"):]
        sse = sse[:sse.index("function openSse")]
        self.assertIn("receiveEvent(e);", sse)
        self.assertIn("chatStatusTrack(e);", sse)
        poll = self.js[self.js.index("async function eventsPoll"):]
        poll = poll[:poll.index("// SSE connection state")]
        self.assertIn("chatStatusTrack(e);", poll)
        # one shared connection, still the existing endpoint
        self.assertIn('"/api/events/stream"', self.js)
        self.assertIn("let EVENT_SOURCE = null;", self.js)
        self.assertIn("function openSse(afterId) { return ensureEventStream(afterId); }",
                      self.js)

    def test_no_second_event_system_or_simulated_progress(self):
        for banned in ("EventSource", "fetch(", "XMLHttpRequest",
                       "setInterval", "setTimeout", "requestAnimationFrame",
                       "Math.random"):
            self.assertNotIn(banned, self.model,
                             f"chat_status.js must not own {banned}")
        # the mapping is over the real lifecycle kinds
        for kind in ("chat.pipeline.started", "chat.pipeline.finished",
                     "chat.pipeline.failed", "astra_gateway.request",
                     "ai.started", "terminal.started", "terminal.completed",
                     "terminal.failed", "browser.", "tool.started",
                     "web3.transaction.confirmed", "workflow.started",
                     "chat.pipeline.verified"):
            self.assertIn(kind, self.model, f"the status mapping skips {kind}")

    def test_noise_filter_and_redaction_are_reused_not_reimplemented(self):
        self.assertIn("lm.isMeaningful", self.model)
        self.assertIn("lm.scrub", self.model)
        self.assertIn("lm.fmtMs", self.model)
        self.assertIn("lm.elapsedMs", self.model)
        # the redaction regex itself must live in exactly one place
        self.assertNotIn("ghp_", self.model)
        self.assertNotIn("sk-[A-Za-z0-9", self.model)

    def test_human_status_lines_exist(self):
        for text in ("Working…", "Understanding your request",
                     "Selecting AI provider", "Running terminal command",
                     "Reading file", "Editing file", "Opening webpage",
                     "Reading webpage", "Checking blockchain data",
                     "Reviewing results", "Terminal command completed",
                     "Terminal command failed", "✓ Completed", "✕ Failed"):
            self.assertIn(text, self.model, f"missing status line {text!r}")

    # -- tapping never executes anything -----------------------------------
    def test_click_only_toggles_the_timeline(self):
        start = self.js.index('btn.addEventListener("click"')
        block = self.js[start:start + 400]
        block = block[:block.index("});")]
        self.assertIn("classList.toggle", block)
        self.assertIn("steps.hidden", block)
        self.assertIn("aria-expanded", block)
        for banned in ("post(", "fetch(", "del(", "loaders.", "chatBubble(",
                       "chatTyping(", "requestSubmit", "submit("):
            self.assertNotIn(banned, block,
                             f"tapping the status must not {banned}")

    def test_expandable_timeline_is_inline_and_accessible(self):
        self.assertIn('btn.setAttribute("aria-expanded"', self.js)
        self.assertIn('summary.setAttribute("role", "status")', self.js)
        self.assertIn('summary.setAttribute("aria-live", "polite")', self.js)
        self.assertIn("steps.hidden = true;", self.js)

    # -- mobile ------------------------------------------------------------
    def test_mobile_stylesheet_rules(self):
        block = self.css[self.css.index(".chat-status {"):]
        block = block[:block.index("}")]
        for rule in ("width: 100%", "max-width: 100%", "box-sizing: border-box",
                     "overflow-wrap: anywhere", "min-height: 44px"):
            self.assertIn(rule, block, f"missing mobile rule {rule!r}")
        self.assertIn(".chat-steps[hidden] { display: none; }", self.css)
        self.assertIn(".chat-step-dur", self.css)

    # -- the Activity Log is untouched -------------------------------------
    def test_activity_log_architecture_unchanged(self):
        self.assertIn("/api/events?limit=500", self.js)
        self.assertIn("AstraLog.normalize(", self.js)
        self.assertIn("AstraLog.planRender(", self.js)
        self.assertIn("AstraLog.isMeaningful(", self.js)
        # the chat status never writes into the log feed
        paint = self.js[self.js.index("function chatStatusPaint"):]
        paint = paint[:paint.index("// One live event")]
        self.assertNotIn("live-feed", paint)

    # -- the model's own node suite ----------------------------------------
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_node_suites_pass(self):
        proc = subprocess.run(["node", "--test", "--test-timeout=20000"] + JS_TESTS,
                              cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"node tests failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
