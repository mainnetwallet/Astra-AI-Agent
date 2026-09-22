"""Root-cause regression tests for the internal tool-call protocol leak.

Reproduces, against the REAL runtime stack (real `ChatPipeline`, real
`AgentToolLoop`, real `ToolRegistry`, real `TerminalManager` — only the
model's raw text reply is scripted, exactly like `tests/test_agent_tool_loop.py`
already does), the exact failure described in the bug report:

    User provides GitHub credentials and asks: "Clone repo"

A model virtually never replies with bare, unwrapped JSON — it prefaces or
follows the tool-call object with a sentence of natural language. The old
`agent_tool_loop._parse_action` required the ENTIRE trimmed reply to start
with "{" and end with "}" before it would even try the lenient/embedded
JSON extractor, so a prefaced reply was never recognized as a tool call at
all: the loop treated the whole raw reply (JSON protocol — action / tool /
args / session_id / thought — included) as the model's final answer and
handed it straight back to the user, and the tool was never executed.

These tests prove:
  1. A prefaced tool-call reply is now actually executed by the terminal,
     not echoed back as text (`test_prefaced_tool_call_is_executed...`).
  2. No internal protocol key/shape ever reaches `reply` (the ONE field
     the chat UI renders), across a normal run, a run that hits
     max_steps mid-tool-call, and a genuinely unparsable reply
     (`test_*_never_in_final_reply`, `test_max_steps_mid_tool_call...`,
     `test_unparsable_reply_is_not_leaked_verbatim`).
  3. A credential echoed back inside a tool result/final answer is
     redacted before it ever reaches `reply`
     (`test_credential_is_redacted_from_final_reply`).
  4. A multi-step loop (tool -> tool -> final) leaks nothing at any
     intermediate step (`test_multi_step_loop_leaks_nothing`).
  5. Ordinary chat with no tool involved is completely unaffected
     (`test_plain_chat_without_tools_is_unaffected`).
"""
import json
import unittest

from astra.agent import Agent
from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.response_boundary import sanitize_final_response
from astra.core.permissions import Policy
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools.builtins import register_builtins
from astra.tools.registry import ToolRegistry

from tests.test_chat_pipeline import FakeGateway, FakeRouter

_PROTOCOL_MARKERS = ('"action"', '"tool"', '"args"', '"session_id"',
                     '"thought"', 'terminal_exec')


def _tool_call(command, **extra):
    payload = {"action": "tool", "tool": "terminal_exec",
              "args": {"command": command}, "thought": "run it",
              "session_id": "sess-abc123"}
    payload.update(extra)
    return json.dumps(payload)


def _final(answer):
    return json.dumps({"action": "final", "answer": answer})


def _stack():
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "system_action"])
    reg = ToolRegistry(policy=policy)
    register_builtins(reg)
    manager = TerminalManager()
    register_terminal_tools(reg, manager)
    return reg, manager


def _pipeline(outputs, reg, manager, **kw):
    gw = FakeGateway([], usable=False)   # verification skipped: rr.text is
                                          # the reply, unmodified except by
                                          # the response-boundary guard —
                                          # exactly what we're testing.
    rt = FakeRouter(list(outputs))
    return ChatPipeline(gw, rt, max_tokens=800, registry=reg,
                        terminal=manager, **kw), gw, rt


class PrefacedToolCallLeakTests(unittest.TestCase):
    """Case 3 from the bug report."""

    def test_prefaced_tool_call_is_executed_not_leaked(self):
        reg, manager = _stack()
        outputs = [
            "Sure, I'll clone that repository for you now.\n\n" +
            _tool_call("echo cloned-ok"),
            _final("I cloned the repository successfully."),
        ]
        pipe, gw, rt = _pipeline(outputs, reg, manager)
        try:
            result = pipe.run(
                "Here is my GitHub token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
                " for repo mainnetwallet/Astra-AI-Agent. Clone repo",
                conversation_id="c-clone")
        finally:
            manager.close_all()

        self.assertTrue(result["ok"])
        reply = result["reply"]

        # 1) the tool was actually executed, not just described.
        loop_trace = result["data"].get("tool_loop") or {}
        self.assertEqual(loop_trace.get("tool_calls"), 1)
        step0 = loop_trace["steps"][0]
        self.assertEqual(step0["tool"], "terminal_exec")
        self.assertTrue(step0["ok"])

        # 2) no internal protocol shape reached the user-facing reply.
        for marker in _PROTOCOL_MARKERS:
            self.assertNotIn(marker, reply)
        self.assertIn("cloned", reply.lower())

        # 3) the user's own pasted token never reached the reply either.
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", reply)

    def test_multi_step_loop_leaks_nothing(self):
        reg, manager = _stack()
        outputs = [
            "First I'll check the directory.\n" + _tool_call("pwd"),
            "Now cloning.\n" + _tool_call("echo cloned-ok"),
            _final("Done — cloned into the current directory."),
        ]
        pipe, gw, rt = _pipeline(outputs, reg, manager)
        try:
            result = pipe.run("Clone repo", conversation_id="c-multi")
        finally:
            manager.close_all()

        self.assertTrue(result["ok"])
        reply = result["reply"]
        self.assertEqual(result["data"]["tool_loop"]["tool_calls"], 2)
        for marker in _PROTOCOL_MARKERS:
            self.assertNotIn(marker, reply)

    def test_max_steps_mid_tool_call_does_not_leak_raw_json(self):
        """If the model never emits a "final" action before the step
        budget runs out, the boundary guard must still keep the last raw
        (tool-call-shaped) reply out of the user-facing text."""
        reg, manager = _stack()
        outputs = [_tool_call(f"echo step-{i}") for i in range(3)]
        pipe, gw, rt = _pipeline(outputs, reg, manager, max_tool_steps=3)
        try:
            result = pipe.run("Clone repo", conversation_id="c-maxsteps")
        finally:
            manager.close_all()

        reply = result["reply"]
        for marker in _PROTOCOL_MARKERS:
            self.assertNotIn(marker, reply)

    def test_unparsable_reply_is_not_leaked_verbatim(self):
        """A genuinely truncated/malformed protocol reply (e.g. cut off by
        a max_tokens limit) cannot be recovered as a tool call, but it must
        still never reach the user verbatim."""
        reg, manager = _stack()
        truncated = ('Cloning now.\n\n{"action": "tool", "tool": '
                    '"terminal_exec", "args": {"command": "git clone')
        outputs = [truncated]
        pipe, gw, rt = _pipeline(outputs, reg, manager)
        try:
            result = pipe.run("Clone repo", conversation_id="c-trunc")
        finally:
            manager.close_all()

        reply = result["reply"]
        for marker in ('"action"', '"tool"', '"args"'):
            self.assertNotIn(marker, reply)


