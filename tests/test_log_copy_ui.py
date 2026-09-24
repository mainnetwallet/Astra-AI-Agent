"""Activity Log: the expanded Input/Output must be selectable and copyable.

Source-level guards for the three things that made them uncopyable: the row
being `user-select: none`, any click inside the details collapsing the row,
and there being no Copy control.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "astra.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")


class TestLogCopy(unittest.TestCase):
    def test_details_are_selectable_even_though_the_row_is_not(self):
        self.assertRegex(CSS, r"\.tl-detail\s*,\s*\.tl-detail \*\s*\{[^}]*user-select:\s*text")

    def test_clicks_inside_details_do_not_collapse_the_row(self):
        self.assertIn('ev.target.closest(".tl-detail")', JS)
        self.assertIn("sel.isCollapsed", JS)

    def test_keyboard_toggle_only_from_the_row_itself(self):
        self.assertIn("if (ev.target !== row) return;", JS)

    def test_each_block_has_a_copy_button_that_does_not_toggle_the_row(self):
        block = JS[JS.index("function buildBlock"):JS.index("function buildDetails")]
        self.assertIn('className = "tl-copy"', block)
        self.assertIn("ev.stopPropagation()", block)
        self.assertIn("copyText(text)", block)

    def test_copy_falls_back_when_clipboard_api_is_unavailable(self):
        fn = JS[JS.index("function copyText"):JS.index("function buildBlock")]
        self.assertIn("navigator.clipboard", fn)
        self.assertIn('execCommand("copy")', fn)


if __name__ == "__main__":
    unittest.main()
