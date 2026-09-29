"""Tool Center: frontend assets + node UI suite, and separation from System Map.

Frontend-only changes, so this asserts the wiring that a browser would rely
on: the dedicated tabview, the assets, load order, and that Tool Center no
longer points at the System Map.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")


def _read(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ToolCenterJS(unittest.TestCase):
    def test_node_suite_passes(self):
        proc = subprocess.run(
            ["node", "--test", os.path.join(ROOT, "tests", "js", "tool_center_ui.test.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ToolCenterAssets(unittest.TestCase):
    def test_index_has_the_dedicated_tabview(self):
        h = _read("static", "index.html")
        self.assertIn('id="tab-tool-center" class="tabview"', h)

    def test_assets_are_linked_and_load_after_their_dependencies(self):
        h = _read("static", "index.html")
        self.assertIn("/static/css/tool_center.css", h)
        at = lambda f: h.index('src="/static/js/%s"' % f)
        self.assertLess(at("astra.js"), at("tool_center.js"))
        self.assertLess(at("system_map_model.js"), at("tool_center.js"))
        self.assertLess(at("astra_os.js"), at("tool_center.js"))

    def test_registers_into_the_shared_loader_registry(self):
        js = _read("static", "js", "tool_center.js")
        self.assertIn('Astra.loaders["tool-center"]', js)

    def test_tool_center_is_its_own_tab_not_a_system_map_focus(self):
        js = _read("static", "js", "astra_os.js")
        self.assertIn('label: "Tool Center", ic: "🔧", tab: "tool-center"', js)
        self.assertNotIn('label: "Tool Center", ic: "🔧", tab: "system-map"', js)
        # the System Map tool node is untouched — its focus mapping still exists
        self.assertIn('tools: "tools"', js)

    def test_one_data_source_and_no_second_registry(self):
        js = _read("static", "js", "tool_center.js")
        self.assertIn('api("/api/tools")', js)
        self.assertNotIn("new EventSource", js)          # reuses the shared feed
        # no hardcoded tool names — everything comes from the API payload
        for name in ("browser_open", "terminal_exec", "tx_prepare", "read_file"):
            self.assertNotIn(name, js)

    def test_no_generic_tool_execution_is_faked(self):
        js = _read("static", "js", "tool_center.js")
        for bad in ("/api/tools/execute", "runTool", "executeTool"):
            self.assertNotIn(bad, js)
        self.assertNotIn("Run / Test Tool", js)

    def test_no_hardcoded_provider_names(self):
        src = _read("static", "js", "tool_center.js").lower()
        for name in ("openrouter", "cerebras", "sambanova", "groq", "mistral"):
            self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()
