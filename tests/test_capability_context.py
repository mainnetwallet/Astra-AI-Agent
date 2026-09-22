"""Regression tests for `astra.ai.capability_context.build_capability_context`
— the authoritative, human-facing runtime capability summary derived live
from the ToolRegistry (see astra/ai/capability_context.py for the "why").

Covers the requirements from the capability-question fix brief:
  - Terminal tools registered -> mentioned
  - File tools registered -> mentioned
  - Browser/web tools registered -> mentioned
  - A category with nothing registered is never claimed
  - Empty/missing registry -> explicit "no external tools" statement
  - The summary never leaks the internal tool-call protocol or raw
    tool names/schemas
"""
import unittest

from astra.ai.capability_context import (NO_TOOLS_MESSAGE,
                                         build_capability_context)
from astra.browser import register_browser_tools
from astra.core.permissions import Policy
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry


def _registry(*, terminal=False, builtins=False, browser=False):
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy)
    manager = None
    if builtins:
        register_builtins(reg)
    if terminal:
        manager = TerminalManager()
        register_terminal_tools(reg, manager)
    if browser:
        register_browser_tools(reg)
    return reg, manager


class EmptyOrMissingRegistryTests(unittest.TestCase):
    def test_none_registry_says_no_tools_available(self):
        self.assertEqual(build_capability_context(None), NO_TOOLS_MESSAGE)
        self.assertIn("no external tools are currently available",
                      NO_TOOLS_MESSAGE)

    def test_empty_registry_says_no_tools_available(self):
        reg, _ = _registry()
        self.assertEqual(build_capability_context(reg), NO_TOOLS_MESSAGE)

    def test_registry_that_raises_fails_safe_to_no_tools(self):
        class Boom:
            def list(self):
                raise RuntimeError("boom")
        self.assertEqual(build_capability_context(Boom()), NO_TOOLS_MESSAGE)


class TerminalAvailableTests(unittest.TestCase):
    def test_terminal_category_is_mentioned(self):
        reg, manager = _registry(terminal=True)
        try:
            ctx = build_capability_context(reg)
            self.assertIn("terminal", ctx.lower())
        finally:
            if manager:
                manager.close_all()

    def test_terminal_only_does_not_claim_other_categories(self):
        reg, manager = _registry(terminal=True)
        try:
            ctx = build_capability_context(reg)
            self.assertNotIn("web browsing", ctx)
            self.assertNotIn("web3", ctx.lower())
        finally:
            if manager:
                manager.close_all()


class FileToolsAvailableTests(unittest.TestCase):
    def test_files_category_is_mentioned(self):
        reg, _ = _registry(builtins=True)
        ctx = build_capability_context(reg)
        self.assertIn("file access", ctx)

    def test_disabled_terminal_is_not_claimed_when_only_builtins_registered(self):
        reg, _ = _registry(builtins=True)
        ctx = build_capability_context(reg)
        self.assertNotIn("terminal", ctx.lower())


class BrowserToolsAvailableTests(unittest.TestCase):
    def test_browser_category_is_mentioned(self):
        reg, _ = _registry(browser=True)
        ctx = build_capability_context(reg)
        self.assertIn("web browsing", ctx)

    def test_unregistered_web3_is_not_claimed(self):
        reg, _ = _registry(browser=True)
        ctx = build_capability_context(reg)
        self.assertNotIn("web3", ctx.lower())


class NoProtocolLeakTests(unittest.TestCase):
    """The summary must stay clean: category-level, human language only —
    never the internal JSON tool-call protocol or raw tool names."""

    def test_no_raw_tool_names_leak(self):
        reg, manager = _registry(terminal=True, builtins=True, browser=True)
        try:
            ctx = build_capability_context(reg)
            for leaked in ("terminal_exec", "read_file", "write_file",
                          "terminal_start", "search_web"):
                self.assertNotIn(leaked, ctx)
        finally:
            if manager:
                manager.close_all()

    def test_no_json_tool_protocol_leaks(self):
        reg, manager = _registry(terminal=True, builtins=True, browser=True)
        try:
            ctx = build_capability_context(reg)
            for marker in ('"action"', '"tool"', '"args"', "{catalog}"):
                self.assertNotIn(marker, ctx)
        finally:
            if manager:
                manager.close_all()

    def test_only_actually_registered_categories_appear(self):
        reg, manager = _registry(terminal=True, builtins=True)
        try:
            ctx = build_capability_context(reg)
            self.assertIn("terminal", ctx.lower())
            self.assertIn("file access", ctx)
            # browser was never registered on this registry
            self.assertNotIn("web browsing", ctx)
        finally:
            if manager:
                manager.close_all()


if __name__ == "__main__":
    unittest.main()
