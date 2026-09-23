"""Real-stack integration tests for the capability-question fix.

Every other capability test in this repo (`test_capability_context.py`,
`test_system_prompt.py`) either calls `build_capability_context` directly or
drives `ChatPipeline`/`AgentToolLoop` with a hand-built `ToolRegistry` and a
`FakeGateway`/`FakeRouter`. Those are valuable unit tests, but none of them
proves the thing a "still failing in the real UI/runtime" report actually
needs proven: that `astra.bootstrap.build()` — the SAME assembly function
`run.py` and `web_fastapi.py` use to boot the real app — produces a
`ChatPipeline` whose `self.registry` is the identical object bootstrap
registered tools on, and that a real capability question sent through that
real pipeline puts the real, live registry categories in front of the model.

These tests build the full stack with `tests.helpers.make_stack()` (an
in-memory Store, real `ToolRegistry` + `TerminalManager` + browser/web3 tools,
real `AstraRouter`/`AstraAIGateway` objects). No network credentials are
configured in the test environment, so the Gateway is correctly reported
`is_usable() == False` and the "understand"/"verify" Gateway calls are
skipped by `ChatPipeline` itself (see `_gateway_usable`) — exactly the
documented fail-open behavior, not a test hack. The one call that would hit
the network — `AstraRouter.route_request` — is monkeypatched on the REAL
router instance to capture the exact messages it was given and return a
scripted reply, so the rest of the stack (registry, terminal, tool loop,
capability_context, system_prompt composition) is the genuine production
code path end to end.
"""
import json
import unittest

from astra.ai.agent_tool_loop import build_tool_catalog
from astra.ai.capability_context import NO_TOOLS_MESSAGE, build_capability_context
from astra.ai.router import RoutingResult
from astra.ai.system_prompt import ASTRA_CORE_SYSTEM_PROMPT
from tests.helpers import make_stack

CORE = ASTRA_CORE_SYSTEM_PROMPT.strip()


def _final(answer: str) -> str:
    return json.dumps({"action": "final", "answer": answer})


def _capture_route_request(router):
    """Monkeypatch the REAL router's `route_request` (the only call in this
    configuration that would otherwise reach the network) so it records
    every request it is given and answers with a scripted final-answer
    tool-protocol reply. Returns the list `route_request` appends to."""
    calls = []
    original = router.route_request

    def fake_route_request(req):
        calls.append(req)
        return RoutingResult(ok=True, text=_final("scripted reply"),
                             provider=req.preferred_provider or "test-provider",
                             model=req.preferred_model or "test-model")

    router.route_request = fake_route_request
    return calls, original


def _system_message(messages):
    return next(m["content"] for m in messages if m["role"] == "system")


class RegistryIdentityAcrossRealStackTests(unittest.TestCase):
    """Requirement: prove capability_context and AgentToolLoop (via
    ChatPipeline) read the SAME live ToolRegistry object bootstrap built —
    not a copy, not a second registry, not None."""

    def test_bootstrap_registry_and_pipeline_registry_are_the_same_object(self):
        stack = make_stack()
        registry = stack["registry"]
        pipeline = stack["chat_pipeline"]
        self.assertIsNotNone(registry)
        self.assertIs(pipeline.registry, registry,
                      "ChatPipeline.registry must be IDENTICAL to the "
                      "ToolRegistry bootstrap.build() registered tools on — "
                      "a copy or a second instance would silently desync.")

    def test_pipeline_registry_is_the_one_agent_tool_loop_will_use(self):
        """AgentToolLoop is constructed fresh per turn from
        `self.registry` inside `ChatPipeline._run_tool_loop` — confirm the
        exact object identity chain a real chat turn relies on."""
        stack = make_stack()
        pipeline = stack["chat_pipeline"]
        from astra.ai.agent_tool_loop import AgentToolLoop
        loop = AgentToolLoop(pipeline.registry, terminal=pipeline.terminal)
        self.assertIs(loop.registry, stack["registry"])

    def test_capability_context_reads_the_same_registry_bootstrap_populated(self):
        stack = make_stack()
        ctx_from_bootstrap_registry = build_capability_context(stack["registry"])
        ctx_from_pipeline_registry = build_capability_context(
            stack["chat_pipeline"].registry)
        self.assertEqual(ctx_from_bootstrap_registry, ctx_from_pipeline_registry)
        self.assertNotEqual(ctx_from_bootstrap_registry, NO_TOOLS_MESSAGE,
                            "bootstrap.build() always registers terminal/"
                            "browser/file/web3 tools — the real registry "
                            "must never fall back to the empty-registry "
                            "message.")


