"""Terminal tools on the SHARED ToolRegistry, with the real policy gate.

The point: both the Gateway and every Provider reach the terminal the same
way — `registry.execute("terminal_exec", ...)` — never a private path.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.core.context import ToolContext
from astra.core.exceptions import PermissionError, ValidationError
from astra.core.permissions import Level, Policy
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.registry import ToolRegistry
from tests.helpers import requires_posix_terminal


def _reg(granted=None, **kw):
    manager = kw.pop("manager", None) or TerminalManager()
    policy = Policy(granted=granted or ["read", "low_risk_write",
                                        "browser_action", "system_action"])
    reg = ToolRegistry(policy=policy)
    register_terminal_tools(reg, manager)
    return reg, manager


def _ctx(manager, session_id="conv-1"):
    return ToolContext(terminal=manager, terminal_session_id=session_id)


class RegistrationTests(unittest.TestCase):
    def test_tools_registered_in_terminal_category(self):
        reg, manager = _reg()
        names = {t["name"] for t in reg.list("terminal")}
        self.assertEqual(names, {
            "terminal_exec", "terminal_start", "terminal_status",
            "terminal_stop", "terminal_kill", "terminal_history",
            "terminal_output_read", "terminal_history_read",
            "terminal_sessions", "terminal_close"})
        manager.close_all()

    def test_terminal_tools_are_system_action_risk(self):
        reg, manager = _reg()
        self.assertEqual(reg.get("terminal_exec").risk, Level.SYSTEM_ACTION)
        manager.close_all()


class PolicyGateTests(unittest.TestCase):
    def test_denied_when_system_action_not_granted(self):
        reg, manager = _reg(granted=["read", "low_risk_write"])
        with self.assertRaises(PermissionError):
            reg.execute("terminal_exec", {"command": "echo no"},
                        ctx=_ctx(manager))
        manager.close_all()

    def test_allowed_when_system_action_granted(self):
        reg, manager = _reg()
        out = reg.execute("terminal_exec", {"command": "echo yes"},
                          ctx=_ctx(manager))
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["status"], "completed")
        manager.close_all()


class ExecutionTests(unittest.TestCase):
    @requires_posix_terminal
    def test_exec_returns_structured_result(self):
        reg, manager = _reg()
        out = reg.execute("terminal_exec", {"command": "printf 'abc'"},
                          ctx=_ctx(manager))
        r = out["result"]
        self.assertEqual(r["stdout"], "abc")
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["session_id"], "conv-1")
        manager.close_all()

    def test_context_session_id_is_used(self):
        reg, manager = _reg()
        reg.execute("terminal_exec", {"command": "echo a"}, ctx=_ctx(manager, "A"))
        a = manager.get("A", create=False)
        self.assertIsNotNone(a)
        self.assertIsNone(manager.get("B", create=False))
        manager.close_all()

    def test_explicit_session_id_overrides_context(self):
        reg, manager = _reg()
        out = reg.execute("terminal_exec",
                          {"command": "echo x", "session_id": "explicit"},
                          ctx=_ctx(manager, "conv-1"))
        self.assertEqual(out["result"]["session_id"], "explicit")
        manager.close_all()

    def test_command_required(self):
        reg, manager = _reg()
        with self.assertRaises(ValidationError):
            reg.execute("terminal_exec", {"command": ""}, ctx=_ctx(manager))
        manager.close_all()

    def test_unknown_session_arg_creates_isolated_session(self):
        reg, manager = _reg()
        reg.execute("terminal_exec", {"command": "cd /tmp"}, ctx=_ctx(manager, "one"))
        reg.execute("terminal_exec", {"command": "cd /tmp"}, ctx=_ctx(manager, "two"))
        self.assertEqual(set(manager.session_ids()) & {"one", "two"},
                         {"one", "two"})
        manager.close_all()


class HistoryAndProcessToolsTests(unittest.TestCase):
    def test_history_tool(self):
        reg, manager = _reg()
        reg.execute("terminal_exec", {"command": "echo h1"}, ctx=_ctx(manager))
        out = reg.execute("terminal_history", {}, ctx=_ctx(manager))
        cmds = [h["command"] for h in out["result"]["commands"]]
        self.assertIn("echo h1", cmds)
        manager.close_all()

    def test_start_status_stop_via_registry(self):
        reg, manager = _reg()
        started = reg.execute("terminal_start", {"command": "sleep 30"},
                              ctx=_ctx(manager))["result"]
        self.assertEqual(started["status"], "running")
        st = reg.execute("terminal_status",
                         {"process_id": started["process_id"]},
                         ctx=_ctx(manager))["result"]
        self.assertEqual(st["status"], "running")
        stopped = reg.execute("terminal_stop",
                              {"process_id": started["process_id"]},
                              ctx=_ctx(manager))["result"]
        self.assertEqual(stopped["status"], "stopped")
        manager.close_all()

    def test_sessions_tool_lists_live_sessions(self):
        reg, manager = _reg()
        reg.execute("terminal_exec", {"command": "echo x"}, ctx=_ctx(manager, "s1"))
        out = reg.execute("terminal_sessions", {}, ctx=_ctx(manager))
        ids = {s["session_id"] for s in out["result"]["sessions"]}
        self.assertIn("s1", ids)
        manager.close_all()

    def test_close_tool(self):
        reg, manager = _reg()
        reg.execute("terminal_exec", {"command": "echo x"}, ctx=_ctx(manager, "s1"))
        out = reg.execute("terminal_close", {}, ctx=_ctx(manager, "s1"))
        self.assertTrue(out["result"]["closed"])
        self.assertIsNone(manager.get("s1", create=False))

    def test_bound_manager_covers_context_without_terminal(self):
        # The registry-bound manager is the fallback, so a ToolContext that
        # does not carry one still works (and uses the default session).
        reg, manager = _reg()
        out = reg.execute("terminal_exec", {"command": "echo x"},
                          ctx=ToolContext())
        self.assertTrue(out["ok"])
        manager.close_all()


if __name__ == "__main__":
    unittest.main()
