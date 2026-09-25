"""HOST-TERMINAL HARD BLOCK — the Agent can never run Agent work on the host.

The Agent Runtime is the only execution surface for Agent work
(`runtime_command` inside the isolated runtime). The legacy HOST terminal
tools (`terminal_exec` family) still exist on the ONE ToolRegistry for
trusted Astra internals/diagnostics and the web operator surface, but they
are structurally unavailable to Agent/Provider/workflow execution:

  * `Tool` carries `agent_forbidden`; the host terminal tools set it.
  * `AgentToolLoop` marks its `ToolContext` with `agent_execution=True`.
  * `ToolRegistry.execute` — the single path every tool call takes — returns
    a structured "blocked" result and executes NOTHING when a forbidden tool
    is called from that context.
  * `build_tool_catalog` never advertises a forbidden tool to a model, so
    the model is never even told the host shell exists.

This is enforcement, not a system prompt. These tests prove it end to end:
a scripted model that explicitly asks for `terminal_exec` gets a refusal and
**no host process is spawned**.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.agent_tool_loop import AgentToolLoop, build_tool_catalog
from astra.core.context import ToolContext
from astra.core.permissions import Level, Policy
from astra.runtime.tools import register_runtime_tools
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry
from tests.helpers import (LocalRuntimeStub, ScriptedBrain,
                           requires_posix_host)

HOST_TERMINAL_TOOLS = ("terminal_exec", "terminal_start", "terminal_status",
                       "terminal_stop", "terminal_kill", "terminal_history",
                       "terminal_output_read", "terminal_history_read",
                       "terminal_sessions", "terminal_close")


class Bus:
    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})

    def kinds(self):
        return [r["kind"] for r in self.rows]


def _stack(*, events=None):
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy, events=events)
    register_builtins(reg)
    host = TerminalManager(events=events)
    register_terminal_tools(reg, host)
    runtime = LocalRuntimeStub(events=events)
    register_runtime_tools(reg, runtime)
    return reg, host, runtime


def _tool_call(command, tool="terminal_exec"):
    return json.dumps({"action": "tool", "tool": tool,
                       "args": {"command": command}, "thought": "run it"})


def _final(answer):
    return json.dumps({"action": "final", "answer": answer})


class AgentExecutionIsMarkedTests(unittest.TestCase):
    def test_agent_tool_loop_marks_its_context_as_agent_execution(self):
        """The marker is set by the loop itself — nothing the model writes
        can clear it."""
        seen = {}

        class _Spy(ScriptedBrain):
            pass

        reg, host, runtime = _stack()
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime)

        captured = {}

        from astra.core.context import ToolContext as _TC
        original = _TC.__init__

        def spy(self, *a, **kw):
            original(self, *a, **kw)
            captured["ctx"] = self

        _TC.__init__ = spy
        try:
            loop.run("x", ScriptedBrain([_final("hi")]), system_prompt="",
                     session_id="conv-7", scope="7")
        finally:
            _TC.__init__ = original
        ctx = captured["ctx"]
        self.assertTrue(ctx.agent_execution)
        # the runtime (not the host terminal) is the execution surface ...
        self.assertIs(ctx.runtime, runtime)
        # ... and it is the SAME conversation session the terminal opens
        self.assertEqual(ctx.runtime_session_id, "conv-7")
        host.close_all()
        runtime.close_all()


class RegistryHardBlockTests(unittest.TestCase):
    def test_every_host_terminal_tool_is_marked_agent_forbidden(self):
        reg, host, runtime = _stack()
        for name in HOST_TERMINAL_TOOLS:
            tool = reg.get(name)
            self.assertIsNotNone(tool, name)
            self.assertTrue(tool.agent_forbidden, name)
        # the runtime tools (the legitimate surface) are NOT forbidden
        for name in ("runtime_command", "runtime_package_install",
                     "runtime_file_write"):
            self.assertFalse(reg.get(name).agent_forbidden, name)
        host.close_all()
        runtime.close_all()

    def test_agent_context_is_refused_and_nothing_executes(self):
        bus = Bus()
        reg, host, runtime = _stack(events=bus)
        marker = os.path.join(tempfile.mkdtemp(), "host-must-not-exist")
        try:
            for name, args in (
                    ("terminal_exec", {"command": f"touch {marker}"}),
                    ("terminal_start", {"command": f"touch {marker}"}),
            ):
                out = reg.execute(name, args,
                                  ctx=ToolContext(agent_execution=True,
                                                  terminal=host))
                self.assertFalse(out["ok"])
                self.assertEqual(out["decision"], "blocked")
                self.assertIn("runtime", out["reason"])
            # absolutely nothing ran on the host
            self.assertFalse(os.path.exists(marker))
            # and the refusal is recorded for the Activity Log
            self.assertIn("tool.blocked", bus.kinds())
            self.assertNotIn("terminal.started", bus.kinds())
        finally:
            if os.path.exists(marker):
                os.remove(marker)
        host.close_all()
        runtime.close_all()

    def test_non_agent_context_can_still_use_the_host_terminal(self):
        """Trusted Astra internals / the operator surface keep the tool —
        only Agent execution loses it."""
        reg, host, runtime = _stack()
        out = reg.execute("terminal_exec", {"command": "echo operator-ok"},
                          ctx=ToolContext(terminal=host))
        self.assertTrue(out["ok"])
        self.assertIn("operator-ok", out["result"]["stdout"])
        host.close_all()
        runtime.close_all()


class AgentLoopCannotReachTheHostTests(unittest.TestCase):
    def test_scripted_model_asking_for_terminal_exec_gets_a_refusal(self):
        bus = Bus()
        reg, host, runtime = _stack(events=bus)
        marker = os.path.join(tempfile.mkdtemp(), "loop-host-must-not-exist")
        try:
            brain = ScriptedBrain([
                _tool_call(f"touch {marker}", tool="terminal_exec"),
                _final("could not run it on the host"),
            ])
            loop = AgentToolLoop(reg, terminal=host, runtime=runtime,
                                 events=bus)
            res = loop.run("touch a host file", brain, system_prompt="",
                           session_id="s", scope="s")
            step = res.steps[0]
            self.assertFalse(step.ok)
            self.assertEqual(step.status, "blocked")
            self.assertIn("host-only", step.error)
            self.assertFalse(os.path.exists(marker))
            # the refusal came back to the model as usable feedback
            self.assertIn("blocked", str(brain.calls[1][-1]["content"]).lower())
        finally:
            if os.path.exists(marker):
                os.remove(marker)
        host.close_all()
        runtime.close_all()

    @requires_posix_host
    def test_the_legit_runtime_path_still_works_in_the_loop(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([
            _tool_call("pwd", tool="runtime_command"),
            _final("ran inside the runtime"),
        ])
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "pwd", brain, system_prompt="", session_id="conv-1", scope="1")
        self.assertTrue(res.steps[0].ok)
        self.assertEqual(res.steps[0].status, "completed")
        # executed in the runtime's workspace, on the conversation session
        self.assertEqual(runtime.history("conv-1")[0]["command"], "pwd")
        self.assertTrue(res.steps[0].result["stdout"].strip())
        host.close_all()
        runtime.close_all()


class CatalogNeverAdvertisesTheHostTests(unittest.TestCase):
    def test_catalog_hides_every_host_terminal_tool_and_shows_the_runtime(self):
        reg, host, runtime = _stack()
        catalog = build_tool_catalog(reg)
        for name in HOST_TERMINAL_TOOLS:
            self.assertNotIn(name, catalog)
        self.assertIn("runtime_command", catalog)
        self.assertIn("runtime_package_install", catalog)
        host.close_all()
        runtime.close_all()

    def test_catalog_entries_carry_the_flag(self):
        reg, host, runtime = _stack()
        by_name = {t["name"]: t for t in reg.list()}
        self.assertTrue(by_name["terminal_exec"]["agent_forbidden"])
        self.assertFalse(by_name["runtime_command"]["agent_forbidden"])
        host.close_all()
        runtime.close_all()


class WorkflowExecutionIsAgentExecutionTests(unittest.TestCase):
    def test_the_bootstrap_workflow_context_cannot_use_the_host_terminal(self):
        from astra.bootstrap import build
        from astra.store import Store
        stack = build(store=Store(":memory:"))
        ctx = stack["workflows"].context
        self.assertTrue(ctx.agent_execution)
        out = stack["registry"].execute(
            "terminal_exec", {"command": "echo workflow-host-pwned"}, ctx=ctx)
        self.assertFalse(out["ok"])
        self.assertEqual(out["decision"], "blocked")

    def test_the_bootstrap_chat_pipeline_carries_the_runtime(self):
        from astra.bootstrap import build
        from astra.store import Store
        stack = build(store=Store(":memory:"))
        self.assertIs(stack["chat_pipeline"].runtime, stack["runtime"])


if __name__ == "__main__":
    unittest.main()