class RealBootstrapCapabilityCatalogTests(unittest.TestCase):
    """Requirement 16/17: send the real failing user messages through the
    REAL bootstrap stack and assert the generated model input contains the
    actual registered capability categories."""

    def test_tomar_ki_ki_tools_available_reaches_model_with_real_categories(self):
        stack = make_stack()
        pipeline = stack["chat_pipeline"]
        calls, _ = _capture_route_request(stack["router"])

        out = pipeline.run("Tomar ki ki tools available?")

        self.assertTrue(out["ok"])
        self.assertTrue(calls, "the provider was never called")
        sys_msg = _system_message(calls[0].messages)
        # The Core prompt is present exactly once (no duplication) ...
        self.assertEqual(sys_msg.count(CORE), 1)
        # ... together with the LIVE runtime capability catalog, not a
        # hardcoded or missing one.
        self.assertIn("Runtime capability catalog", sys_msg)
        for category_phrase in ("terminal/shell access", "file access",
                                "web browsing", "web3/blockchain tools"):
            self.assertIn(category_phrase, sys_msg)
        # And it must match exactly what the live registry reports right now
        # — the model input is never allowed to drift from the registry.
        self.assertIn(build_capability_context(stack["registry"]), sys_msg)

    def test_tumi_ki_terminal_use_korte_paro_sees_terminal_capability(self):
        stack = make_stack()
        pipeline = stack["chat_pipeline"]
        calls, _ = _capture_route_request(stack["router"])

        out = pipeline.run("Tumi ki terminal use korte paro?")

        self.assertTrue(out["ok"])
        sys_msg = _system_message(calls[0].messages)
        self.assertIn("terminal/shell access", sys_msg)
        # And the internal tool-call catalog handed to the model for actually
        # invoking a tool is built from the identical registry (never a
        # separate/stale one).
        internal_catalog = build_tool_catalog(stack["registry"])
        # The Agent's shell surface is the isolated runtime; the legacy HOST
        # terminal tools are never advertised to a model that drives Agent
        # execution (and are blocked at execution — see
        # tests/test_host_terminal_block.py).
        self.assertIn("runtime_command", internal_catalog)
        self.assertNotIn("terminal_exec", internal_catalog)

    def test_final_reply_never_leaks_the_runtime_catalog_or_protocol(self):
        stack = make_stack()
        pipeline = stack["chat_pipeline"]
        _capture_route_request(stack["router"])
        out = pipeline.run("Tomar ki ki tools available?")
        for leaked in ("Runtime capability catalog", "TOOL_PROTOCOL",
                      '"action": "tool"', "terminal_exec", "{catalog}", CORE):
            self.assertNotIn(leaked, out["reply"])


class EmptyRegistryFallbackOnlyWhenGenuinelyEmptyTests(unittest.TestCase):
    """Requirement 18: the 'no external tools' fallback must appear only
    when the registry is genuinely empty — never for the real, populated
    bootstrap registry — and must still appear honestly for a pipeline that
    truly has no registry wired (e.g. a minimal/legacy embedder)."""

    def test_real_bootstrap_stack_never_falls_back_to_no_tools(self):
        stack = make_stack()
        calls, _ = _capture_route_request(stack["router"])
        stack["chat_pipeline"].run("Tomar ki ki tools available?")
        sys_msg = _system_message(calls[0].messages)
        self.assertNotIn("no external tools are currently available", sys_msg)

    def test_pipeline_with_no_registry_wired_gets_honest_no_tools_fallback(self):
        stack = make_stack()
        pipeline = stack["chat_pipeline"]
        # Simulate a caller that builds ChatPipeline without ever wiring a
        # registry (e.g. an embedder that only wants plain chat) — this must
        # degrade to the plain provider route with an honest fallback, never
        # silently claim tools that were never registered.
        pipeline.registry = None
        calls, _ = _capture_route_request(stack["router"])
        pipeline.run("Tomar ki ki tools available?")
        sys_msg = _system_message(calls[0].messages)
        self.assertIn("no external tools are currently available", sys_msg)

    def test_no_tools_fallback_never_fires_just_because_registry_is_slow_or_odd(self):
        """A registry that legitimately has zero tools registered (but is
        not None and does not raise) must also get the honest fallback —
        proving the fallback is driven by actual content, not by a type
        check that could accidentally match the real registry too."""
        from astra.core.permissions import Policy
        from astra.tools.registry import ToolRegistry
        empty_registry = ToolRegistry(policy=Policy(granted=[]))
        self.assertEqual(build_capability_context(empty_registry),
                         NO_TOOLS_MESSAGE)


if __name__ == "__main__":
    unittest.main()
