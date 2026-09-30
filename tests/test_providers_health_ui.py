"""Unified "Providers Health Test" page: frontend assets + node UI suite +
no-fake-data / no-secret guards.

Frontend-first redesign, so this asserts the wiring a browser relies on: the
one page owns BOTH the direct AI providers and the Astra AI Gateway from the
existing /api/providers payload, its chrome is linked in load order, the old
Router navigation page stays gone, nothing the backend cannot report is
fabricated, and a credential value is never read.
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
    """The JS with comments stripped (file banners are prose, not behaviour)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ProvidersHealthJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", os.path.join(ROOT, "tests", "js", "providers_health_ui.test.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ProvidersHealthAssets(unittest.TestCase):
    def test_assets_are_linked_and_load_after_their_dependencies(self):
        h = _read("static", "index.html")
        self.assertIn("/static/css/providers_health.css", h)
        at = lambda f: h.index('src="/static/js/%s"' % f)
        self.assertLess(at("astra.js"), at("providers_health.js"))
        self.assertLess(at("astra_os.js"), at("providers_health.js"))

    def test_one_page_owns_both_providers_and_the_gateway(self):
        h = _read("static", "index.html")
        # the existing core tabview id is untouched (routes/links keep working)
        self.assertIn('id="tab-providers" class="tabview"', h)
        # both tables live inside that one page
        tab = h[h.index('id="tab-providers"'):h.index('id="tab-settings"')
                if 'id="tab-settings"' in h else len(h)]
        self.assertIn('id="providers-list"', tab)
        self.assertIn('id="gateway-card"', tab)
        # the existing control ids survive the redesign
        for keep in ("btn-providers-test-all", "btn-providers-toggle-models",
                     "btn-health-key-selector", "health-key-selector"):
            self.assertIn('id="%s"' % keep, tab, "lost existing control: " + keep)

    def test_the_old_markup_is_replaced(self):
        h = _read("static", "index.html")
        js = _read("static", "js", "astra.js")
        self.assertNotIn("AI Providers health", h)
        self.assertNotIn("\U0001f648 Hide models", h)
        self.assertNotIn("\U0001f310 Astra AI Gateway", h)
        # the strip of legacy per-table toggles is gone from the module
        self.assertNotIn("AI Providers health", js)

    def test_no_separate_router_page(self):
        h = _read("static", "index.html")
        os_js = _read("static", "js", "astra_os.js")
        self.assertNotIn('id="tab-router"', h)
        self.assertNotIn('"/router": "router"', os_js)
        self.assertNotIn("Astra.loaders[\"router\"]", os_js)
        self.assertNotIn('{"tab": "router"', _read("astra", "web.py"))

    def test_one_data_source_and_one_test_engine(self):
        js = _read("static", "js", "providers_health.js")
        code = _code(js)
        # it never fetches: every number comes from astra.js state (the single
        # /api/providers loader), so there is no second feed to keep in sync
        self.assertNotIn("/api/", code)
        self.assertNotIn("fetch(", code)
        self.assertIn("window.AstraProviders", js)
        # no second SSE subscription and no timer of its own
        self.assertNotIn("new EventSource", js)
        self.assertIsNone(re.search(r"(^|[^.\w])setInterval\s*\(", js),
                          "the page must not start its own interval")
        # wraps the EXISTING loader so a refresh repaints the chrome
        self.assertIn("loaders.providers = async function", code)
        # Test All is forwarded to the one existing operation
        self.assertIn("api().testAll", code)

    def test_no_fake_metrics_or_reference_numbers(self):
        blob = (_read("static", "js", "providers_health.js")
                + _read("static", "css", "providers_health.css")
                + _read("static", "index.html"))
        for fake in ("98%", "72 / 74", "74 models available", "19 API keys",
                     "10 providers", "8 gateway connections", "12:02 PM"):
            self.assertNotIn(fake, blob, "a reference-image value leaked: " + fake)
        # the chrome is generic: it must not hardcode any provider/catalogue
        low = _code(_read("static", "js", "providers_health.js")).lower()
        for name in ("gemini", "openai", "anthropic", "cloudflare", "openrouter",
                     "huggingface", "mistral", "groq"):
            self.assertNotIn(name, low, "hardcoded provider name: " + name)
        # no fabricated per-model execution endpoints
        for bad in ("/api/models/execute", "runModel", "fakeTest"):
            self.assertNotIn(bad, blob)

    def test_a_secret_is_never_read_or_rendered(self):
        code = _code(_read("static", "js", "providers_health.js"))
        bad = re.compile(
            r"\.(api_?key|secret|token|password|passphrase|private_?key|authorization)\b",
            re.I)
        self.assertIsNone(bad.search(code),
                          "the page must never read a credential value field")
        for leak in ("Bearer ", "sk-", "AIza"):
            self.assertNotIn(leak, code)


if __name__ == "__main__":
    unittest.main()
