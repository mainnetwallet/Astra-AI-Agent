"""Real agent-loop integration: Planner (and, end-to-end, Orchestrator)
now supplies a Task Completion Contract into RoutingRequest, and the
Gateway's completion supervisor actually runs against a real AstraRouter
and a real (fake-adapter) Provider — never a mock of the Gateway/Router
internals themselves.

Runtime chain exercised here:

    User goal
      -> Orchestrator.run() / Planner.plan()
      -> Planner._ai_steps(): Gateway request-intelligence (no-op when
         absent) -> Task Completion Contract -> AstraRouter.route_request()
      -> AstraRouter: candidate ranking/failover (unchanged) -> real
         Provider adapter .chat() -> Gateway.supervise_task() (verify /
         correct / re-verify, bounded)
      -> Planner parses the (possibly corrected) JSON plan
      -> Orchestrator executes the resulting steps (unchanged)

Nothing here mocks AstraRouter, AstraAIGateway, or the task-completion
supervisor — only the Provider adapter is a scripted fake, exactly like
tests/test_gateway_task_completion.py and
tests/test_gateway_result_supervision.py already do for the router layer.
"""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_task_completion import COMPLETE, INCOMPLETE
from astra.ai.router import AstraRouter
from astra.core.correction import MAX_CORRECTION_ATTEMPTS
from astra.core.exceptions import ProviderError
from astra.core.executor import Executor
from astra.core.orchestrator import Orchestrator
from astra.core.planner import Planner
from astra.store import Store


class _ShimPool:
    def __bool__(self):
        return True


class SequencedProvider:
    """Fake Provider adapter: scripted replies, real call recording — the
    same fixture shape used by test_gateway_task_completion.py, so a
    correction round-trip that only exists in a mock would fail these
    tests too."""

    def __init__(self, name, models, replies):
        self.name = name
        self.models = models
        self.pool = _ShimPool()
        self.calls: list[list] = []
        self._replies = list(replies)

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500):
        self.calls.append(list(messages))
        if not self._replies:
            raise ProviderError(f"{self.name}: no more scripted replies")
        return self._replies.pop(0)


def _router(provider, store=None):
    store = store or Store(":memory:")
    gw = AstraAIGateway(connections=[], store=store)
    return AstraRouter(providers=[provider], gateway=gw)


# ── Planner: real router.route_request() actually receives task_contract ──
class TestPlannerSuppliesTaskContract(unittest.TestCase):
    def test_valid_plan_first_try_no_correction_needed(self):
        """Simple-request optimization: a well-formed plan on the first
        try never triggers a correction round-trip."""
        provider = SequencedProvider(
            "gemini", ["model-a"],
            ['{"steps":[{"id":"s1","tool":"answer","params":{"text":"hi"},'
             '"description":"reply"}]}'])
        router = _router(provider)
        planner = Planner(router=router, tools=["answer"])
        steps = planner.plan("say hi")
        self.assertEqual(len(provider.calls), 1)   # no correction
        self.assertEqual(planner.last_completion_status, COMPLETE)
        self.assertEqual(steps[0]["tool"], "answer")

    def test_malformed_json_triggers_real_correction_round_trip(self):
        """Incomplete task -> correction -> corrected task -> COMPLETE:
        the FIRST reply is not valid JSON (would previously just have been
        silently discarded as `None` -> a generic fallback step); the
        Gateway's Task Completion supervisor now sends a real correction
        back through the same provider/model, and the SECOND (valid)
        reply is what actually gets parsed into steps."""
        provider = SequencedProvider(
            "gemini", ["model-a"],
            ["not json at all",
             '{"steps":[{"id":"s1","tool":"answer","params":{"text":"hi"},'
             '"description":"reply"}]}'])
        router = _router(provider)
        planner = Planner(router=router, tools=["answer"])
        steps = planner.plan("say hi")
        self.assertEqual(len(provider.calls), 2)   # real correction reached
        self.assertIn("Missing", provider.calls[1][-1]["content"] +
                      provider.calls[1][-1]["content"])  # sanity: has content
        self.assertEqual(planner.last_completion_status, COMPLETE)
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertNotEqual(steps[0]["description"], "Direct reply (no Provider configured)")

    def test_missing_steps_field_triggers_correction(self):
        """Required field ('steps') absent -> INCOMPLETE -> a real
        correction turn is sent, naming exactly what's missing."""
        provider = SequencedProvider(
            "gemini", ["model-a"],
            ['{"plan":[]}',
             '{"steps":[{"id":"s1","tool":"answer","params":{"text":"hi"},'
             '"description":"reply"}]}'])
        router = _router(provider)
        planner = Planner(router=router, tools=["answer"])
        planner.plan("say hi")
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("steps", provider.calls[1][-1]["content"])

    def test_bounded_correction_falls_back_to_answer_step(self):
        """Provider failure / persistent bad output -> existing recovery:
        after MAX_CORRECTION_ATTEMPTS the Gateway gives up (never silently
        claims COMPLETE), Planner gets no usable plan, and falls back to
        its existing graceful 'answer' step — never a crash, never a
        falsely-COMPLETE plan."""
        provider = SequencedProvider(
            "gemini", ["model-a"], ["still not json"] * 10)
        router = _router(provider)
        planner = Planner(router=router, tools=["answer"])
        steps = planner.plan("say hi")
        self.assertEqual(len(provider.calls), 1 + MAX_CORRECTION_ATTEMPTS)
        self.assertEqual(planner.last_completion_status, INCOMPLETE)
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertTrue(steps[0].get("is_answer"))   # the graceful fallback

    def test_provider_execution_failure_uses_existing_recovery_not_correction(self):
        """A genuine provider failure (not an incomplete result) must not
        be treated as something to correct — that's the existing
        recovery/failover path's job. Here there is only one provider and
        no fallback target, so planning fails over to the graceful
        fallback step rather than crashing."""
        provider = SequencedProvider("gemini", ["model-a"], [])  # errors immediately
        router = _router(provider)
        planner = Planner(router=router, tools=["answer"])
        steps = planner.plan("say hi")
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertTrue(steps[0].get("is_answer"))

    def test_router_without_route_request_still_works(self):
        """A duck-typed router that only implements the legacy `.route()`
        tuple interface (no Task Completion Contract support) keeps
        working exactly as before — the contract is additive, never a
        hard requirement to plan at all."""
        class _LegacyRouter:
            def __init__(self):
                self.calls = 0

            def route(self, messages):
                self.calls += 1
                return ("gemini", "model-a",
                        '{"steps":[{"id":"s1","tool":"answer",'
                        '"params":{"text":"hi"},"description":"d"}]}')

        legacy = _LegacyRouter()
        planner = Planner(router=legacy, tools=["answer"])
        steps = planner.plan("say hi")
        self.assertEqual(legacy.calls, 1)
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertEqual(planner.last_completion_status, "")   # no contract ran


