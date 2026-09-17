"""Tests for the two gaps closed in this pass:

Gap 1 — strict mandatory Gateway availability/entry enforcement:
  * `build_astra_ai_gateway` must never return None: the Gateway's
    control/governance layer (routing decisions + Task Completion
    Contract verification) does not depend on any GW_* connection being
    configured, so it must always be attached to the router.
  * `AstraRouter.route_request` must fail CLOSED (not silently succeed
    unverified) for any request carrying a `task_contract` if, despite
    the above, no Gateway is attached.

Gap 2 — automatic Executor/tool execution evidence -> Gateway final
task-completion verification:
  * After the Executor genuinely runs a plan's tool steps, Orchestrator
    must gather real evidence from what happened and hand it to the
    Gateway's Task Completion Contract machinery for a FINAL verification
    pass distinct from Planner's pre-execution plan-shape check.
  * A verification failure must trigger a real, bounded correction
    round-trip through the Existing Provider System, whose result — new
    steps — actually get executed (never a fabricated/placeholder
    "success"), and an already-succeeded step must never be replayed.
  * A pure-conversation ("answer"-only) plan has nothing execution-side
    to verify and must be skipped (not dragged into evidence machinery
    it never needed — matches the existing "don't invent requirements"
    philosophy elsewhere in the Gateway spec).
  * When no Gateway control layer is attached at all, a tool-executing
    task must be reported UNVERIFIED — never silently COMPLETE.
"""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway, build_astra_ai_gateway
from astra.ai.gateway_task_completion import COMPLETE
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.config import Config
from astra.core.exceptions import ProviderError
from astra.core.executor import Executor
from astra.core.orchestrator import Orchestrator
from astra.core.planner import Planner
from astra.store import Store
from astra.tools.registry import ToolRegistry


# ═══════════════════════════════════════════════════════════════════════
# Gap 1 — strict mandatory Gateway availability/entry enforcement
# ═══════════════════════════════════════════════════════════════════════
class TestGap1StrictGatewayAvailability(unittest.TestCase):
    def test_builder_never_returns_none_even_fully_unconfigured(self):
        gw = build_astra_ai_gateway(config=Config())
        self.assertIsNotNone(gw)
        self.assertEqual(gw.connections, [])
        self.assertFalse(gw.is_usable())
        # the control layer (routing decisions + task-completion
        # verification) is present regardless of connections:
        self.assertTrue(hasattr(gw, "select_execution_target"))
        self.assertTrue(hasattr(gw, "recover_execution_target"))
        self.assertTrue(hasattr(gw, "supervise_task"))

    def test_route_request_fails_closed_for_contract_with_no_gateway(self):
        """Defense-in-depth: even if some caller builds an AstraRouter
        without wiring a Gateway in at all, a task-contract-bearing
        request must fail closed with a clear error, never silently
        report an unverified success."""
        from astra.ai.gateway_task_completion import build_task_completion_contract

        class _Provider:
            name = "gemini"
            models = ["m"]
            def health_check(self): return True
            def chat(self, messages, model=None, max_tokens=500):
                return "some text"

        router = AstraRouter(providers=[_Provider()])   # gateway=None (default)
        self.assertIsNone(router.gateway)
        contract = build_task_completion_contract("do a thing")
        rr = router.route_request(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_contract=contract))
        self.assertFalse(rr.ok)
        self.assertIn("Gateway", rr.error)

    def test_plain_request_with_no_contract_is_unaffected(self):
        """Regression safety: a request that never asked for Gateway
        verification (task_contract=None, the default) behaves exactly
        as before — this fix is additive, not a new requirement on every
        router call."""
        class _Provider:
            name = "gemini"
            models = ["m"]
            def health_check(self): return True
            def chat(self, messages, model=None, max_tokens=500):
                return "hello"

        router = AstraRouter(providers=[_Provider()])
        rr = router.route_request(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.text, "hello")


# ═══════════════════════════════════════════════════════════════════════
# Gap 2 fixtures: a real ToolRegistry + Executor + Orchestrator stack
# ═══════════════════════════════════════════════════════════════════════
class _ShimPool:
    def __bool__(self):
        return True


