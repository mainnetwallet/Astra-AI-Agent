"""Contract for the Assistant chat EXECUTION CARDS.

The behaviour lives in JS: the DOM-free card model in static/js/chat_status.js
(tests/js/chat_cards.test.js) and the real UI wiring in static/js/astra.js
(tests/js/chat_cards_ui.test.js, driven over tests/js/dom_shim.js).

This test locks the structural contract the markup, the stylesheet and the
event wiring must keep, and runs the node suites when node is available.

What must never regress:
  * a REAL collection of card DOM elements, one per real tool execution —
    not a restyle of the step timeline,
  * the compact live status stays ABOVE them,
  * every card is a >=44px tappable control whose click only toggles detail,
  * the full output is a separate, explicit, read-only GET,
  * the Activity Log stays the detailed timeline (untouched).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_TESTS = [os.path.join(ROOT, "tests", "js", "chat_cards.test.js"),
            os.path.join(ROOT, "tests", "js", "chat_cards_ui.test.js")]


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestChatCardsUI(unittest.TestCase):
    def setUp(self):
        self.js = _read("static", "js", "astra.js")
        self.model = _read("static", "js", "chat_status.js")
        self.css = _read("static", "css", "style.css")
        self.web = _read("astra", "web.py")
        # only the card section of the shared model file
        start = self.model.index("execution cards --")
        end = self.model.index("--------------------------------------------------------------- helpers */")
        self.card_model = self.model[start:end]

    # -- a real card collection, not a restyled timeline --------------------
    def test_cards_are_a_separate_dom_collection(self):
        for anchor in ("chat-cards", "chat-exec", "chat-exec-card",
                       "chat-exec-mark", "chat-exec-title", "chat-exec-dur",
                       "chat-exec-detail", "chat-exec-more"):
            self.assertIn(anchor, self.js, f"astra.js is missing {anchor}")
            self.assertIn(anchor, self.css, f"style.css is missing {anchor}")
        # one element per card, keyed so an update never duplicates it
        self.assertIn("CHAT_CARDS.els[snap.id]", self.js)
        self.assertIn("chatCardsPaint", self.js)
        self.assertIn("chatCardsTrack", self.js)

    def test_cards_render_below_the_compact_live_status(self):
        # the cards container is appended to the SAME row, after the status
        self.assertIn("content.append(btn, steps, cards);", self.js)
        self.assertIn("cards.className = \"chat-cards\";", self.js)

    def test_card_model_is_shared_and_dom_free(self):
        self.assertIn("createCardTracker", self.model)
        self.assertIn("createCardTracker()", self.js)
        for banned in ("document.", "innerHTML", "addEventListener",
                       "EventSource", "fetch(", "setInterval", "setTimeout",
                       "Math.random", "Date.now"):
            self.assertNotIn(banned, self.card_model,
                             f"the card model must not own {banned}")

    def test_cards_are_driven_by_the_existing_event_stream(self):
        # the ONE entry point the SSE frame handler and the polling fallback
        # both call is chatStatusTrack(); it feeds the cards too.
        track = self.js[self.js.index("function chatStatusTrack"):]
        track = track[:track.index("function chatCardsTrack")]
        self.assertIn("chatCardsTrack(event);", track)
        sse = self.js[self.js.index("function ensureEventStream"):]
        sse = sse[:sse.index("function openSse")]
        self.assertIn("chatStatusTrack(e);", sse)
        poll = self.js[self.js.index("async function eventsPoll"):]
        poll = poll[:poll.index("// SSE connection state")]
        self.assertIn("chatStatusTrack(e);", poll)
        # the card model consumes the real tool/terminal lifecycle kinds
        for kind in ("tool.started", "tool.completed", "tool.failed",
                     "terminal.started", "terminal.output",
                     "terminal.completed", "terminal.failed",
                     "terminal.timeout", "terminal.stopped"):
            self.assertIn(kind, self.model, f"the card model skips {kind}")

    def test_no_fake_events_timers_or_progress(self):
        for banned in ("setInterval", "setTimeout", "requestAnimationFrame",
                       "Math.random", "Date.now"):
            self.assertNotIn(banned, self.card_model,
                             f"the card model must not fabricate progress: {banned}")

    # -- tapping a card never executes anything ------------------------------
    def test_card_click_only_toggles_its_detail(self):
        start = self.js.index('btn.addEventListener("click", () => {',
                              self.js.index("function chatCardEl"))
        block = self.js[start:start + 400]
        block = block[:block.index("});")]
        self.assertIn("classList.toggle", block)
        self.assertIn("detail.hidden", block)
        self.assertIn("aria-expanded", block)
        for banned in ("post(", "fetch(", "del(", "loaders.", "chatBubble(",
                       "requestSubmit", "submit(", "execute"):
            self.assertNotIn(banned, block,
                             f"tapping a card must not {banned}")

    def test_full_output_is_an_explicit_read_only_get(self):
        load = self.js[self.js.index("async function chatCardLoadOutput"):]
        load = load[:load.index("function chatCardOutput")]
        self.assertIn("/api/terminal/output?", load)
        self.assertIn("api(url)", load)
        self.assertIn("blob_id=", load)
        # the only place the card path touches the network
        paint = self.js[self.js.index("function chatCardsPaint"):]
        paint = paint[:paint.index("// One live event")]
        self.assertNotIn("api(", paint)

    def test_output_is_never_dumped_into_the_chat(self):
        # the card carries a short scrubbed preview; the raw stream only ever
        # appears behind the explicit load button (a <pre>, not the bubble)
        self.assertIn("CHAT_CARD_OUT_CHUNK", self.js)
        self.assertIn("chat-exec-pre", self.css)
        self.assertIn("pre.textContent", self.js)

    def test_activity_log_is_untouched(self):
        self.assertIn("/api/events?limit=500", self.js)
        self.assertIn("AstraLog.planRender(", self.js)
        self.assertNotIn("live-feed", self.js[self.js.index("function chatCardsPaint"):
                                              self.js.index("function chatCardsTrack")])

    # -- the read-only retrieval endpoint ------------------------------------
    def test_terminal_output_endpoint_exists(self):
        self.assertIn('path == ["api", "terminal", "output"]', self.web)
        self.assertIn("def _terminal_output", self.web)
        self.assertIn("def _terminal_output_payload", self.web)
        # read-only: it never starts a session and never executes
        block = self.web[self.web.index("def _terminal_output(self, req"):]
        block = block[:block.index("# -- main route table")]
        self.assertIn("create=False", block)
        for banned in ("session.exec(", ".exec(", "session.start(", "stop("):
            self.assertNotIn(banned, block,
                             f"the output endpoint must never {banned}")

    # -- mobile --------------------------------------------------------------
    def test_mobile_stylesheet_rules(self):
        block = self.css[self.css.index(".chat-exec-card {"):]
        block = block[:block.index("}")]
        for rule in ("width: 100%", "max-width: 100%", "box-sizing: border-box",
                     "min-height: 44px", "overflow-wrap: anywhere"):
            self.assertIn(rule, block, f"missing mobile rule {rule!r}")
        # long text ellipsises instead of pushing the bubble wide
        sub = self.css[self.css.index(".chat-exec-sub {"):]
        sub = sub[:sub.index("}")]
        self.assertIn("text-overflow: ellipsis", sub)
        self.assertIn("white-space: nowrap", sub)
        more = self.css[self.css.index(".chat-exec-more {"):]
        more = more[:more.index("}")]
        self.assertIn("min-height: 36px", more)

    # -- the card model's own node suite -------------------------------------
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_node_suites_pass(self):
        proc = subprocess.run(["node", "--test", "--test-timeout=30000"] + JS_TESTS,
                              cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"node tests failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
