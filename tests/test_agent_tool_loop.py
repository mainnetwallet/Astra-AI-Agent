"""The multi-step AI tool loop: the AI decides, tools execute, results come
back to the SAME execution, and the loop continues until the AI answers.

The loop, the ToolRegistry and the Terminal are the real implementations;
only the model reply is scripted (there is no live model offline).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.agent_tool_loop import (AgentToolLoop, GatewayToolCaller,
                                      ProviderToolCaller, build_tool_catalog)
from astra.ai.execution_history import AgentExecutionHistory
from astra.ai.router import RoutingResult
from astra.core.permissions import Policy
from astra.runtime.tools import register_runtime_tools
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry
from tests.helpers import LocalRuntimeStub, ScriptedBrain


def _tool(command):
    # The Agent's shell work goes through the isolated Agent Runtime
    # (`runtime_command`), never the host terminal. The legacy HOST
    # `terminal_exec` is structurally blocked for Agent execution; see
    # tests/test_host_terminal_block.py.
    return json.dumps({"action": "tool", "tool": "runtime_command",
                       "args": {"command": command}, "thought": "do it"})


def _final(answer):
    return json.dumps({"action": "final", "answer": answer})


class Bus:
    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})
        return self.rows[-1]

    def kinds(self):
        return [r["kind"] for r in self.rows]


def _ctx_helper(terminal, session_id="s"):
    from astra.core.context import ToolContext
    return ToolContext(terminal=terminal, terminal_session_id=session_id)


def _stack(**kw):
    """The real ToolRegistry with the real builtins, the real HOST terminal
    tools and the real RUNTIME tools — only the runtime's process backend is
    a local stub (see `helpers.LocalRuntimeStub`). Returns
    `(registry, host_terminal, runtime_stub)`."""
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy)
    register_builtins(reg)
    host = kw.pop("host", None) or TerminalManager()
    register_terminal_tools(reg, host)
    runtime = kw.pop("runtime", None) or LocalRuntimeStub()
    register_runtime_tools(reg, runtime)
    return reg, host, runtime


class LoopExecutionTests(unittest.TestCase):
    def test_multi_step_loop_executes_and_continues(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_tool("echo one"), _tool("echo two"),
                               _final("both ran")])
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime)
        res = loop.run("do two things", brain, system_prompt="You are Astra.",
                       session_id="conv-1", scope="1")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "both ran")
        self.assertEqual(res.tool_calls, 2)
        self.assertEqual([s.status for s in res.steps],
                         ["completed", "completed"])
        # the results really came back into the same conversation
        self.assertIn("Tool result", brain.calls[1][-1]["content"])
        self.assertIn("echo one", brain.calls[1][-1]["content"])
        host.close_all()
        runtime.close_all()

    def test_failed_command_is_recoverable(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_tool("exit 7"), _tool("echo fixed"),
                               _final("recovered")])
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime)
        res = loop.run("fix it", brain, system_prompt="", session_id="s",
                       scope="s")
        self.assertEqual(res.steps[0].status, "failed")
        self.assertEqual(res.steps[0].result["exit_code"], 7)
        self.assertEqual(res.steps[1].status, "completed")
        self.assertEqual(res.text, "recovered")
        host.close_all()
        runtime.close_all()

    def test_cwd_persists_across_loop_steps(self):
        base = tempfile.mkdtemp()
        sub = os.path.join(base, "project")
        os.mkdir(sub)
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_tool(f"cd {sub}"), _tool("pwd"),
                               _final("ok")])
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime)
        res = loop.run("enter project", brain, system_prompt="", session_id="s",
                       scope="s")
        self.assertIn(sub, res.steps[1].result["stdout"])
        host.close_all()
        runtime.close_all()

    def test_unknown_tool_is_reported_not_fatal(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([
            json.dumps({"action": "tool", "tool": "nope", "args": {}}),
            _final("answered anyway")])
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", brain, system_prompt="", session_id="s", scope="s")
        self.assertFalse(res.steps[0].ok)
        self.assertIn("unknown tool", res.steps[0].error)
        self.assertEqual(res.text, "answered anyway")
        host.close_all()
        runtime.close_all()

    def test_tool_exception_is_reported_not_fatal(self):
        reg, host, runtime = _stack()
        # missing required arg -> registry validation error
        brain = ScriptedBrain([
            json.dumps({"action": "tool", "tool": "runtime_command",
                        "args": {}}),
            _final("done")])
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", brain, system_prompt="", session_id="s", scope="s")
        self.assertFalse(res.steps[0].ok)
        self.assertEqual(res.text, "done")
        host.close_all()
        runtime.close_all()

    def test_plain_text_is_treated_as_final(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain(["just a normal answer"])
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "hi", brain, system_prompt="", session_id="s", scope="s")
        self.assertEqual(res.text, "just a normal answer")
        self.assertEqual(res.tool_calls, 0)
        host.close_all()
        runtime.close_all()

    def test_max_steps_is_bounded(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_tool("echo x") for _ in range(10)])
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime, max_steps=3)
        res = loop.run("loop forever", brain, system_prompt="", session_id="s",
                       scope="s")
        self.assertEqual(res.tool_calls, 3)
        self.assertEqual(res.stopped_reason, "max_steps")
        host.close_all()
        runtime.close_all()

    def test_caller_error_stops_loop_cleanly(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([RuntimeError("model down")])
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", brain, system_prompt="", session_id="s", scope="s")
        self.assertFalse(res.ok)
        self.assertEqual(res.stopped_reason, "error")
        host.close_all()
        runtime.close_all()

    def test_calling_model_sees_prior_tool_results(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_tool("echo 12345"), _final("saw it")])
        AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", brain, system_prompt="", session_id="s", scope="s")
        # 2nd model call must contain the first result
        self.assertIn("12345", str(brain.calls[1]))
        host.close_all()
        runtime.close_all()


class LoopContextTests(unittest.TestCase):
    def test_execution_history_is_recorded_and_scoped(self):
        reg, host, runtime = _stack()
        hist = AgentExecutionHistory()
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime, execution_history=hist)
        brain = ScriptedBrain([_tool("echo a"), _final("done")])
        loop.run("x", brain, system_prompt="", session_id="s", scope="scope-A")
        self.assertEqual(len(hist.entries("scope-A")), 1)
        self.assertEqual(hist.entries("scope-B"), [])
        host.close_all()
        runtime.close_all()

    def test_execution_history_context_is_bounded(self):
        hist = AgentExecutionHistory()
        for i in range(60):
            hist.record("s", "terminal_exec", ok=True, status="completed",
                        result={"stdout": "x" * 500}, step=i)
        text = hist.context_text("s", max_entries=5, max_chars=300)
        self.assertLessEqual(len(text), 301)

    def test_execution_history_bounds_the_number_of_scopes(self):
        """A long-lived server serving many conversations must not grow the
        scope table without bound."""
        hist = AgentExecutionHistory(max_entries=2, max_scopes=3)
        for scope in ("a", "b", "c", "d"):
            hist.record(scope, "terminal_exec", ok=True, status="completed",
                        result={"stdout": scope})
        self.assertEqual(hist.entries("a"), [])      # LRU scope evicted
        self.assertTrue(hist.entries("d"))
        self.assertTrue(hist.entries("c"))

    def test_context_blocks_reach_the_model(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_final("ok")])
        AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", brain, system_prompt="", session_id="s", scope="s",
            context_blocks=["Live terminal session state:\nTERMINAL-MARKER"])
        self.assertIn("TERMINAL-MARKER", str(brain.calls[0]))
        host.close_all()
        runtime.close_all()


class EventsTests(unittest.TestCase):
    def test_tool_args_in_events_are_redacted(self):
        """Regression: a model may run `export API_KEY=<secret>` or a curl
        with an Authorization header; the Activity Log's `agent.tool_call`
        args must be redacted, exactly like ToolRegistry's own tool events."""
        bus = Bus()
        reg, host, runtime = _stack()
        host.events = bus
        runtime.events = bus
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime, events=bus)
        secret = "ghp_" + "A" * 30
        brain = ScriptedBrain([
            _tool(f"export API_KEY={secret}"),
            _final("ok")])
        loop.run("x", brain, system_prompt="", session_id="s", scope="s")
        call_events = [r for r in bus.rows if r["kind"] == "agent.tool_call"]
        self.assertTrue(call_events)
        for r in call_events:
            self.assertNotIn(secret, json.dumps(r["data"]))
        host.close_all()
        runtime.close_all()

    def test_lifecycle_events_emitted(self):
        bus = Bus()
        reg, host, runtime = _stack()
        host.events = bus
        runtime.events = bus
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime, events=bus)
        brain = ScriptedBrain([_tool("echo e"), _final("ok")])
        loop.run("x", brain, system_prompt="", session_id="s", scope="s")
        kinds = bus.kinds()
        self.assertIn("terminal.started", kinds)
        self.assertIn("terminal.completed", kinds)
        self.assertIn("agent.tool_loop.started", kinds)
        self.assertIn("agent.tool_call", kinds)
        self.assertIn("agent.tool_result", kinds)
        self.assertIn("agent.tool_loop.finished", kinds)
        # completion events are terminal so the Activity Log closes the row
        done = [r for r in bus.rows if r["kind"] == "terminal.completed"][0]
        self.assertTrue(done["data"]["terminal"])
        host.close_all()
        runtime.close_all()

    def test_started_and_terminal_events_share_op(self):
        bus = Bus()
        reg, host, runtime = _stack()
        host.events = bus
        runtime.events = bus
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime, events=bus)
        brain = ScriptedBrain([_tool("echo correlated"), _final("ok")])
        loop.run("x", brain, system_prompt="", session_id="s", scope="s")
        starts = {r["data"].get("op") for r in bus.rows
                  if r["kind"] == "terminal.started"}
        terms = {r["data"].get("op") for r in bus.rows
                 if r["kind"] in ("terminal.completed", "terminal.failed")}
        self.assertTrue(starts)
        self.assertEqual(starts, terms)
        outputs = [r for r in bus.rows if r["kind"] == "terminal.output"]
        self.assertTrue(outputs)
        for r in outputs:
            self.assertIn(r["data"].get("op"), starts)
        host.close_all()
        runtime.close_all()

    def test_tool_loop_terminal_reports_its_own_duration(self):
        """Regression: the loop row used to inherit a per-tool `duration_ms`
        from an intermediate agent.tool_result, so a ~108s loop rendered as
        "COMPLETE · 3ms". The loop's terminal event must carry the loop's own
        start->terminal duration, and the progress event must not masquerade
        as one."""
        bus = Bus()
        reg, host, runtime = _stack()
        host.events = bus
        runtime.events = bus
        loop = AgentToolLoop(reg, terminal=host, runtime=runtime, events=bus)
        brain = ScriptedBrain([_tool("echo a"), _final("ok")])
        loop.run("x", brain, system_prompt="", session_id="s", scope="s")
        finished = [r for r in bus.rows
                    if r["kind"] == "agent.tool_loop.finished"][0]
        self.assertTrue(finished["data"]["terminal"])
        self.assertIn("duration_ms", finished["data"])
        self.assertGreaterEqual(finished["data"]["duration_ms"], 0)
        # the per-step event carries its own tool timing under a distinct key
        # so the frontend can never read it as the loop's duration
        results = [r for r in bus.rows if r["kind"] == "agent.tool_result"]
        self.assertTrue(results)
        for r in results:
            self.assertNotIn("duration_ms", r["data"])
            self.assertIn("tool_duration_ms", r["data"])
        host.close_all()
        runtime.close_all()

    def test_background_process_lifecycle_events_close_their_op(self):
        bus = Bus()
        reg, host, runtime = _stack()
        host.events = bus
        runtime.events = bus
        out = reg.execute("terminal_start", {"command": "sleep 30"},
                          ctx=_ctx_helper(host))["result"]
        self.assertEqual(out["status"], "running")
        starts = [r for r in bus.rows if r["kind"] == "terminal.started"]
        self.assertEqual(len(starts), 1)
        op = starts[0]["data"]["op"]
        reg.execute("terminal_stop", {"process_id": out["process_id"]},
                    ctx=_ctx_helper(host))
        stopped = [r for r in bus.rows if r["kind"] == "terminal.stopped"]
        self.assertEqual([r["data"]["op"] for r in stopped], [op])
        self.assertTrue(stopped[0]["data"]["terminal"])
        host.close_all()
        runtime.close_all()

    def test_no_duplicate_execution_of_one_requested_command(self):
        reg, host, runtime = _stack()
        brain = ScriptedBrain([_tool("echo once"), _final("ok")])
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", brain, system_prompt="", session_id="s", scope="s")
        self.assertEqual(res.tool_calls, 1)
        self.assertEqual(reg.stats("runtime_command")["calls"], 1)
        hist = runtime.history("s")
        self.assertEqual([h["command"] for h in hist], ["echo once"])
        host.close_all()
        runtime.close_all()


