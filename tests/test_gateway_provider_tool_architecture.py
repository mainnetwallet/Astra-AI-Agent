"""Gateway -> Provider -> ToolRegistry architecture, proven on the REAL
runtime stack.

This is the integration coverage for the architecture requirement:

    User
    -> Gateway  (understands intent + the LIVE runtime capabilities)
    -> Gateway decides whether real tool execution is required, and which
       capability category performs it (structured handoff)
    -> Provider / AgentToolLoop executes with the SAME live ToolRegistry
    -> tool result returns to the Provider
    -> Provider continues -> Gateway verifies using real execution evidence
    -> final natural-language response

Unlike the unit tests in `test_chat_pipeline.py` / `test_agent_tool_loop.py`
(hand-built registries and scripted fakes), every test here boots the real
stack with `tests.helpers.make_stack()` — the same `astra.bootstrap.build()`
`run.py` uses — so the real `ToolRegistry`, `TerminalManager`,
`AstraRouter` and `AstraAIGateway` objects are the production ones. Only the
two things that would otherwise hit the network are scripted on the REAL
instances:

  * `chat_pipeline.gateway.chat`      (the Gateway's own GW_* AI calls)
  * `router.route_request`            (the Provider's model calls)

so the pipeline, tool loop, registry, terminal and response boundary under
test are all genuine production code end to end.
"""
from __future__ import annotations

import json
import unittest

from astra.ai.capability_context import (NO_TOOLS_MESSAGE,
                                         collect_runtime_capabilities)
from astra.ai.gateway import AstraAIGateway
from astra.ai.router import AstraRouter, RoutingRequest, RoutingResult
from astra.core.exceptions import ProviderError
from astra.store import Store
from tests.helpers import LocalRuntimeStub, ScriptedBrain, make_stack

# A synthetic, deliberately NON-real credential for the redaction test.
FAKE_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


def understand(final_request="", *, required=False, capability="", intent="",
               criteria=("the request was satisfied",), provider="groq",
               model="llama-fast", was_incomplete=False, extra=None):
    data = {"final_request": final_request, "was_incomplete": was_incomplete,
            "provider": provider, "model": model, "criteria": list(criteria),
            "reason": "best fit",
            "execution": {"required": required, "capability": capability,
                          "intent": intent}}
    if extra is not None:
        data["execution"] = extra
    return json.dumps(data)


def verdict(v="complete", missing=(), action="fix", instructions=""):
    return json.dumps({"verdict": v, "missing": list(missing),
                       "action": action, "instructions": instructions})


def tool_call(command, tool="runtime_command", **args):
    payload = {"command": command}
    payload.update(args)
    return json.dumps({"action": "tool", "tool": tool, "args": payload,
                       "thought": "run it"})


def final(answer):
    return json.dumps({"action": "final", "answer": answer})


class Harness:
    """The real bootstrap stack with the two network edges scripted."""

    def __init__(self, gateway_replies=(), brain_replies=(), *,
                 brain="provider"):
        self.stack = make_stack()
        self.pipeline = self.stack["chat_pipeline"]
        # The Agent's shell work runs in the isolated Agent Runtime. Point
        # the pipeline at a LOCAL runtime stub so these architecture tests
        # exercise the real loop/registry/runtime-tool code without booting
        # proot for every command; real isolation is covered by
        # tests/test_runtime.py.
        self.runtime = LocalRuntimeStub(events=self.stack["events"])
        self.pipeline.runtime = self.runtime
        # Pin the brain mode so the test is independent of the ambient .env.
        self.pipeline.agent_brain = brain
        self.registry = self.stack["registry"]
        self.router = self.stack["router"]
        self.gateway = self.pipeline.gateway
        self.rows = []
        events = self.stack["events"]
        original_emit = events.emit

        def emit(kind, agent="", **data):
            self.rows.append({"kind": kind, "agent": agent, "data": data})
            return original_emit(kind, agent=agent, **data)

        events.emit = emit

        self.gateway_calls = []
        self._gateway_replies = list(gateway_replies)

        def gateway_chat(messages, model=None, max_tokens=500, category=None,
                         trace=""):
            self.gateway_calls.append({"messages": messages,
                                       "category": category, "trace": trace})
            if not self._gateway_replies:
                return "{}"
            reply = self._gateway_replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        self.gateway.chat = gateway_chat
        self.gateway.is_usable = lambda: True

        self.brain = ScriptedBrain(list(brain_replies))
        self.route_requests = []

        def route_request(req):
            self.route_requests.append(req)
            return RoutingResult(
                ok=True, text=self.brain.chat(req.messages),
                provider=req.preferred_provider or "groq",
                model=req.preferred_model or "llama-fast")

        self.router.route_request = route_request

    # -- observation helpers -------------------------------------------------
    def kinds(self, prefix=""):
        return [r["kind"] for r in self.rows if r["kind"].startswith(prefix)]

    def understand_user_content(self):
        """The user-role content of the Gateway's UNDERSTAND call."""
        call = self.gateway_calls[0]
        return next(m["content"] for m in call["messages"]
                    if m["role"] == "user")

    def verify_user_content(self):
        call = self.gateway_calls[1]
        return next(m["content"] for m in call["messages"]
                    if m["role"] == "user")

    def provider_system_prompt(self):
        return self.route_requests[0].messages[0]["content"]

    def run(self, message, **kw):
        return self.pipeline.run(message, **kw)


