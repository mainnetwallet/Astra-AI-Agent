"""Frontend safety net: run the Activity Log JS unit tests under node.

The timeline mapping + live-scroll state machine live in
static/js/log_model.js as pure, DOM-free functions so they can be verified
without a browser. Skipped when node is not installed (it is a dev-only
convenience, never a runtime dependency)."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = os.path.join(ROOT, "tests", "js", "log_model.test.js")
MODEL_FILE = os.path.join(ROOT, "static", "js", "log_model.js")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestActivityLogModelJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", TEST_FILE],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"node tests failed:\n{proc.stdout}\n{proc.stderr}")

    def test_static_module_is_served_alongside_astra_js(self):
        # index.html must load the model before astra.js consumes it.
        html = open(os.path.join(ROOT, "static", "index.html"),
                    encoding="utf-8").read()
        self.assertIn("/static/js/log_model.js", html)
        self.assertLess(html.index('src="/static/js/log_model.js"'),
                        html.index('src="/static/js/astra.js"'))


if __name__ == "__main__":
    unittest.main()
