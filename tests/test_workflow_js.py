"""Frontend safety net for the Agent Workflow tab: run its node test suites.

`static/js/workflow_model.js` is the pure editor model (draft <-> definition,
canvas graph, validation, run-status mapping) and `static/js/workflow.js` is
the DOM half. The model suite is pure; the UI suite drives the real
workflow.js against a minimal DOM, so both run under node with no browser.
Skipped when node is not installed (it is a dev-only convenience, never a
runtime dependency).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_TEST = os.path.join(ROOT, "tests", "js", "workflow_model.test.js")
UI_TEST = os.path.join(ROOT, "tests", "js", "workflow_ui.test.js")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestWorkflowJS(unittest.TestCase):
    def _run(self, path):
        proc = subprocess.run(["node", "--test", path], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"node tests failed:\n{proc.stdout}\n{proc.stderr}")

    def test_model_suite_passes(self):
        self._run(MODEL_TEST)

    def test_ui_suite_passes(self):
        self._run(UI_TEST)

    def test_index_html_loads_the_model_before_the_ui(self):
        with open(os.path.join(ROOT, "static", "index.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("/static/js/workflow_model.js", html)
        self.assertIn("/static/js/workflow.js", html)
        self.assertLess(html.index('src="/static/js/workflow_model.js"'),
                        html.index('src="/static/js/workflow.js"'))

    def test_the_tab_has_no_inline_handlers(self):
        """script-src 'self' means an inline onclick would be silently dead."""
        with open(os.path.join(ROOT, "static", "js", "workflow.js"),
                  encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("onclick=", "onchange=", "oninput=", "onload="):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
