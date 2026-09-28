"""System Map / Command Center: frontend model tests (node) + serving contract."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")


def _html() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SystemMapModelJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", os.path.join(ROOT, "tests", "js", "system_map_model.test.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class SystemMapAssets(unittest.TestCase):
    def test_scripts_load_in_order(self):
        h = _html()
        self.assertLess(h.index("/static/js/astra.js"), h.index("/static/js/system_map_model.js"))
        self.assertLess(h.index("/static/js/system_map_model.js"), h.index("/static/js/astra_os.js"))
        self.assertIn("/static/css/astra_os.css", h)

    def test_tabviews_and_shell_present(self):
        h = _html()
        for needle in ('id="tab-command-center"', 'id="tab-system-map"',
                       'id="os-sidebar"', 'id="os-drawer"'):
            self.assertIn(needle, h)

    def test_existing_tabs_still_present(self):
        h = _html()
        for tab in ("tab-assistant", "tab-terminal", "tab-workflow", "tab-logs", "tab-web3"):
            self.assertIn(f'id="{tab}"', h)

    def test_sse_is_shared_not_duplicated(self):
        js = open(os.path.join(ROOT, "static", "js", "astra_os.js"), encoding="utf-8").read()
        self.assertNotIn("new EventSource", js)      # reuses astra.js ensureEventStream
        self.assertIn("astra:event", js)

    def test_workflow_sse_is_shared_not_duplicated(self):
        # workflow.js previously opened its own EventSource("/api/events/stream")
        # alongside astra.js's shared one — two persistent connections to the
        # same endpoint, eating into the browser's per-origin connection limit
        # and slowing down the rest of the app. It must reuse the one shared
        # feed via ensureEventStream()/the "astra:event" DOM event instead.
        js = open(os.path.join(ROOT, "static", "js", "workflow.js"), encoding="utf-8").read()
        self.assertNotIn("new EventSource", js)
        self.assertIn("astra:event", js)

    def test_no_hardcoded_provider_names_in_ui_code(self):
        for f in ("astra_os.js", "system_map_model.js"):
            src = open(os.path.join(ROOT, "static", "js", f), encoding="utf-8").read().lower()
            for name in ("openrouter", "cerebras", "sambanova", "groq", "mistral"):
                self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()
