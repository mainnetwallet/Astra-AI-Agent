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
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry

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


def _stack(manager=None):
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy)
    register_builtins(reg)
    manager = manager or TerminalManager()
    register_terminal_tools(reg, manager)
    return reg, manager


class ContextReachesBothTests(unittest.TestCase):
    def _prime(self, manager, session_id, marker):
        manager.get(session_id).exec(f"echo {marker}")

    def test_gateway_understand_prompt_has_terminal_context(self):
        reg, manager = _stack()
        self._prime(manager, "conv-7", "TERM-MARK-7")
        gw = FakeGateway([understand(final_request="run it"),
                          verdict("complete")])
        rt = FakeRouter(["done"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager)
        history = ConversationContext(messages=[], conversation_id=7)
        out = pipe.run("run it", history=history)
        self.assertTrue(out["ok"])
        self.assertIn("TERM-MARK-7", gw.calls[0][1]["content"])
        manager.close_all()

    def test_provider_messages_have_terminal_context(self):
        reg, manager = _stack()
        self._prime(manager, "conv-8", "TERM-MARK-8")
        gw = FakeGateway([understand(final_request="run it"),
                          verdict("complete")])
        rt = FakeRouter(["done"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager)
        history = ConversationContext(messages=[], conversation_id=8)
        pipe.run("run it", history=history)
        first_provider_messages = rt.requests[0].messages
        joined = "\n".join(str(m.get("content")) for m in first_provider_messages)
        self.assertIn("TERM-MARK-8", joined)
        manager.close_all()

    def test_execution_history_reaches_provider_on_later_call(self):
        reg, manager = _stack()
        gw = FakeGateway([understand(final_request="do a thing"),
                          verdict("complete")])
        rt = FakeRouter(["done"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager)
        history = ConversationContext(messages=[], conversation_id=11)
        # First turn: the model asks for a terminal command then finishes.
        rt.outputs = [json.dumps({"action": "tool", "tool": "terminal_exec",
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
        manager.close_all()


class ChatHistoryIntactTests(unittest.TestCase):
    def test_history_turns_still_reach_gateway_and_provider(self):
        reg, manager = _stack()
        gw = FakeGateway([understand(final_request="what is my name?"),
                          verdict("complete")])
        rt = FakeRouter(["Alex."])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager)
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
        manager.close_all()


class SessionIsolationTests(unittest.TestCase):
    def test_unrelated_conversations_get_isolated_sessions(self):
        reg, manager = _stack()
        base_a = tempfile.mkdtemp()
        base_b = tempfile.mkdtemp()
        manager.get("conv-1").exec(f"cd {base_a}")
        manager.get("conv-2").exec(f"cd {base_b}")
        self.assertEqual(manager.get("conv-1").cwd, os.path.realpath(base_a))
        self.assertEqual(manager.get("conv-2").cwd, os.path.realpath(base_b))
        self.assertNotIn("conv-1", manager.context_text("conv-2"))
        manager.close_all()


class GatewayBrainTests(unittest.TestCase):
    def test_gateway_brain_drives_the_same_loop_and_terminal(self):
        reg, manager = _stack()

        def tool(command):
            return json.dumps({"action": "tool", "tool": "terminal_exec",
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
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager,
                            agent_brain="gateway")
        out = pipe.run("do it", history=ConversationContext(
            messages=[], conversation_id=21))
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "corrected answer")
        self.assertEqual(out["data"]["tool_loop"]["tool_calls"], 1)
        self.assertEqual(out["data"]["tool_loop"]["steps"][0]["tool"],
                         "terminal_exec")
        # the ONE shared Terminal really executed the command
        session = manager.get("conv-21", create=False)
        self.assertEqual([h["command"] for h in session.history()],
                         ["echo GATEWAY-BRAIN"])
        # the correction went back to the Gateway and carried a tool summary
        self.assertIn("ALREADY performed", str(gw.calls[-2]))
        self.assertEqual(rt.requests, [])
        manager.close_all()


class CorrectionPhaseTests(unittest.TestCase):
    def test_correction_messages_exclude_tool_protocol(self):
        reg, manager = _stack()
        gw = FakeGateway([understand(final_request="do it"),
                          verdict("incomplete"), verdict("complete")])
        rt = FakeRouter([json.dumps({"action": "tool", "tool": "terminal_exec",
                                     "args": {"command": "echo c"}}),
                         json.dumps({"action": "final", "answer": "partial"}),
                         "corrected answer"])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager)
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
        manager.close_all()


class ToolLoopTraceTests(unittest.TestCase):
    def test_pipeline_records_tool_loop_trace(self):
        reg, manager = _stack()
        manager.get("conv-5").exec("echo VERIFY-CTX")
        gw = FakeGateway([understand(final_request="do it"),
                          verdict("complete")])
        rt = FakeRouter([json.dumps({"action": "tool", "tool": "terminal_exec",
                                     "args": {"command": "echo TRACE"}}),
                         json.dumps({"action": "final", "answer": "traced"})])
        pipe = ChatPipeline(gw, rt, registry=reg, terminal=manager)
        out = pipe.run("do it", history=ConversationContext(
            messages=[], conversation_id=5))
        trace = out["data"]["tool_loop"]
        self.assertEqual(trace["tool_calls"], 1)
        self.assertEqual(trace["stopped_reason"], "final")
        self.assertEqual(trace["steps"][0]["tool"], "terminal_exec")
        self.assertEqual(out["data"]["terminal_session"], "conv-5")
        # The Gateway's VERIFY call must see the same terminal + execution
        # context the Provider saw (it judges whether the work is really done).
        verify_prompt = gw.calls[1][1]["content"]
        self.assertIn("VERIFY-CTX", verify_prompt)
        self.assertIn("Actions already taken", verify_prompt)
        self.assertIn("Execution context", verify_prompt)
        manager.close_all()


if __name__ == "__main__":
    unittest.main()
