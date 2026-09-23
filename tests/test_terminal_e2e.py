"""Real end-to-end: Agent -> ChatPipeline -> ToolRegistry -> AGENT RUNTIME ->
result -> AI -> runtime again.

Only the model reply is scripted (there is no live model offline); the
Agent, ChatPipeline, ToolRegistry, runtime TOOLS, file tools, git and the
bounded verify supervisor are the real production objects. The task is a
genuine repository task: run a failing test, read the buggy file, fix it,
re-run the tests, inspect the git diff, then answer.

The Agent's shell work runs through `runtime_command` — the HOST terminal
tools are structurally blocked for Agent execution (see
tests/test_host_terminal_block.py). Only the runtime's process backend is a
local stub here so the test stays fast; real proot isolation is covered by
tests/test_runtime.py.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.agent import Agent
from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.conversation_context import ConversationContext
from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
from astra.ai.router import RoutingResult
from astra.core.permissions import Policy
from astra.runtime.tools import register_runtime_tools
from astra.terminal import TerminalManager, register_terminal_tools
from astra.tools import builtins
from astra.tools.registry import ToolRegistry
from tests.helpers import LocalRuntimeStub

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST_FILE = ("from calc import add\n\n\n"
             "def test_add():\n    assert add(2, 3) == 5\n")

TARGETS = [{"provider": "groq", "model": "coder", "capabilities": ["chat", "coding"],
            "quality": "high", "context_window": 32000}]


def understand(final_request=""):
    return json.dumps({"final_request": final_request, "was_incomplete": False,
                       "provider": "groq", "model": "coder",
                       "criteria": ["tests pass"], "reason": "fit"})


def verdict(v="complete"):
    return json.dumps({"verdict": v, "missing": [], "action": "fix",
                       "instructions": ""})


class FakeGateway:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def is_usable(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, category=None,
             trace=""):
        self.calls.append(messages)
        return self.replies.pop(0)

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class ScriptedRouter:
    """The 'model'. Each route_request returns the next scripted reply —
    real models speak the same JSON tool protocol over the same loop."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def available_targets(self):
        return list(TARGETS)

    def route_request(self, req):
        self.requests.append(req)
        text = self.replies.pop(0) if self.replies else "done"
        return RoutingResult(ok=True, text=text, provider="groq", model="coder")


def _tool(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args,
                       "thought": "next step"})


def _final(answer):
    return json.dumps({"action": "final", "answer": answer})


