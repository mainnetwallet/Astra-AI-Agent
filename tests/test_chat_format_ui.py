"""Chat code blocks: wiring guards (behaviour is in tests/js/chat_format.test.js)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "astra.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")


class TestChatFormatWiring(unittest.TestCase):
    def test_module_is_loaded_before_the_core_bundle(self):
        self.assertIn("/static/js/chat_format.js", HTML)
        self.assertLess(HTML.index('src="/static/js/chat_format.js"'),
                        HTML.index('src="/static/js/astra.js"'))

    def test_chat_bubble_uses_the_safe_formatter_not_raw_innerhtml_of_text(self):
        bubble = JS[JS.index("function chatBubble"):JS.index("/* --------------------- assistant execution status")]
        self.assertIn("AstraChatFormat.toHtml(text)", bubble)
        # the fallback path must escape too
        self.assertIn("esc(text)", bubble)

    def test_code_copy_button_is_wired_by_delegation(self):
        self.assertIn('.closest(".code-copy")', JS)
        self.assertIn('.closest(".code-block")', JS)

    def test_code_block_styles_exist(self):
        for sel in (".code-block", ".code-head", ".code-copy", ".code-pre"):
            self.assertIn(sel, CSS)


if __name__ == "__main__":
    unittest.main()
