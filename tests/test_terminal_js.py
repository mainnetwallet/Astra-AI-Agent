"""Frontend safety net for the Astra Agent Terminal.

`static/js/terminal.js` is the terminal half of the runtime feature (the
pane, tabs, keystrokes, SSE stream, resize, workspace sidebar). Its node
suite pins the wiring that makes it a real terminal over the Agent Runtime
rather than a log view. Runnable standalone with
`node --test tests/js/terminal_assets.test.js`; skipped when node is absent
(node is a dev convenience, never a runtime dependency).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMINAL_TEST = os.path.join(ROOT, "tests", "js",
                            "terminal_assets.test.js")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestTerminalJS(unittest.TestCase):
    def test_terminal_assets_suite_passes(self):
        proc = subprocess.run(["node", "--test", TERMINAL_TEST], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"node tests failed:\n{proc.stdout}\n{proc.stderr}")
