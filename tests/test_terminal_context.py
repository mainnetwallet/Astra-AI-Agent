"""Both the Gateway AND the Provider receive terminal/execution context,
and terminal state stays isolated per conversation — while Chat History is
untouched.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.conversation_context import ConversationContext
from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
from astra.ai.router import RoutingResult
from astra.core.permissions import Policy
from astra.runtime.tools import register_runtime_tools
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry
from tests.helpers import LocalRuntimeStub, requires_posix_host

TARGETS = [{"provider": "groq", "model": "llama", "capabilities": ["chat", "coding"],
            "quality": "high", "context_window": 32000}]


def understand(final_request="", provider="groq", model="llama"):
    return json.dumps({"final_request": final_request, "was_incomplete": False,
                       "provider": provider, "model": model,
                       "criteria": ["answers"], "reason": "fit"})


def verdict(v="complete"):
    return json.dumps({"verdict": v, "missing": [], "action": "fix",
                       "instructions": ""})


class FakeGateway:
    def __init__(self, replies, usable=True):
        self.replies = list(replies)
        self.calls = []
        self.usable = usable
        self.last_model = "gw-model"

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None,
             trace=""):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class FakeRouter:
    def __init__(self, outputs, targets=None):
        self.outputs = list(outputs)
        self.requests = []
        self._targets = targets if targets is not None else TARGETS

    def available_targets(self):
        return list(self._targets)

    def route_request(self, req):
        self.requests.append(req)
        out = self.outputs.pop(0) if self.outputs else "fallback"
        if out is None:
            return RoutingResult(ok=False, error="all providers failed")
        return RoutingResult(ok=True, text=out, provider="groq", model="llama")


def _stack():
    """Real registry, real HOST terminal tools and real RUNTIME tools — only
    the runtime's process backend is a local stub (fast, hermetic; real
    isolation lives in tests/test_runtime.py). Returns
    `(registry, host_terminal, runtime)`."""
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy)
    register_builtins(reg)
    host = TerminalManager()
    register_terminal_tools(reg, host)
    runtime = LocalRuntimeStub()
    register_runtime_tools(reg, runtime)
    return reg, host, runtime


class ContextReachesBothTests(unittest.TestCase):
    def _prime(self, runtime, session_id, marker):
        runtime.exec_command(f"echo {marker}", session_id=session_id)

    @requires_posix_host
    def test_gateway_understand_prompt_has_terminal_context(self):
        reg, host, runtime = _stack()
        self._prime(runtime, "conv-7", "TERM-MARK-7")
        gw = FakeGateway([understand(final_request="run it"),
                          verdict("complete")])
        rt = FakeRouter(["done"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        history = ConversationContext(messages=[], conversation_id=7)
        out = pipe.run("run it", history=history)
        self.assertTrue(out["ok"])
        self.assertIn("TERM-MARK-7", gw.calls[0][1]["content"])
        host.close_all()
        runtime.close_all()

    @requires_posix_host
    def test_provider_messages_have_terminal_context(self):
        reg, host, runtime = _stack()
        self._prime(runtime, "conv-8", "TERM-MARK-8")
        gw = FakeGateway([understand(final_request="run it"),
                          verdict("complete")])
        rt = FakeRouter(["done"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        history = ConversationContext(messages=[], conversation_id=8)
        pipe.run("run it", history=history)
        first_provider_messages = rt.requests[0].messages
        joined = "\n".join(str(m.get("content")) for m in first_provider_messages)
        self.assertIn("TERM-MARK-8", joined)
        host.close_all()
        runtime.close_all()

    def test_execution_history_reaches_provider_on_later_call(self):
        reg, host, runtime = _stack()
        gw = FakeGateway([understand(final_request="do a thing"),
                          verdict("complete")])
        rt = FakeRouter(["done"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        history = ConversationContext(messages=[], conversation_id=11)
        # First turn: the model asks for a terminal command then finishes.
        rt.outputs = [json.dumps({"action": "tool", "tool": "runtime_command",
                                  "args": {"command": "echo HIST-11"}}),
                      json.dumps({"action": "final", "answer": "first done"})]
        pipe.run("do a thing", history=history)
        # Second turn sees the execution history.
        rt.outputs = ["second done"]
        gw.replies = [understand(final_request="do another"),
                      verdict("complete")]
        out = pipe.run("do another", history=history)
        self.assertTrue(out["ok"])
        joined = "\n".join(str(m.get("content"))
                           for m in rt.requests[-1].messages)
        self.assertIn("execution history", joined.lower())
        host.close_all()
        runtime.close_all()


class ChatHistoryIntactTests(unittest.TestCase):
    def test_history_turns_still_reach_gateway_and_provider(self):
        reg, host, runtime = _stack()
        gw = FakeGateway([understand(final_request="what is my name?"),
                          verdict("complete")])
        rt = FakeRouter(["Alex."])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        history = ConversationContext(messages=[
            {"role": "user", "content": "my name is Alex"},
            {"role": "assistant", "content": "nice to meet you, Alex"}],
            conversation_id=3)
        pipe.run("why?", history=history)
        self.assertIn("my name is Alex", gw.calls[0][1]["content"])
        provider_msgs = rt.requests[0].messages
        contents = [m["content"] for m in provider_msgs
                    if isinstance(m.get("content"), str)]
        self.assertIn("my name is Alex", contents)
        self.assertIn("nice to meet you, Alex", contents)
        # current message exactly once, last
        self.assertEqual(provider_msgs[-1]["role"], "user")
        host.close_all()
        runtime.close_all()


class SessionIsolationTests(unittest.TestCase):
    def test_unrelated_conversations_get_isolated_sessions(self):
        reg, host, runtime = _stack()
        base_a = tempfile.mkdtemp()
        base_b = tempfile.mkdtemp()
        host.get("conv-1").exec(f"cd {base_a}")
        host.get("conv-2").exec(f"cd {base_b}")
        self.assertEqual(host.get("conv-1").cwd, os.path.realpath(base_a))
        self.assertEqual(host.get("conv-2").cwd, os.path.realpath(base_b))
        self.assertNotIn("conv-1", host.context_text("conv-2"))
        host.close_all()
        runtime.close_all()

    @requires_posix_host
    def test_explicit_session_id_isolates_cid_less_callers(self):
        """With no conversation_id an embedder can still isolate runs by
        passing session_id; two ids must never share cwd/history."""
        reg, host, runtime = _stack()
        base_a = tempfile.mkdtemp()

        def tool(command):
            return json.dumps({"action": "tool", "tool": "runtime_command",
                               "args": {"command": command}})

        def final(answer):
            return json.dumps({"action": "final", "answer": answer})

        gw = FakeGateway([understand(final_request="cd"),
                          verdict("complete")])
        rt = FakeRouter([tool(f"cd {base_a}"), final("a done")])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        pipe.run("go to a", session_id="run-A")

        gw.replies = [understand(final_request="pwd"), verdict("complete")]
        rt.outputs = [tool("pwd"), final("here")]
        pipe.run("where am i", session_id="run-B")

        self.assertEqual(os.path.realpath(runtime.cwd("run-A")),
                         os.path.realpath(base_a))
        self.assertNotEqual(os.path.realpath(runtime.cwd("run-B")),
                            os.path.realpath(base_a))
        host.close_all()
        runtime.close_all()

    @requires_posix_host
    def test_concurrent_conversations_keep_terminal_state_isolated(self):
        """Two chats served at the same time, through ONE shared pipeline
        stack (registry + terminal + execution history), must each keep
        their own cwd and must not corrupt each other's state."""
        import threading
        from astra.ai.execution_history import AgentExecutionHistory

        reg, host, runtime = _stack()
        shared_history = AgentExecutionHistory()
        base_a = tempfile.mkdtemp()
        base_b = tempfile.mkdtemp()
        results = {}
        errors = []

        def worker(cid, base):
            try:
                def tool(command):
                    return json.dumps({"action": "tool",
                                       "tool": "runtime_command",
                                       "args": {"command": command}})

                def final(answer):
                    return json.dumps({"action": "final", "answer": answer})

                gw = FakeGateway([understand(final_request="work"),
                                  verdict("complete")])
                rt = FakeRouter([tool(f"cd {base}"), tool("pwd"),
                                 final("done")])
                pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                                    runtime=runtime,
                                    execution_history=shared_history)
                hist = ConversationContext(messages=[], conversation_id=cid)
                results[cid] = pipe.run("work", history=hist)
            except Exception as e:          # surfaced by the assertions below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(1, base_a)),
                   threading.Thread(target=worker, args=(2, base_b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertTrue(results.get(1, {}).get("ok"), results)
        self.assertTrue(results.get(2, {}).get("ok"), results)
        self.assertEqual(os.path.realpath(runtime.cwd("conv-1")),
                         os.path.realpath(base_a))
        self.assertEqual(os.path.realpath(runtime.cwd("conv-2")),
                         os.path.realpath(base_b))
        host.close_all()
        runtime.close_all()


class NoConversationSessionTests(unittest.TestCase):
    """Regression: a caller with no conversation_id and no explicit
    session_id used to share ONE process-wide "default" terminal session,
    so unrelated callers inherited each other's cwd/history/processes.
    Such a turn must get its own request-scoped session instead."""

    class _Bus:
        def __init__(self):
            self.rows = []

        def emit(self, kind, agent="", **data):
            self.rows.append({"kind": kind, "agent": agent, "data": data})
            return self.rows[-1]

    @staticmethod
    def _tool(command):
        return json.dumps({"action": "tool", "tool": "runtime_command",
                           "args": {"command": command}})

    @staticmethod
    def _final(answer):
        return json.dumps({"action": "final", "answer": answer})

    def test_cidless_turns_get_isolated_request_scoped_sessions(self):
        reg, host, runtime = _stack()
        bus = self._Bus()
        runtime.events = bus
        base_a = tempfile.mkdtemp()

        gw = FakeGateway([understand(final_request="go"), verdict("complete")])
        rt = FakeRouter([self._tool(f"cd {base_a}"), self._final("done")])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        out1 = pipe.run("go somewhere")
        self.assertTrue(out1["ok"])
        self.assertTrue(out1["data"]["terminal_session"].startswith("req-"))
        # the request-scoped session is cleaned up when the turn ends
        self.assertEqual(runtime.session_ids(), [])

        # a second cid-less turn must NOT see turn 1's cwd
        gw.replies = [understand(final_request="where"), verdict("complete")]
        rt.outputs = [self._tool("pwd"), self._final("here")]
        self.assertTrue(pipe.run("where am i")["ok"])

        started = [r["data"].get("cwd") for r in bus.rows
                   if r["kind"] == "terminal.started"]
        self.assertGreaterEqual(len(started), 2)
        for cwd in started:
            if cwd:
                self.assertNotEqual(os.path.realpath(cwd),
                                    os.path.realpath(base_a))
        self.assertEqual(runtime.session_ids(), [])

    @requires_posix_host
    def test_conversation_session_still_persists_across_turns(self):
        reg, host, runtime = _stack()
        base = tempfile.mkdtemp()
        gw = FakeGateway([understand(final_request="go"), verdict("complete")])
        rt = FakeRouter([self._tool(f"cd {base}"), self._final("done")])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        hist = ConversationContext(messages=[], conversation_id=11)
        pipe.run("go", history=hist)
        # a conversation's session is kept (NOT closed at turn end)
        self.assertIn("conv-11", runtime.session_ids())
        self.assertEqual(os.path.realpath(runtime.cwd("conv-11")),
                         os.path.realpath(base))
        host.close_all()
        runtime.close_all()


class GatewayBrainTests(unittest.TestCase):
    @requires_posix_host
    def test_gateway_brain_drives_the_same_loop_and_terminal(self):
        reg, host, runtime = _stack()

        def tool(command):
            return json.dumps({"action": "tool", "tool": "runtime_command",
                               "args": {"command": command}})

        def final(answer):
            return json.dumps({"action": "final", "answer": answer})

        gw = FakeGateway([
            understand(final_request="do it"),           # 1. understand
            tool("echo GATEWAY-BRAIN"),                  # 2. loop decides
            final("partial"),                            # 3. loop answers
            verdict("incomplete"),                       # 4. verify
            "corrected answer",                          # 5. correction
            verdict("complete"),                         # 6. re-verify
        ])
        rt = FakeRouter([])   # the provider path must not be used
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host, runtime=runtime,
                            agent_brain="gateway")
        out = pipe.run("do it", history=ConversationContext(
            messages=[], conversation_id=21))
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "corrected answer")
        self.assertEqual(out["data"]["tool_loop"]["tool_calls"], 1)
        self.assertEqual(out["data"]["tool_loop"]["steps"][0]["tool"],
                         "runtime_command")
        # the ONE shared Terminal really executed the command
        self.assertEqual([h["command"] for h in runtime.history("conv-21")],
                         ["echo GATEWAY-BRAIN"])
        # the correction went back to the Gateway and carried a tool summary
        self.assertIn("ALREADY performed", str(gw.calls[-2]))
        self.assertEqual(rt.requests, [])
        host.close_all()
        runtime.close_all()


