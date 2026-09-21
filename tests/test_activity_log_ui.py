"""Static contract for the Activity Log panel.

The timeline behaviour lives in JS (covered by tests/js/log_model.test.js under
node). This test locks the structural contract the backend/static serving and
the panel markup must keep: the real DOM anchors the JS binds to, the filter
chips, chronological append (never prepend), and the stylesheet for the
timeline/jump-to-latest affordances.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestActivityLogUI(unittest.TestCase):
    def setUp(self):
        self.html = _read("static", "index.html")
        self.js = _read("static", "js", "astra.js")
        self.css = _read("static", "css", "style.css")

    def test_panel_anchors_exist(self):
        for anchor in ('id="live-feed"', 'id="logs-jump"', 'id="logs-live"',
                       'id="logs-count"', 'id="logs-stats"', 'id="logs-filters"',
                       'id="logs-search"', 'id="btn-logs-pause"',
                       'id="btn-logs-copy"', 'id="btn-logs-clear"'):
            self.assertIn(anchor, self.html, f"missing {anchor}")

    def test_filter_chips_match_the_requested_categories(self):
        for chip in ("all", "agents", "ai", "tools", "browser", "web3", "errors"):
            self.assertIn(f'data-filter="{chip}"', self.html)

    def test_logs_are_appended_not_prepended(self):
        feed_section = self.js[self.js.index("function appendEvent"):]
        self.assertIn("feed.appendChild(", feed_section)
        # the old newest-first rendering prepended; the timeline must not.
        self.assertNotIn("feed.prepend(", self.js)

    def test_rendering_uses_the_shared_model(self):
        for call in ("AstraLog.normalize(", "AstraLog.isMeaningful(",
                     "AstraLog.onAppend(", "AstraLog.onScroll(",
                     "AstraLog.matchesRow(", "AstraLog.admit(",
                     "AstraLog.orderHistory("):
            self.assertIn(call, self.js, f"astra.js does not use {call}")

    def test_event_text_is_never_injected_as_html(self):
        # Values are set via textContent (safe); only static markup uses
        # innerHTML. Guards against re-introducing an XSS through an event.
        self.assertIn("pre.textContent = text", self.js)
        for dangerous in ("innerHTML = m.", "innerHTML = ev.data",
                          "innerHTML = event."):
            self.assertNotIn(dangerous, self.js)

    def test_timeline_and_jump_styles_exist(self):
        for rule in (".tl-row", ".tl-detail", ".logs-jump", ".logs-live",
                     ".tl-dot.running"):
            self.assertIn(rule, self.css, f"missing style {rule}")
        # no terminal-only classes left behind
        self.assertNotIn(".term-line", self.css)

    def test_existing_backend_endpoints_are_still_used(self):
        # the panel must keep using the existing history + SSE feed, not a
        # second logging system.
        self.assertIn("/api/events?limit=500", self.js)
        self.assertIn("/api/events/stream", self.js)
        self.assertIn('del("/api/events")', self.js)


if __name__ == "__main__":
    unittest.main()