class RealRepoTaskE2E(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="astra-e2e-")
        self._old_workspace = builtins.WORKSPACE
        with open(os.path.join(self.repo, "calc.py"), "w") as fh:
            fh.write(BUGGY)
        with open(os.path.join(self.repo, "test_calc.py"), "w") as fh:
            fh.write(TEST_FILE)
        builtins.WORKSPACE = self.repo

    def tearDown(self):
        builtins.WORKSPACE = self._old_workspace
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_fix_failing_tests_end_to_end(self):
        policy = Policy(granted=["read", "low_risk_write", "browser_action",
                                 "system_action"])
        registry = ToolRegistry(policy=policy)
        builtins.register_builtins(registry)
        manager = TerminalManager()
        register_terminal_tools(registry, manager)
        runtime = LocalRuntimeStub(workspace=self.repo)
        register_runtime_tools(registry, runtime)

        script = [
            # 1. inspect: run the tests -> failure
            _tool("runtime_command", command=f"cd {self.repo} && python3 -B -m pytest -q -p no:cacheprovider"),
            # 2. read the failing module
            _tool("read_file", path="calc.py"),
            # 3. edit the bug
            _tool("write_file", path="calc.py", content=FIXED, overwrite=True),
            # 4. retest — no `cd` again: cwd must have persisted
            _tool("runtime_command",
                  command="python3 -B -m pytest -q -p no:cacheprovider"),
            # 5. combine with git
            _tool("runtime_command",
                  command="git init -q && git add -A && git diff --cached --stat"),
            _final("Fixed add() to subtract->add; all tests pass."),
        ]
        gw = FakeGateway([understand(final_request="fix the failing tests"),
                          verdict("complete")])
        router = ScriptedRouter(script)
        pipe = ChatPipeline(gw, router, registry=registry, terminal=manager,
                            runtime=runtime)
        agent = Agent(pipeline=pipe)

        history = ConversationContext(messages=[
            {"role": "user", "content": "I have a failing test in calc.py"},
            {"role": "assistant", "content": "Let's run it and see."}],
            conversation_id=42)
        out = agent.handle("Fix the failing tests.", history=history)

        # 1-3: final answer generated from the real loop
        self.assertTrue(out["ok"], out)
        self.assertIn("tests pass", out["reply"])
        self.assertIn("failing test", gw.calls[0][1]["content"])

        # 4: the AI decided tools were needed and ran them
        trace = out["data"]["tool_loop"]
        self.assertEqual([s["tool"] for s in trace["steps"]],
                         ["runtime_command", "read_file", "write_file",
                          "runtime_command", "runtime_command"])

        # 5-6: the model remembered earlier results (failure, file content)
        last_model_messages = str(router.requests[-2].messages)
        self.assertIn("failed", last_model_messages)
        self.assertIn("a - b", last_model_messages)

        # 7: session + cwd stayed correct across calls (in the RUNTIME)
        self.assertEqual(os.path.realpath(runtime.cwd("conv-42")),
                         os.path.realpath(self.repo))

        # 8: a failure was diagnosed and recovered from. The tool call
        # itself succeeded (the shell ran), the COMMAND failed — which is
        # exactly the structured signal the AI needs to diagnose.
        steps = trace["steps"]
        self.assertEqual(steps[0]["status"], "failed")
        self.assertEqual(steps[0]["result"]["exit_code"], 1)
        failing_output = (steps[0]["result"].get("stdout") or "") + \
            (steps[0]["result"].get("stderr") or "") + str(steps[0]["result"])
        self.assertIn("assert", failing_output)
        self.assertEqual(steps[3]["status"], "completed",
                         msg=repr(steps[3]["result"]))

        # 9: real rerun actually passed
        cmds = [h["command"] for h in runtime.history("conv-42")]
        self.assertIn("python3 -B -m pytest -q -p no:cacheprovider", cmds)

        # 10: the file on disk is genuinely fixed
        with open(os.path.join(self.repo, "calc.py")) as fh:
            self.assertIn("a + b", fh.read())

        # git diff ran and saw the change
        git_step = steps[4]
        self.assertIn("calc.py", git_step["result"].get("stdout", "") +
                      git_step["result"].get("stderr", ""))

        # 11: execution history is recorded, once per tool call (no dupes)
        self.assertEqual(registry.stats("runtime_command")["calls"], 3)
        manager.close_all()
        runtime.close_all()

    def test_tool_loop_context_survives_multiple_calls(self):
        policy = Policy(granted=["read", "low_risk_write", "system_action"])
        registry = ToolRegistry(policy=policy)
        builtins.register_builtins(registry)
        manager = TerminalManager()
        register_terminal_tools(registry, manager)
        runtime = LocalRuntimeStub()
        register_runtime_tools(registry, runtime)
        router = ScriptedRouter([
            _tool("runtime_command", command="echo FIRST-MARK"),
            _tool("runtime_command", command="echo SECOND-MARK"),
            _final("marks done"),
        ])
        gw = FakeGateway([understand(final_request="run two commands"),
                          verdict("complete")])
        pipe = ChatPipeline(gw, router, registry=registry, terminal=manager,
                            runtime=runtime)
        Agent(pipeline=pipe).handle(
            "run two commands",
            history=ConversationContext(messages=[], conversation_id=9))
        # the second model call must carry the first command's output
        self.assertIn("FIRST-MARK", str(router.requests[1].messages))
        self.assertEqual(len(runtime.history("conv-9")), 2)
        manager.close_all()
        runtime.close_all()


def registry_ctx(manager):
    from astra.core.context import ToolContext
    return ToolContext(terminal=manager, terminal_session_id="conv-42")


class RealWiringTests(unittest.TestCase):
    """bootstrap.build() wires the shared terminal into the real stack."""

    def test_build_registers_terminal_and_manager(self):
        from tests.helpers import make_stack
        stack = make_stack()
        names = {t["name"] for t in stack["registry"].list("terminal")}
        self.assertIn("terminal_exec", names)
        self.assertIsNotNone(stack["terminal"])
        from astra.core.context import ToolContext
        ctx = ToolContext(registry=stack["registry"], events=stack["events"],
                          terminal=stack["terminal"], terminal_session_id="boot")
        out = stack["registry"].execute("terminal_exec", {"command": "echo boot"},
                                        ctx=ctx)
        self.assertEqual(out["result"]["stdout"].strip(), "boot")
        stack["terminal"].close_all()
        stack["store"].close()


if __name__ == "__main__":
    unittest.main()