class CorrectionPhaseTests(unittest.TestCase):
    @requires_posix_host
    def test_correction_messages_exclude_tool_protocol(self):
        reg, host, runtime = _stack()
        workdir = tempfile.mkdtemp()
        runtime.exec_command(f"cd {workdir}", session_id="conv-13")
        gw = FakeGateway([understand(final_request="do it"),
                          verdict("incomplete"), verdict("complete")])
        rt = FakeRouter([json.dumps({"action": "tool", "tool": "runtime_command",
                                     "args": {"command": "echo c"}}),
                         json.dumps({"action": "final", "answer": "partial"}),
                         "corrected answer"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        out = pipe.run("do it", history=ConversationContext(
            messages=[], conversation_id=13))
        self.assertTrue(out["ok"])
        correction = rt.requests[-1].messages
        joined = str(correction)
        # no tool protocol leaks into the correction prompt...
        self.assertNotIn("Reply with ONE JSON object", joined)
        self.assertNotIn('"action": "tool"', joined)
        # ...but the tools already run are summarised for the correcting model
        self.assertIn("ALREADY performed", joined)
        # ...and it sees the live terminal state too (regression: the
        # correction call used to get only the tool summary, not terminal).
        self.assertIn(workdir, joined)
        self.assertIn("Current execution context", joined)
        host.close_all()
        runtime.close_all()


class MultimodalToolLoopTests(unittest.TestCase):
    """Regression: the agent tool loop stringified multimodal content, so an
    attached image was silently dropped on the production (terminal-wired)
    path."""

    def test_image_attachment_reaches_the_provider_as_a_content_part(self):
        import base64
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        workdir = tempfile.mkdtemp()
        path = os.path.join(workdir, "x.png")
        with open(path, "wb") as fh:
            fh.write(png)
        reg, host, runtime = _stack()
        gw = FakeGateway([understand(final_request="describe"),
                          verdict("complete")])
        rt = FakeRouter(["a picture"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        out = pipe.run("what is in this image?", attachments=[
            {"family": "image", "storage_path": path,
             "detected_type": "image/png", "original_filename": "x.png"}])
        self.assertTrue(out["ok"])
        content = rt.requests[0].messages[-1]["content"]
        self.assertIsInstance(content, list)
        self.assertTrue(any(isinstance(p, dict) and p.get("type") == "image_url"
                            for p in content))
        host.close_all()
        runtime.close_all()


class ToolLoopTraceTests(unittest.TestCase):
    @requires_posix_host
    def test_pipeline_records_tool_loop_trace(self):
        reg, host, runtime = _stack()
        runtime.exec_command("echo VERIFY-CTX", session_id="conv-5")
        gw = FakeGateway([understand(final_request="do it"),
                          verdict("complete")])
        rt = FakeRouter([json.dumps({"action": "tool", "tool": "runtime_command",
                                     "args": {"command": "echo TRACE"}}),
                         json.dumps({"action": "final", "answer": "traced"})])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=host,
                              runtime=runtime)
        out = pipe.run("do it", history=ConversationContext(
            messages=[], conversation_id=5))
        trace = out["data"]["tool_loop"]
        self.assertEqual(trace["tool_calls"], 1)
        self.assertEqual(trace["stopped_reason"], "final")
        self.assertEqual(trace["steps"][0]["tool"], "runtime_command")
        self.assertEqual(out["data"]["terminal_session"], "conv-5")
        # The Gateway's VERIFY call must see the same terminal + execution
        # context the Provider saw (it judges whether the work is really done).
        verify_prompt = gw.calls[1][1]["content"]
        self.assertIn("VERIFY-CTX", verify_prompt)
        self.assertIn("Actions already taken", verify_prompt)
        self.assertIn("Execution context", verify_prompt)
        host.close_all()
        runtime.close_all()


if __name__ == "__main__":
    unittest.main()