class GatewayLiveCapabilityContextTests(unittest.TestCase):
    def test_understand_call_receives_live_capability_context(self):
        h = Harness([understand("Tomar ki ki tools available?"),
                     verdict("complete")], ["Ami Astra, ekhane help korte pari."])
        h.run("Tomar ki ki tools available?")
        content = h.understand_user_content()
        caps = collect_runtime_capabilities(h.registry)
        # human-facing block ...
        self.assertIn("Runtime capability catalog", content)
        self.assertIn(caps.human_context, content)
        # ... plus the exact machine-facing capability IDs ...
        self.assertIn(caps.catalog_text(), content)
        self.assertIn("terminal", caps.categories)
        # ... and never the exact tool-call catalog / raw tool names.
        self.assertNotIn("terminal_exec", content)
        self.assertNotIn('"action"', content)

    def test_gateway_and_provider_read_the_same_capability_source(self):
        h = Harness([understand("Tomar ki ki tools available?"),
                     verdict("complete")], ["Ami shob korte pari."])
        h.run("Tomar ki ki tools available?")
        caps = collect_runtime_capabilities(h.registry)
        self.assertIn(caps.human_context, h.understand_user_content())
        self.assertIn(caps.human_context, h.provider_system_prompt())

    def test_capability_context_is_derived_not_hardcoded(self):
        h = Harness([understand("Tomar ki ki tools available?"),
                     verdict("complete")], ["ok"])
        caps = collect_runtime_capabilities(h.registry)
        live = {t["category"] for t in h.registry.list()}
        self.assertEqual(set(caps.categories), live)
        self.assertTrue(caps.available)
        # Only registered categories are ever named — a category with no
        # tool on the live registry cannot appear.
        for ghost in ("telepathy", "quantum-compute", "time-travel"):
            self.assertNotIn(ghost, caps.catalog_text())
            self.assertNotIn(ghost, caps.human_context)
        self.assertNotEqual(caps.human_context, NO_TOOLS_MESSAGE)

    def test_terminal_capability_question_sees_terminal(self):
        h = Harness([understand("Tumi ki terminal use korte paro?"),
                     verdict("complete")], ["Hyam, ami terminal use korte pari."])
        h.run("Tumi ki terminal use korte paro?")
        self.assertIn("terminal/shell access", h.understand_user_content())
        self.assertIn("terminal", h.provider_system_prompt())

    def test_empty_tool_registry_is_reported_honestly_as_no_tools(self):
        caps = collect_runtime_capabilities(None)
        self.assertFalse(caps.available)
        self.assertEqual(caps.human_context, NO_TOOLS_MESSAGE)
        self.assertIn("none", caps.catalog_text())


