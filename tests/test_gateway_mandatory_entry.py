"""Mandatory-Gateway-entry integration tests.

These prove, end-to-end and at the real HTTP layer, that:

  1. A normal chat message (unclaimed by any plugin) cannot reach an AI
     model without first passing through Astra AI Gateway Request
     Intelligence and the Task Completion Contract / supervision loop.
  2. BOTH normal-chat entry points — `/api/chat` (Agent.handle ->
     Orchestrator.submit) and `/api/agents` (Orchestrator.submit directly)
     — funnel into the same Gateway-guarded Planner path. Neither can
     bypass it.
  3. The two legacy bypasses removed in this pass (Agent's raw `llm`
     callable, and the dead `astra/llm.py` direct-Anthropic-API module)
     stay removed — this is a regression lock, not just a point-in-time
     audit note.
  4. Existing-Provider execution failures are kept separate from content
     verification/correction (§7 of the Gateway spec), and a genuinely
     incomplete result gets a real, bounded correction round-trip before
     the final answer reaches the user — exercised here through the full
     Agent -> Orchestrator -> Planner -> AstraRouter -> Gateway chain,
     not just at the Planner-unit level (see
     tests/test_planner_task_completion_integration.py for that).

Nothing here mocks AstraRouter, AstraAIGateway, GatewayRequestIntelligence,
or the task-completion supervisor themselves — only the Provider adapter is
a scripted fake and, where noted, the Gateway's own request-intelligence
connection is a spy (never a mock of `GatewayRequestIntelligence` itself).
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from astra.agent import Agent
from astra.ai.gateway import AstraAIGateway, GatewayRequestIntelligence
from astra.ai.gateway_task_completion import COMPLETE, INCOMPLETE
from astra.ai.router import AstraRouter
from astra.core.correction import MAX_CORRECTION_ATTEMPTS
from astra.core.exceptions import ProviderError
from astra.core.executor import Executor
from astra.core.orchestrator import Orchestrator
from astra.core.planner import Planner
from astra.store import Store
from astra.web import AstraServer


class _ShimPool:
    def __bool__(self):
        return True


class SequencedProvider:
    """Fake Existing-Provider adapter: scripted replies, real call
    recording — same fixture shape as
    tests/test_planner_task_completion_integration.py."""

    def __init__(self, name, models, replies):
        self.name = name
        self.models = models
        self.pool = _ShimPool()
        self.calls: list[list] = []
        self._replies = list(replies)

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls.append(list(messages))
        if not self._replies:
            raise ProviderError(f"{self.name}: no more scripted replies")
        return self._replies.pop(0)


class RecordingGatewayIntelligence(GatewayRequestIntelligence):
    """Real `GatewayRequestIntelligence` subclass (never a mock of the
    class itself) that records every `process()` call so tests can assert
    Gateway Request Intelligence actually ran, while behaving exactly like
    the real thing (pass-through, since no Gateway connections are
    configured here)."""

    def __init__(self):
        super().__init__(gateway=None)   # unusable -> real pass-through path
        self.calls: list[str] = []

    def process(self, raw_text: str, *, max_tokens: int = 400, context: str = ""):
        self.calls.append(raw_text)
        return super().process(raw_text, max_tokens=max_tokens, context=context)


def _build_stack(replies, store=None):
    """Real Orchestrator/Planner/AstraRouter/AstraAIGateway wired together
    on a scripted Existing-Provider adapter, plus a spy on Gateway Request
    Intelligence — everything a normal `/api/chat` or `/api/agents`
    request would actually run through in production."""
    store = store or Store(":memory:")
    provider = SequencedProvider("gemini", ["model-a"], replies)
    gw = AstraAIGateway(connections=[], store=store)   # no GW_* connections
    router = AstraRouter(providers=[provider], store=store, gateway=gw)
    gateway_intelligence = RecordingGatewayIntelligence()
    planner = Planner(router=router, tools=["answer"],
                      gateway_intelligence=gateway_intelligence)
    executor = Executor(registry=None)
    orch = Orchestrator(store, planner=planner, executor=executor, router=router)
    agent = Agent([], orchestrator=orch)   # no plugins -> everything is "normal AI"
    return agent, orch, provider, gateway_intelligence, router


# ── 1/2: both normal-chat entry points reach Gateway Request Intelligence ──
class TestBothEntryPointsReachGateway(unittest.TestCase):
    def test_agent_handle_runs_gateway_request_intelligence(self):
        """`/api/chat`'s path: Agent.handle() -> Orchestrator.submit().

        A pure "answer" step intentionally reports `ok: False` at the
        Agent-reply layer (Agent._reply_from_report's long-standing
        "gentle unknown-fallback contract" for plain conversational
        replies, unrelated to Gateway completion status) — the property
        this test actually cares about is that Gateway Request
        Intelligence ran and the Existing Provider executed, which is
        checked directly below rather than through `reply["ok"]`.
        """
        agent, orch, provider, gi, router = _build_stack(
            ['{"steps":[{"id":"s1","tool":"answer",'
             '"params":{"text":"hello there"},"description":"reply"}]}'])
        reply = agent.handle("say hello")
        self.assertEqual(reply["reply"], "hello there")
        self.assertEqual(orch.planner.last_completion_status, COMPLETE)
        self.assertEqual(gi.calls, ["say hello"])   # Gateway RI actually ran
        self.assertEqual(len(provider.calls), 1)    # Existing Provider executed

    def test_orchestrator_submit_directly_runs_gateway_request_intelligence(self):
        """`/api/agents`'s path: Orchestrator.submit() with no Agent/plugin
        layer at all — proves the Gateway guard lives in Planner, not in
        Agent, so neither entry point can route around it."""
        agent, orch, provider, gi, router = _build_stack(
            ['{"steps":[{"id":"s1","tool":"answer",'
             '"params":{"text":"hello there"},"description":"reply"}]}'])
        rec = orch.submit("say hello", sync=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(gi.calls, ["say hello"])
        self.assertEqual(len(provider.calls), 1)

    def test_http_chat_and_agents_endpoints_both_reach_gateway(self):
        """Black-box HTTP test: boot the real server, hit both endpoints,
        and confirm Gateway Request Intelligence ran for each — the
        strongest proof that neither route can bypass the Gateway."""
        agent, orch, provider, gi, router = _build_stack([
            '{"steps":[{"id":"s1","tool":"answer",'
            '"params":{"text":"hi from chat"},"description":"reply"}]}',
            '{"steps":[{"id":"s1","tool":"answer",'
            '"params":{"text":"hi from agents"},"description":"reply"}]}',
        ])
        store = orch.store
        server = AstraServer(("127.0.0.1", 0), store, agent, agent.plugins,
                             stack={"orchestrator": orch, "router": router})
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"

            def post(path, body):
                data = json.dumps(body).encode()
                req = urllib.request.Request(
                    base + path, data=data, method="POST",
                    headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req) as resp:
                        return resp.status, json.loads(resp.read().decode())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read().decode())

            s1, b1 = post("/api/chat", {"message": "first request"})
            self.assertEqual(s1, 200)
            self.assertEqual(b1["data"]["reply"], "hi from chat")

            s2, b2 = post("/api/agents", {"goal": "second request", "sync": True})
            self.assertEqual(s2, 201)
            self.assertEqual(b2["data"]["status"], "COMPLETED")

            # Gateway Request Intelligence ran for BOTH, in order — neither
            # endpoint reached the Existing Provider without it.
            self.assertEqual(gi.calls, ["first request", "second request"])
            self.assertEqual(len(provider.calls), 2)
        finally:
            server.shutdown()
            server.server_close()


# ── 3: the two removed bypasses stay removed (regression lock) ────────────
class TestLegacyBypassesStayRemoved(unittest.TestCase):
    def test_agent_no_longer_accepts_a_raw_llm_callable(self):
        """A prior version accepted `Agent(plugins, llm=callable)` and
        would call it directly on orchestrator failure/absence — a normal
        chat message could reach a model with zero Gateway involvement.
        That parameter must not exist."""
        with self.assertRaises(TypeError):
            Agent([], llm=lambda msg: "bypassed")   # noqa: ARG005

    def test_agent_without_orchestrator_never_calls_a_model(self):
        """With no orchestrator at all, Agent must degrade to the local,
        non-AI fallback text — never silently reach for a model."""
        agent = Agent([])   # no plugins, no orchestrator
        reply = agent.handle("anything at all")
        self.assertFalse(reply["ok"])
        self.assertIn("Ei command ta ami bojhini", reply["reply"])

    def test_agent_orchestrator_failure_falls_back_without_a_model_call(self):
        class _ExplodingOrchestrator:
            def submit(self, *a, **k):
                raise RuntimeError("boom")

        agent = Agent([], orchestrator=_ExplodingOrchestrator())
        reply = agent.handle("anything at all")
        self.assertFalse(reply["ok"])
        self.assertIn("Ei command ta ami bojhini", reply["reply"])

    def test_direct_anthropic_bypass_module_is_gone(self):
        """`astra/llm.py` used to call the Anthropic API directly over raw
        HTTP — a legacy path that bypassed both the Gateway and the
        Existing Provider System entirely. It was unused (imported
        nowhere) and has been deleted; this must stay true."""
        with self.assertRaises(ModuleNotFoundError):
            import astra.llm  # noqa: F401


# ── 4: verification/correction + failure-vs-correction separation, exercised
#      through the FULL chain (Agent -> Orchestrator -> Planner -> Router ->
#      Gateway), not just at the Planner-unit level ─────────────────────────
class TestFullChainVerificationAndCorrection(unittest.TestCase):
    def test_incomplete_result_is_corrected_before_reaching_the_user(self):
        agent, orch, provider, gi, router = _build_stack([
            "not json at all",   # first attempt: INCOMPLETE
            '{"steps":[{"id":"s1","tool":"answer",'
            '"params":{"text":"corrected answer"},"description":"reply"}]}',
        ])
        reply = agent.handle("say hello")
        self.assertEqual(len(provider.calls), 2)              # real correction ran
        self.assertEqual(orch.planner.last_completion_status, COMPLETE)
        # the corrected text, not the first bad attempt, reaches the user
        self.assertEqual(reply["reply"], "corrected answer")

    def test_correction_is_bounded_and_never_falsely_reports_complete(self):
        agent, orch, provider, gi, router = _build_stack(
            ["still not json"] * 10)
        reply = agent.handle("say hello")
        self.assertEqual(len(provider.calls), 1 + MAX_CORRECTION_ATTEMPTS)
        self.assertEqual(orch.planner.last_completion_status, INCOMPLETE)
        # graceful fallback reaches the user; nothing here claims a plan
        # that was never actually verified COMPLETE.
        self.assertFalse(reply["ok"])

    def test_provider_execution_failure_is_not_treated_as_correctable(self):
        """§7: a genuine Existing-Provider failure (not an incomplete
        result) must not be fed into the Gateway's content-correction
        loop — that stays the Existing Provider System's own
        recovery/retry path's job. AstraRouter's own retry (up to
        `max_retries`) legitimately re-attempts the SAME original prompt
        on transient failure; what must NOT happen is the Gateway's
        correction instruction ("Required Next Action: ...") ever being
        sent, since there was no result to correct — only a failure to
        recover from."""
        agent, orch, provider, gi, router = _build_stack([])  # errors immediately
        reply = agent.handle("say hello")
        self.assertFalse(reply["ok"])
        self.assertGreaterEqual(len(provider.calls), 1)
        for call in provider.calls:
            joined = json.dumps(call)
            self.assertNotIn("Required Next Action", joined)
        self.assertEqual(orch.planner.last_completion_status, "")


if __name__ == "__main__":
    unittest.main()