class SequencedProvider:
    """Real call-path fake Existing-Provider adapter: scripted replies."""

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


def _check_state():
    """A tiny in-memory 'system' a fake tool mutates, so a corrective
    step can genuinely change real state — proving the correction loop
    executes real work, not a fabricated success."""
    return {"checked": False}


def _build_stack(provider_replies, register_flaky_tool=True):
    store = Store(":memory:")
    provider = SequencedProvider("gemini", ["model-a"], provider_replies)
    gw = AstraAIGateway(connections=[], store=store)
    router = AstraRouter(providers=[provider], gateway=gw)
    planner = Planner(router=router, tools=["run_check"])
    registry = ToolRegistry()
    state = _check_state()

    def run_check(params, ctx):
        state["checked"] = True
        return {"ok": True, "result": {"checked": True}}

    if register_flaky_tool:
        registry.register_function("run_check", run_check, idempotent=True)
    executor = Executor(registry=registry)
    orch = Orchestrator(store, planner=planner, executor=executor, router=router)
    return orch, provider, router, state


# ═══════════════════════════════════════════════════════════════════════
# Gap 2 — automatic evidence -> Gateway final task-completion verification
# ═══════════════════════════════════════════════════════════════════════
class TestGap2AutomaticEvidenceVerification(unittest.TestCase):
    def test_pure_answer_plan_skips_final_verification(self):
        """Nothing execution-side to verify for a plain conversational
        reply — Planner's own pre-execution contract already covered it."""
        orch, provider, router, state = _build_stack(
            ['{"steps":[{"id":"s1","tool":"answer",'
             '"params":{"text":"hi"},"description":"reply"}]}'])
        rec = orch.submit("say hi", sync=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["gateway_verification_status"], "")

    def test_tool_based_plan_all_ok_verifies_complete_without_correction(self):
        orch, provider, router, state = _build_stack(
            ['{"steps":[{"id":"s1","tool":"run_check",'
             '"params":{},"description":"run the check"}]}'])
        rec = orch.submit("run the check", sync=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["gateway_verification_status"], COMPLETE)
        self.assertTrue(state["checked"])
        self.assertEqual(len(provider.calls), 1)   # no correction call needed

    def test_evidence_is_real_not_fabricated(self):
        """Directly exercises `_gateway_final_task_verification` with a
        manufactured evidence-carrying `results` dict, proving the
        evidence handed to the Gateway is built from actual step
        results (step_failures / steps_completed), not invented."""
        orch, provider, router, state = _build_stack(
            ['{"steps":[{"id":"s2","tool":"run_check",'
             '"params":{},"description":"fix it"}]}'])
        plan = [{"id": "s1", "tool": "run_check", "params": {},
                 "description": "run the check", "verify": [], "retries": 0,
                 "depends_on": []}]
        results = {"s1": {"step": "s1", "tool": "run_check", "ok": False,
                          "error": "check failed", "output": {}}}
        original_ids = set(results)
        row = {"id": 1}
        results2, status, reason = orch._gateway_final_task_verification(
            "exec-x", row, "run the check", plan, results, "")
        self.assertEqual(status, COMPLETE)   # corrective run_check succeeded
        self.assertTrue(state["checked"])
        # Planner always numbers a freshly-parsed plan starting at "s1",
        # so this collides with the original "s1" and gets renumbered —
        # what matters is that SOME new step actually ran and succeeded.
        new_ids = set(results2) - original_ids
        self.assertEqual(len(new_ids), 1)
        self.assertTrue(results2[next(iter(new_ids))]["ok"])
        self.assertEqual(len(provider.calls), 1)   # exactly one correction call

    def test_already_succeeded_step_is_never_replayed(self):
        """The corrective plan's step id 's1' collides with an already-
        successful step in the CURRENT plan — it must be renumbered, and
        the original successful step must never be re-executed, even
        though a sibling step ('s2') failed and triggers correction."""
        orch, provider, router, state = _build_stack(
            ['{"steps":[{"id":"s1","tool":"run_check",'
             '"params":{},"description":"fix the broken part"}]}'])
        run_count = {"n": 0}

        def counting_check(params, ctx):
            run_count["n"] += 1
            return {"ok": True, "result": {"n": run_count["n"]}}
        orch.executor.registry._tools["run_check"].fn = counting_check

        plan = [
            {"id": "s1", "tool": "run_check", "params": {},
             "description": "already done", "verify": [], "retries": 0,
             "depends_on": []},
            {"id": "s2", "tool": "run_check", "params": {},
             "description": "broken part", "verify": [], "retries": 0,
             "depends_on": []},
        ]
        # s1 already genuinely succeeded (a distinctive, hand-set output
        # that counting_check would never itself produce as its FIRST
        # real call, so re-execution is detectable); s2 genuinely failed.
        results = {"s1": {"step": "s1", "tool": "run_check", "ok": True,
                          "output": {"n": 99}, "error": ""},
                   "s2": {"step": "s2", "tool": "run_check", "ok": False,
                          "output": {}, "error": "broken"}}
        row = {"id": 1}
        results2, status, reason = orch._gateway_final_task_verification(
            "exec-y", row, "fix the broken part", plan, results, "")
        self.assertEqual(status, COMPLETE)
        # exactly one real execution happened (the renumbered corrective
        # step) — 's1' was never touched again.
        self.assertEqual(run_count["n"], 1)
        self.assertEqual(results2["s1"]["output"], {"n": 99})

    def test_correction_is_bounded_and_never_falsely_reports_complete(self):
        """A goal that keeps failing across every bounded correction
        attempt must end UNCERTAIN/INCOMPLETE, never a false COMPLETE."""
        from astra.core.correction import MAX_CORRECTION_ATTEMPTS
        orch, provider, router, state = _build_stack(
            ["not valid json"] * (MAX_CORRECTION_ATTEMPTS + 1),
            register_flaky_tool=False)
        # tool always fails, no fix ever registered
        registry = orch.executor.registry
        registry.register_function("run_check",
                                   lambda p, c: {"ok": False, "error": "still broken"},
                                   idempotent=True)
        plan = [{"id": "s1", "tool": "run_check", "params": {},
                 "description": "check", "verify": [], "retries": 0,
                 "depends_on": []}]
        results = {"s1": {"step": "s1", "tool": "run_check", "ok": False,
                          "error": "still broken", "output": {}}}
        row = {"id": 1}
        results2, status, reason = orch._gateway_final_task_verification(
            "exec-z", row, "fix the check", plan, results, "")
        self.assertNotEqual(status, COMPLETE)
        self.assertEqual(len(provider.calls), MAX_CORRECTION_ATTEMPTS)

    def test_no_gateway_attached_reports_unverified_not_complete(self):
        """Strict Gap-1 enforcement carried into Gap 2: a tool-executing
        task with no Gateway control layer at all must be reported
        UNVERIFIED, never silently COMPLETE."""
        store = Store(":memory:")
        provider = SequencedProvider("gemini", ["model-a"], [])
        router = AstraRouter(providers=[provider])   # no gateway at all
        planner = Planner(router=router, tools=["run_check"])
        registry = ToolRegistry()
        registry.register_function(
            "run_check", lambda p, c: {"ok": True, "result": {}},
            idempotent=True)
        executor = Executor(registry=registry)
        orch = Orchestrator(store, planner=planner, executor=executor, router=router)
        plan = [{"id": "s1", "tool": "run_check", "params": {},
                 "description": "check", "verify": [], "retries": 0,
                 "depends_on": []}]
        results = {"s1": {"step": "s1", "tool": "run_check", "ok": True,
                          "output": {}, "error": ""}}
        row = {"id": 1}
        results2, status, reason = orch._gateway_final_task_verification(
            "exec-w", row, "check it", plan, results, "")
        self.assertEqual(status, "UNVERIFIED")
        self.assertIn("Gateway", reason)


if __name__ == "__main__":
    unittest.main()