class GatewayExecutionDecisionTests(unittest.TestCase):
    def test_terminal_required_task_produces_structured_handoff(self):
        h = Harness(
            [understand("Clone the Astra repository from GitHub.",
                        required=True, capability="terminal",
                        intent="clone the requested repository"),
             verdict("complete")],
            [tool_call("cd ~ && echo cloned"), final("Repo cloned.")])
        out = h.run("GitHub repo clone koro")
        self.assertTrue(out["data"]["execution"]["required"])
        self.assertEqual(out["data"]["execution"]["capability"], "terminal")
        # The decision reached the Provider's runtime context (survives
        # provider failover because it is part of the messages).
        sys_prompt = h.provider_system_prompt()
        self.assertIn("Gateway execution decision", sys_prompt)
        self.assertIn("Required capability: terminal", sys_prompt)
        # ... and the AgentToolLoop's task context.
        loop_task = h.brain.calls[0][-1]["content"]
        self.assertIn("Gateway execution decision", loop_task)

    def test_non_tool_task_is_not_marked_for_execution(self):
        h = Harness([understand("What is the capital of France?"),
                     verdict("complete")],
                    [final("Paris.")])
        out = h.run("What is the capital of France?")
        self.assertFalse(out["data"]["execution"]["required"])
        self.assertEqual(out["data"]["execution"]["capability"], "")
        # No execution requirement is injected into the Provider's runtime
        # context (the tool protocol itself still mentions the concept, so
        # the assertion is on the decision block, not that phrase).
        self.assertNotIn("Required capability:", h.provider_system_prompt())
        self.assertNotIn("Required capability:",
                         h.brain.calls[0][-1]["content"])
        self.assertEqual([k for k in h.kinds() if k == "agent.tool_call"], [])

    def test_capability_absent_from_the_runtime_is_never_demanded(self):
        # A hallucinated category the live registry does not have (the test
        # stays valid no matter which real tools happen to be configured).
        h = Harness(
            [understand("Do the thing.", extra={
                "required": True, "capability": "telepathy",
                "intent": "read the user's mind"}),
             verdict("complete")], ["ok"])
        out = h.run("Do the thing.")
        self.assertTrue(out["data"]["execution"]["required"])
        # required stays true (the user did ask for an action) but the
        # invented capability is dropped, never trusted or demanded.
        self.assertEqual(out["data"]["execution"]["capability"], "")

    def test_malformed_execution_block_degrades_to_no_execution(self):
        h = Harness([understand("Hello", extra="not-a-dict"),
                     verdict("complete")], ["Hi!"])
        out = h.run("Hello")
        self.assertFalse(out["data"]["execution"]["required"])


class RealToolExecutionTests(unittest.TestCase):
    def test_terminal_exec_runs_and_result_returns_to_the_provider(self):
        h = Harness(
            [understand("Clone the repo.", required=True,
                        capability="terminal", intent="clone the repo"),
             verdict("complete")],
            [tool_call("echo astra-clone-ok"),
             final("Cloned: astra-clone-ok")])
        out = h.run("GitHub repo clone koro")
        self.assertEqual(out["reply"], "Cloned: astra-clone-ok")
        self.assertEqual(out["data"]["verification"]["status"], "COMPLETE")
        # the REAL terminal ran the REAL command, and the REAL tool result
        # came back into the SAME provider conversation.
        self.assertIn("terminal.started", h.kinds())
        self.assertIn("terminal.completed", h.kinds())
        self.assertGreaterEqual(len(h.brain.calls), 2)
        self.assertIn("Tool result", h.brain.calls[1][-1]["content"])
        self.assertIn("astra-clone-ok", h.brain.calls[1][-1]["content"])

    def test_multi_step_execution_continues_until_final_answer(self):
        h = Harness(
            [understand("Do the two steps.", required=True,
                        capability="terminal", intent="run two commands"),
             verdict("complete")],
            [tool_call("echo step-one"), tool_call("echo step-two"),
             final("both steps done")])
        out = h.run("do two things")
        self.assertEqual(out["reply"], "both steps done")
        self.assertIn("step-one", h.brain.calls[1][-1]["content"])
        self.assertIn("step-two", h.brain.calls[2][-1]["content"])
        self.assertGreaterEqual(len(h.route_requests), 3)

    def test_tool_catalog_reaches_the_model_with_real_arg_schemas(self):
        h = Harness(
            [understand("Run it.", required=True, capability="terminal",
                        intent="run a command"),
             verdict("complete")],
            [final("nothing to do")])
        h.run("run it")
        system = h.brain.calls[0][0]["content"]
        self.assertIn("runtime_command", system)       # exact tool name
        self.assertIn("args: {", system)                # real arg schema
        self.assertIn("command:string", system)


