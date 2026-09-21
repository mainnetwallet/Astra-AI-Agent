"""Activity Log backend contract: ToolRegistry publishes one tool.started and
exactly one terminal tool.completed/tool.failed per executed call, with the
tool name + duration (and a redacted input/output summary) so the timeline can
show real tool activity."""
from __future__ import annotations

import unittest

from astra.core.exceptions import PermissionError as ToolPermissionError
from astra.core.permissions import Level
from astra.tools.registry import ToolRegistry
from astra.tools.schemas import Tool


class _Bus:
    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})

    def kinds(self, prefix):
        return [r for r in self.rows if r["kind"].startswith(prefix)]


def _tool(name="echo", fn=None, **kw):
    fn = fn or (lambda args, ctx: {"echo": args.get("msg", "")})
    return Tool(name, fn, input={"msg": {"type": "string", "required": False}},
                **kw)


class TestToolLifecycleEvents(unittest.TestCase):
    def test_success_emits_started_then_completed_with_duration(self):
        bus = _Bus()
        reg = ToolRegistry(events=bus)
        reg.register(_tool())
        out = reg.execute("echo", {"msg": "hi"})
        self.assertTrue(out["ok"])
        self.assertEqual([r["kind"] for r in bus.rows],
                         ["tool.started", "tool.completed"])
        started, done = bus.rows
        self.assertEqual(started["data"]["tool"], "echo")
        self.assertEqual(started["agent"], "tools")
        self.assertIn("input", started["data"])
        self.assertIn("duration_ms", done["data"])
        self.assertIn("output", done["data"])
        self.assertIn("hi", started["data"]["input"])

    def test_failure_emits_started_then_failed_with_error(self):
        bus = _Bus()
        reg = ToolRegistry(events=bus)

        def boom(args, ctx):
            raise RuntimeError("kaboom")

        reg.register(_tool("boom", fn=boom))
        with self.assertRaises(RuntimeError):
            reg.execute("boom", {})
        self.assertEqual([r["kind"] for r in bus.rows],
                         ["tool.started", "tool.failed"])
        failed = bus.rows[1]
        self.assertIn("kaboom", failed["data"]["error"])
        self.assertIn("duration_ms", failed["data"])

    def test_denied_tool_emits_nothing(self):
        bus = _Bus()
        reg = ToolRegistry(policy=_Deny(), events=bus)
        reg.register(_tool("dangerous", risk=Level.SYSTEM_ACTION))
        with self.assertRaises(ToolPermissionError):
            reg.execute("dangerous", {})
        self.assertEqual(bus.rows, [])

    def test_confirmation_gate_emits_nothing(self):
        bus = _Bus()
        reg = ToolRegistry(events=bus)
        reg.register(_tool("guarded", requires_confirmation=True))
        out = reg.execute("guarded", {})
        self.assertEqual(out["decision"], "ask")
        self.assertEqual(bus.rows, [])

    def test_input_and_output_are_redacted(self):
        bus = _Bus()
        reg = ToolRegistry(events=bus)
        reg.register(_tool("leak", fn=lambda args, ctx: {"api_key": "sk-abcdefghij0123456789"}))
        reg.execute("leak", {"token": "ghp_" + "a" * 30})
        blob = str(bus.rows)
        self.assertNotIn("sk-abcdefghij0123456789", blob)
        self.assertNotIn("ghp_aaaaaaaa", blob)

    def test_a_broken_event_bus_never_breaks_the_tool(self):
        class _Bad:
            def emit(self, *a, **k):
                raise RuntimeError("bus down")

        reg = ToolRegistry(events=_Bad())
        reg.register(_tool())
        self.assertTrue(reg.execute("echo", {"msg": "ok"})["ok"])

    def test_start_and_terminal_share_one_correlation_id(self):
        # the Activity Log resolves a start/terminal pair by `op`, never by
        # title, so both events of one call must carry the same id.
        bus = _Bus()
        reg = ToolRegistry(events=bus)
        reg.register(_tool())
        reg.execute("echo", {"msg": "hi"})
        started, done = bus.rows
        self.assertTrue(started["data"].get("op"))
        self.assertEqual(started["data"]["op"], done["data"]["op"])
        self.assertTrue(done["data"]["terminal"])
        self.assertNotIn("terminal", started["data"])

    def test_concurrent_calls_get_distinct_correlation_ids(self):
        bus = _Bus()
        reg = ToolRegistry(events=bus)
        reg.register(_tool())
        reg.execute("echo", {"msg": "one"})
        reg.execute("echo", {"msg": "two"})
        ops = [r["data"]["op"] for r in bus.rows]
        self.assertEqual(len(ops), 4)
        self.assertEqual(len(set(ops)), 2, "each call must have its own op")


class _Deny:
    """Policy stub that denies everything (execute() only calls .decision)."""
    def decision(self, risk, requires_confirmation, tool_name="",
                 confirmation_delegate=""):
        return "deny"


if __name__ == "__main__":
    unittest.main()
