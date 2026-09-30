"""Security Center: frontend assets + node UI suite; sibling pages untouched."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SecurityCenterJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", os.path.join(ROOT, "tests", "js", "security_center_ui.test.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class Assets(unittest.TestCase):
    def test_wired_and_siblings_still_present(self):
        h = _read("static", "index.html")
        self.assertIn('id="tab-security-center" class="tabview"', h)
        for keep in ("tab-tool-center", "tab-command-center", "tab-system-map", "tab-web3",
                     "/static/js/system_health.js", "/static/js/tool_center.js", "/static/js/web3_center.js"):
            self.assertIn(keep, h)

    def test_css_is_scoped_and_has_no_overflow_hacks(self):
        css = _read("static", "css", "security_center.css")
        self.assertIn("@media (max-width: 640px)", css)
        self.assertNotIn("overflow-x: scroll", css)

    def test_no_security_events_section(self):
        self.assertNotRegex(_read("static", "js", "security_center.js"), r"(?i)security events")


if __name__ == "__main__":
    unittest.main()