class VerificationUsesExecutionEvidenceTests(unittest.TestCase):
    def test_verifier_sees_the_execution_decision_and_evidence(self):
        h = Harness(
            [understand("Clone it.", required=True, capability="terminal",
                        intent="clone the repo"),
             verdict("complete")],
            [tool_call("echo evidence-here"), final("Cloned.")])
        h.run("clone it")
        content = h.verify_user_content()
        self.assertIn("Gateway execution decision: required", content)
        self.assertIn("REQUIRES real tool execution", content)
        self.assertIn("evidence-here", content)

    def test_verification_does_not_block_a_required_tool(self):
        """The reported failure: the Provider answers with instructions
        ("run git clone yourself") instead of executing. The deterministic
        evidence gate rejects it, and the correction re-enters the tool loop
        so the required tool actually runs."""
        h = Harness(
            [understand("GitHub repo clone koro", required=True,
                        capability="terminal",
                        intent="clone the requested repository"),
             verdict("complete")],
            [final("You can clone it yourself by running:\n"
                   "  git clone <url>"),
             tool_call("echo actually-cloned"),
             final("Repo cloned successfully.")])
        out = h.run("GitHub repo clone koro")
        self.assertEqual(out["reply"], "Repo cloned successfully.")
        self.assertNotIn("git clone", out["reply"])
        self.assertIn("terminal.started", h.kinds())
        self.assertEqual(out["data"]["verification"]["status"], "COMPLETE")
        self.assertGreaterEqual(out["data"]["verification"]["attempts"], 1)
        # the correction round was a real tool loop, not a plain re-prompt
        self.assertIn("runtime_command", h.brain.calls[1][0]["content"])

    def test_execution_task_is_not_complete_without_real_evidence(self):
        h = Harness(
            [understand("Clone it.", required=True, capability="terminal",
                        intent="clone the repo"),
             verdict("complete")],
            [final("Here is how you can do it yourself."),
             final("Still just instructions."),
             final("And again.")])
        out = h.run("clone it")
        self.assertEqual(out["data"]["verification"]["status"], "INCOMPLETE")
        self.assertNotIn("terminal.started", h.kinds())
        # the internal caveat is recorded for the log, never shown to the user
        self.assertIn("internal_note", out["data"])
        for leak in ("⚠️", "Gateway verification", "Missing:"):
            self.assertNotIn(leak, out["reply"])


class FailoverTests(unittest.TestCase):
    """A fallback provider must receive the SAME execution requirement and
    the SAME tools — a failover must never turn an execution task into
    "tools are unavailable"."""

    class _RecordingProvider:
        def __init__(self, name, models, *, fail=False):
            self.name = name
            self.models = models
            self.pool = True
            self.fail = fail
            self.messages_seen = []

        def health_check(self):
            return True

        def chat(self, messages, model=None, max_tokens=500,
                 response_format=None):
            self.messages_seen.append(messages)
            if self.fail:
                raise ProviderError("HTTP 429 rate limit reached")
            return final("served after failover")

    def test_execution_intent_and_tool_catalog_survive_provider_failover(self):
        from astra.ai.agent_tool_loop import TOOL_PROTOCOL, build_tool_catalog
        from astra.core.permissions import Policy
        from astra.runtime.tools import register_runtime_tools
        from astra.tools.registry import ToolRegistry
        registry = ToolRegistry(policy=Policy(granted=["system_action"]))
        runtime = LocalRuntimeStub()
        register_runtime_tools(registry, runtime)
        self.addCleanup(runtime.close_all)
        catalog = build_tool_catalog(registry)
        bad = self._RecordingProvider("gemini", ["model-a"], fail=True)
        good = self._RecordingProvider("groq", ["model-b"])
        router = AstraRouter(providers=[bad, good],
                             gateway=AstraAIGateway(connections=[],
                                                    store=Store(":memory:")))
        messages = [
            {"role": "system", "content":
                "You are Astra.\n\nGateway execution decision "
                "(authoritative — this request REQUIRES real tool execution "
                "before you answer):\n- Required capability: terminal "
                "(available in this runtime).\n\n"
                + TOOL_PROTOCOL.replace("{catalog}", catalog)},
            {"role": "user", "content": "clone the repo"},
        ]
        rr = router.route_request(RoutingRequest(task_type="simple_chat",
                                                 messages=messages))
        self.assertTrue(rr.ok, rr.error)
        self.assertTrue(good.messages_seen)
        seen = json.dumps(good.messages_seen[0])
        self.assertIn("Gateway execution decision", seen)
        self.assertIn("Required capability: terminal", seen)
        self.assertIn("runtime_command", seen)