class CallerTests(unittest.TestCase):
    class _GW:
        def __init__(self, replies):
            self.replies = list(replies)
            self.categories = []

        def chat(self, messages, model=None, max_tokens=500, category=None,
                 trace=""):
            self.categories.append(category)
            return self.replies.pop(0)

    class _Router:
        def __init__(self, replies):
            self.replies = list(replies)
            self.requests = []

        def route_request(self, req):
            self.requests.append(req)
            text = self.replies.pop(0)
            return RoutingResult(ok=True, text=text, provider="groq",
                                 model="llama")

    def test_gateway_brained_loop_uses_gateway_and_same_terminal(self):
        bus = Bus()
        reg, host, runtime = _stack()
        gw = self._GW([_tool("echo via-gateway"), _final("gateway done")])
        from astra.ai.agent_tool_loop import AgentToolLoop as L
        loop = L(reg, terminal=host, runtime=runtime, events=bus)
        res = loop.run("x", GatewayToolCaller(gw), system_prompt="",
                       session_id="shared", scope="s")
        self.assertEqual(res.text, "gateway done")
        self.assertEqual(res.tool_calls, 1)
        self.assertIn("via-gateway", res.steps[0].result["stdout"])
        # same ToolRegistry -> same Terminal session the provider path uses
        self.assertIn("echo via-gateway",
                      [h["command"] for h in runtime.history("shared")])
        host.close_all()
        runtime.close_all()

    def test_provider_brained_loop_uses_router(self):
        reg, host, runtime = _stack()
        router = self._Router([_tool("echo via-provider"), _final("provider done")])
        caller = ProviderToolCaller(router, task_type="coding")
        res = AgentToolLoop(reg, terminal=host, runtime=runtime).run(
            "x", caller, system_prompt="", session_id="shared", scope="s")
        self.assertEqual(res.text, "provider done")
        self.assertTrue(router.requests, "router must have been called")
        self.assertEqual(router.requests[0].task_type, "coding")
        host.close_all()
        runtime.close_all()

    def test_catalog_lists_runtime_tools_and_not_the_host_terminal(self):
        reg, host, runtime = _stack()
        catalog = build_tool_catalog(reg)
        # the Agent's execution surface is the isolated runtime ...
        self.assertIn("runtime_command", catalog)
        self.assertIn("runtime_package_install", catalog)
        self.assertIn("read_file", catalog)
        # ... and the legacy HOST terminal tools are never advertised to a
        # model driving Agent execution (they are also blocked at execute).
        self.assertNotIn("terminal_exec", catalog)
        self.assertNotIn("terminal_start", catalog)
        # the tools are still registered — only Agent execution is refused
        self.assertIsNotNone(reg.get("terminal_exec"))
        host.close_all()
        runtime.close_all()


if __name__ == "__main__":
    unittest.main()