class CredentialRedactionTests(unittest.TestCase):
    def test_credential_is_redacted_from_final_reply(self):
        reg, manager = _stack()
        token = "ghp_" + "Z" * 36
        outputs = [_final(f"Cloned using token {token} successfully.")]
        pipe, gw, rt = _pipeline(outputs, reg, manager)
        try:
            result = pipe.run("Clone repo", conversation_id="c-cred")
        finally:
            manager.close_all()
        self.assertNotIn(token, result["reply"])
        self.assertIn("redacted", result["reply"].lower())

    def test_agent_exception_fallback_redacts_credentials(self):
        """`Agent.handle`'s catch-all must not let a credential inside an
        exception message (e.g. a failed authenticated git clone URL)
        reach the frontend."""
        token = "ghp_" + "Y" * 36

        class BoomingPipeline:
            def run(self, *a, **kw):
                raise RuntimeError(
                    f"clone failed: https://{token}@github.com/x/y.git")

        agent = Agent(pipeline=BoomingPipeline())
        result = agent.handle("clone it")
        self.assertFalse(result["ok"])
        self.assertNotIn(token, result["reply"])
        self.assertNotIn(token, result["data"]["error"])


class SanitizeFinalResponseUnitTests(unittest.TestCase):
    """Direct unit coverage of the boundary guard itself."""

    def test_strips_embedded_protocol_json(self):
        text = ('Sure.\n\n{"action": "tool", "tool": "terminal_exec", '
               '"args": {"command": "ls"}, "session_id": "s1", '
               '"thought": "list files"}')
        out = sanitize_final_response(text)
        for marker in _PROTOCOL_MARKERS:
            self.assertNotIn(marker, out)

    def test_leaves_ordinary_json_untouched(self):
        """A user who legitimately asked for a JSON example must not have
        it stripped — only protocol-shaped objects (2+ protocol keys) are
        removed."""
        text = 'Here you go: {"name": "Alice", "age": 30}'
        self.assertEqual(sanitize_final_response(text), text)

    def test_redacts_github_token(self):
        token = "ghp_" + "A" * 36
        out = sanitize_final_response(f"done, used {token}")
        self.assertNotIn(token, out)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(sanitize_final_response(""), "")
        self.assertIsNone(sanitize_final_response(None))


class PlainChatUnaffectedTests(unittest.TestCase):
    def test_plain_chat_without_tools_is_unaffected(self):
        reg, manager = _stack()
        outputs = [_final("Dhaka is the capital of Bangladesh.")]
        pipe, gw, rt = _pipeline(outputs, reg, manager)
        try:
            result = pipe.run("What is the capital of Bangladesh?",
                              conversation_id="c-plain")
        finally:
            manager.close_all()
        self.assertTrue(result["ok"])
        self.assertIn("Dhaka", result["reply"])

    def test_no_registry_single_call_path_unaffected(self):
        """When no terminal/registry is wired (many embedders), the
        original single-provider-call path runs — the boundary guard must
        not alter a normal answer on that path either."""
        gw = FakeGateway([], usable=False)
        rt = FakeRouter(["Paris is the capital of France."])
        pipe = ChatPipeline(gw, rt, max_tokens=800)
        result = pipe.run("capital of France?", conversation_id="c-noreg")
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "Paris is the capital of France.")


if __name__ == "__main__":
    unittest.main()