class BrainModeTests(unittest.TestCase):
    def test_provider_brain_executes_through_the_tool_loop(self):
        h = Harness(
            [understand("Run it.", required=True, capability="terminal",
                        intent="run a command"),
             verdict("complete")],
            [tool_call("echo provider-brain"), final("ran via provider brain")],
            brain="provider")
        out = h.run("run it")
        self.assertEqual(out["reply"], "ran via provider brain")
        self.assertIn("terminal.started", h.kinds())

    def test_gateway_brain_executes_through_the_same_tool_loop(self):
        h = Harness(
            [understand("Run it.", required=True, capability="terminal",
                        intent="run a command"),
             tool_call("echo gateway-brain"), final("ran via gateway brain"),
             verdict("complete")],
            [], brain="gateway")
        out = h.run("run it")
        self.assertEqual(out["reply"], "ran via gateway brain")
        self.assertIn("terminal.started", h.kinds())
        # the Gateway drove the loop with the SAME tool protocol catalog
        loop_system = next(m["content"] for m in h.gateway_calls[1]["messages"]
                           if m["role"] == "system")
        self.assertIn("runtime_command", loop_system)
        self.assertEqual(h.gateway_calls[1]["category"], "tool_use")


class AdapterPayloadTests(unittest.TestCase):
    """Requirement: do not assume building a Python string means the model
    receives it. Trace the final `messages` payload all the way through the
    REAL router into the REAL provider adapter's HTTP body."""

    def test_tool_loop_payload_reaches_the_adapter_verbatim(self):
        from astra.ai.adapters.base import CompatibleAdapter
        from astra.ai.agent_tool_loop import (AgentToolLoop,
                                              ProviderToolCaller)
        from astra.ai.router import AstraRouter
        from astra.core.permissions import Policy
        from astra.runtime.tools import register_runtime_tools
        from astra.tools.registry import ToolRegistry

        class _CaptureAdapter(CompatibleAdapter):
            name = "capture"
            models = ["capture-1"]
            base_url = "http://capture.invalid/v1"

            def __init__(self, replies):
                super().__init__(config=None)
                # no credential pool: the router must not skip this adapter
                # for having zero configured keys (credential handling is
                # stubbed out below).
                self.pool = None
                self.bodies = []
                self._replies = list(replies)

            def health_check(self):
                return True

            def _pick(self, model=None):
                return "cred"

            def _done(self, cred=None, errored=False, reason="", **kw):
                return None

            def _post(self, url, body, cred):
                self.bodies.append(body)
                text = (self._replies.pop(0) if self._replies
                        else json.dumps({"action": "final", "answer": "done"}))
                return {"choices": [{"message": {"content": text}}],
                        "usage": {}}

        registry = ToolRegistry(policy=Policy(granted=["system_action"]))
        runtime = LocalRuntimeStub()
        register_runtime_tools(registry, runtime)
        self.addCleanup(runtime.close_all)

        adapter = _CaptureAdapter([tool_call("echo adapter-trace"),
                                   final("adapter done")])
        router = AstraRouter(providers=[adapter])
        system_prompt = (
            "You are Astra.\n\nRuntime capability catalog (authoritative): "
            "- terminal/shell access\n\nGateway execution decision "
            "(authoritative — this request REQUIRES real tool execution "
            "before you answer):\n- Required capability: terminal "
            "(available in this runtime).\n- Actually perform the action.")
        loop = AgentToolLoop(registry, runtime=runtime)
        res = loop.run("clone the repo", ProviderToolCaller(
            router, task_type="simple_chat"), system_prompt=system_prompt,
            history=[{"role": "user", "content": "earlier turn"}])
        self.assertTrue(res.ok)

        self.assertGreaterEqual(len(adapter.bodies), 2)
        first = adapter.bodies[0]["messages"]
        system = next(m["content"] for m in first if m["role"] == "system")
        # capability context + execution decision + the exact tool catalog
        # (names + argument schemas) + the tool protocol all reached the wire
        self.assertIn("Runtime capability catalog", system)
        self.assertIn("Gateway execution decision", system)
        self.assertIn("Required capability: terminal", system)
        self.assertIn("runtime_command", system)
        self.assertIn("command:string", system)
        # conversation history is intact in the actual request body
        self.assertIn({"role": "user", "content": "earlier turn"}, first)
        # the second real call carries the tool result back to the same model
        second = adapter.bodies[1]["messages"]
        self.assertTrue(any(m["role"] == "user" and "Tool result" in str(m["content"])
                            for m in second))
        self.assertTrue(any("adapter-trace" in str(m.get("content"))
                            for m in second))