# ── real Orchestrator: planner -> router -> Gateway -> executor end-to-end ──
class TestOrchestratorEndToEnd(unittest.TestCase):
    """Constructs a real Orchestrator wired to a real AstraRouter/Gateway
    and a scripted Provider adapter — no bootstrap network/credential
    dependencies, but every layer between Planner and the Provider is the
    genuine production object."""

    def _stack(self, replies):
        store = Store(":memory:")
        provider = SequencedProvider("gemini", ["model-a"], replies)
        router = _router(provider, store=store)
        planner = Planner(router=router, tools=["answer"])
        executor = Executor(registry=None)
        orch = Orchestrator(store, planner=planner, executor=executor,
                            router=router)
        return orch, provider

    def test_complete_task_runs_to_completion_via_real_router_and_gateway(self):
        orch, provider = self._stack(
            ['{"steps":[{"id":"s1","tool":"answer",'
             '"params":{"text":"hello there"},"description":"reply"}]}'])
        rec = orch.submit("say hello", sync=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(len(provider.calls), 1)

    def test_incomplete_plan_is_corrected_before_orchestrator_executes(self):
        """The correction happens INSIDE Planner/Router/Gateway, entirely
        before the Orchestrator ever sees a step to execute — proving the
        verify->correct->re-verify loop is on the real path, not just
        reachable in isolation."""
        orch, provider = self._stack(
            ["garbage, not json",
             '{"steps":[{"id":"s1","tool":"answer",'
             '"params":{"text":"hello there"},"description":"reply"}]}'])
        rec = orch.submit("say hello", sync=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(len(provider.calls), 2)
        results = rec.get("results") or {}
        self.assertEqual(results["s1"]["output"]["text"], "hello there")

    def test_never_falsely_reports_complete_after_exhausted_correction(self):
        """§8 final gate: exhausting correction attempts must never
        surface as a falsely-successful plan/execution — the Orchestrator
        still completes (via the graceful answer fallback), but the
        Planner's own completion_status stays INCOMPLETE, never COMPLETE."""
        orch, provider = self._stack(["still garbage"] * 10)
        rec = orch.submit("say hello", sync=True)
        self.assertEqual(orch.planner.last_completion_status, INCOMPLETE)
        # the run still finishes (graceful fallback), but nothing here
        # claims the ORIGINAL plan request was ever verified COMPLETE.
        self.assertIn(rec["status"], ("COMPLETED", "FAILED"))


if __name__ == "__main__":
    unittest.main()
