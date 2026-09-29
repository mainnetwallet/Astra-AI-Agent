"""System Health page: frontend assets + node UI suite + no-fake-data guards.

Frontend-only change, so this asserts the wiring a browser relies on: the
stylesheet/script are linked in load order, the page owns the existing
`command-center` tabview (so /command-center keeps working), the old Command
Center "LIVE OPERATIONS"/"AI ROUTING" markup is gone, and nothing the backend
cannot report is fabricated.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")


def _read(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _code(js: str) -> str:
    """The JS with comments stripped (the header follows the emoji convention)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


EMOJI = re.compile("[\U0001F300-\U0001FAFF\uFE0F]")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SystemHealthJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", os.path.join(ROOT, "tests", "js", "system_health_ui.test.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class SystemHealthAssets(unittest.TestCase):
    def test_assets_are_linked_and_load_after_their_dependencies(self):
        h = _read("static", "index.html")
        self.assertIn("/static/css/system_health.css", h)
        at = lambda f: h.index('src="/static/js/%s"' % f)
        self.assertLess(at("system_map_model.js"), at("system_health.js"))
        self.assertLess(at("astra_os.js"), at("system_health.js"))

    def test_owns_the_command_center_tabview_so_the_route_still_works(self):
        h = _read("static", "index.html")
        self.assertIn('id="tab-command-center" class="tabview"', h)
        os_js = _read("static", "js", "astra_os.js")
        self.assertIn('"/command-center": "command-center"', os_js)
        self.assertIn('Astra.loaders["command-center"]', os_js)

    def test_the_old_command_center_markup_is_gone(self):
        os_js = _read("static", "js", "astra_os.js")
        for gone in ("LIVE OPERATIONS", "AI ROUTING", "ccShell"):
            self.assertNotIn(gone, os_js)

    def test_one_data_source_reuses_the_shared_poll_and_feed(self):
        js = _read("static", "js", "system_health.js")
        self.assertNotIn("new EventSource", js)          # reuses the shared feed
        # no timer of its own — it drives astra_os.js's ONE poll via the hooks
        self.assertIsNone(re.search(r"(^|[^.\w])setInterval\s*\(", js),
                          "System Health must not start its own interval")
        self.assertIn("OS.hooks.setAuto", js)
        self.assertIn("OS.hooks.setInterval", js)
        # the model registry is read from the real endpoint, nothing else
        self.assertIn('api("/api/models")', js)

    def test_no_fake_metrics_or_invented_values(self):
        js = _read("static", "js", "system_health.js")
        code = _code(js)
        # reference-image numbers must never be hardcoded
        for fake in ("99.98", "342 ms", "0.12%", "99.2%"):
            self.assertNotIn(fake, code, "a reference-image value leaked into the code")
        # no hardcoded provider names — the catalogue comes from /api/providers
        low = code.lower()
        for name in ("gemini", "openai", "anthropic", "cloudflare", "openrouter",
                     "huggingface", "mistral", "groq"):
            self.assertNotIn(name, low, "hardcoded provider name: " + name)
        # no fabricated per-model endpoints / execution
        for bad in ("/api/models/execute", "/api/tools/execute", "runModel", "testModel"):
            self.assertNotIn(bad, js)

    def test_secrets_are_never_read(self):
        code = _code(_read("static", "js", "system_health.js"))
        # it may name the backend's secret-free key metadata (key_id / label /
        # healthy), but it must never touch a credential VALUE field
        bad = re.compile(
            r"\.(api_?key|secret|token|password|passphrase|private_?key|authorization)\b",
            re.I)
        self.assertIsNone(bad.search(code),
                          "System Health must never read a credential value field")
        for leak in ("Bearer ", "sk-", "AIza"):
            self.assertNotIn(leak, code)

    def test_icons_use_the_astra_glyph_system_not_emoji(self):
        js = _read("static", "js", "system_health.js")
        self.assertIn('"ic-service"', js)
        self.assertIn("sh-ic ", js)
        self.assertIsNone(EMOJI.search(_code(js)),
                          "System Health must not use emoji icons")
        css = _read("static", "css", "system_health.css")
        self.assertIn("--ic:", css, "the monochrome glyph convention must be used")
        self.assertIsNone(EMOJI.search(css), "System Health CSS must not use emoji icons")


if __name__ == "__main__":
    unittest.main()