class ResponseBoundaryTests(unittest.TestCase):
    def test_raw_tool_protocol_json_never_reaches_the_user(self):
        h = Harness(
            [understand("Run it.", required=True, capability="terminal",
                        intent="run a command"),
             verdict("complete")],
            [tool_call("echo boundary"), final("The command ran.")])
        out = h.run("run it")
        for leak in ('"action": "tool"', '"session_id"', '"thought"',
                     "terminal_exec", "{catalog}"):
            self.assertNotIn(leak, out["reply"])

    def test_a_model_reply_that_leaks_thought_and_args_is_not_shown(self):
        leaky = ('Sure, doing it now.\n'
                 '{"action": "tool", "tool": "terminal_exec", '
                 '"args": {"command": "echo x"}, "thought": "secret plan", '
                 '"session_id": "conv-1"}')
        h = Harness(
            [understand("Run it.", required=True, capability="terminal",
                        intent="run a command"),
             verdict("complete")],
            [leaky, final("Done.")])
        out = h.run("run it")
        self.assertEqual(out["reply"], "Done.")
        self.assertNotIn("secret plan", out["reply"])

    def test_credential_shaped_output_is_redacted_everywhere(self):
        h = Harness(
            [understand(f"Clone using {FAKE_TOKEN}", required=True,
                        capability="terminal", intent="clone with a token"),
             verdict("complete")],
            [tool_call(f"echo {FAKE_TOKEN}"), final(f"Used token {FAKE_TOKEN}")])
        out = h.run(f"clone with {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, out["reply"])
        self.assertIn("***redacted***", out["reply"])
        # the tool result fed back into the model conversation is redacted too
        seen = json.dumps(h.brain.calls[1][-1]["content"])
        self.assertNotIn(FAKE_TOKEN, seen)


class ConversationHistoryTests(unittest.TestCase):
    def test_history_turns_reach_both_gateway_and_provider_intact(self):
        h = Harness(
            [understand("What is my name?", required=False),
             verdict("complete")],
            [final("Your name is Rahim.")])
        history = [{"role": "user", "content": "amar nam Rahim"},
                   {"role": "assistant", "content": "Bujhlam, Rahim."}]
        out = h.run("amar nam ki?", history=history)
        self.assertTrue(out["ok"])
        understand_content = h.understand_user_content()
        self.assertIn("Recent conversation", understand_content.replace(
            "Prior conversation", "Recent conversation"))
        self.assertIn("Rahim", understand_content)
        provider_messages = h.route_requests[0].messages
        self.assertIn({"role": "user", "content": "amar nam Rahim"},
                      provider_messages)


if __name__ == "__main__":
    unittest.main()
