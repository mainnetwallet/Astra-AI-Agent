"""Frontend safety net: run the Agent Workflow JS unit tests under node.

The node-graph layout, the WorkflowEngine semantics and the live-event
reduction live in static/js/workflow_model.js as pure, DOM-free functions so
they can be verified without a browser. Skipped when node is not installed
(dev-only convenience, never a runtime dependency)."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = os.path.join(ROOT, "tests", "js", "workflow_model.test.js")
MODEL_FILE = os.path.join(ROOT, "static", "js", "workflow_model.js")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestWorkflowModelJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", TEST_FILE],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"node tests failed:\n{proc.stdout}\n{proc.stderr}")

    def test_static_module_is_served_before_astra_js(self):
        with open(os.path.join(ROOT, "static", "index.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("/static/js/workflow_model.js", html)
        self.assertLess(html.index('src="/static/js/workflow_model.js"'),
                        html.index('src="/static/js/astra.js"'))


if __name__ == "__main__":
    unittest.main()
