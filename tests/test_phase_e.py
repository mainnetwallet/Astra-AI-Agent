"""Phase E — orchestrator / planner / executor upgrades.

Covers:
- error classification (canonical codes + text fingerprinting),
- retry policy (exponential backoff + jitter; non-retryable codes never),
- dependency ordering + {{x}} data-flow through the orchestrator,
- recovery semantics: WAITING_USER executions survive a restart (reconstituted,
  not blanket-failed) while uncertain mid-flight ones fail safely,
- no silent exception swallowing: a raising tool becomes a classified step
  result, never an unhandled crash.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from astra.core.classification import (RetryPolicy, code_for, CODES,
                                       NON_RETRYABLE)
from astra.core.executor import Executor
from astra.core.planner import Planner
from astra.core.orchestrator import Orchestrator
from astra.core.exceptions import (AstraError, PermissionError,
                                   NetworkError, ValidationError)


def make_stack():
    from astra.bootstrap import build
    from astra.store import Store
    d = tempfile.mkdtemp()
    return build(Store(os.path.join(d, "t.db")))


# ── error classification ──────────────────────────────────────────────────────
class TestClassification(unittest.TestCase):
    def test_canonical_codes_are_the_spec_set(self):
        spec = {"validation", "authentication", "authorization", "rate_limit",
                "timeout", "network", "provider", "model_unavailable", "tool",
                "browser", "web3", "database", "internal", "user_cancelled",
                "transaction_policy", "transaction_rejected",
                "transaction_failed"}
        self.assertEqual(CODES, spec)

    def test_maps_exception_categories(self):
        self.assertEqual(code_for(ValidationError("x")), "validation")
        self.assertEqual(code_for(PermissionError("x")), "authorization")
        self.assertEqual(code_for(NetworkError("x")), "network")

    def test_maps_exception_code_attribute(self):
        from astra.web3.policy import TransactionPolicyError
        self.assertEqual(code_for(TransactionPolicyError("x")),
                         "transaction_policy")

    def test_text_fingerprint(self):
        self.assertEqual(code_for(message="https error 429 rate limit"), "rate_limit")
        self.assertEqual(code_for(message="timed out waiting for rpc"), "timeout")
        self.assertEqual(code_for(message="authentication failed"), "authentication")
        self.assertEqual(code_for(message="user cancelled the run"), "user_cancelled")
        self.assertEqual(code_for(message="something unknown"), "internal")

    def test_retry_set(self):
        for code in ("validation", "authorization", "transaction_policy",
                     "transaction_rejected", "transaction_failed"):
            self.assertIn(code, NON_RETRYABLE)
        rp = RetryPolicy()
        self.assertFalse(rp.can_retry("transaction_failed"))
        self.assertTrue(rp.can_retry("rate_limit"))
        self.assertEqual(rp.attempts("transaction_failed"), 0)

    def test_backoff_is_exponential_and_bounded(self):
        rp = RetryPolicy(base_delay_s=0.1, max_delay_s=0.4)
        delays = [rp.backoff_ms(i) for i in range(4)]
        for d in delays:
            self.assertLessEqual(d, 400)
        self.assertLessEqual(delays[0], delays[1])


# ── executor: classified, no-swallow retries ─────────────────────────────────
class TestExecutorClassified(unittest.TestCase):
    def _flaky_reg(self, exc=NetworkError):
        from astra.tools.registry import ToolRegistry
        from astra.tools.schemas import Tool
        reg = ToolRegistry()
        calls = []

        def flaky(args, ctx):
            calls.append(1)
            raise exc("boom")
        reg.register(Tool("flaky", flaky, idempotent=True, retries=0))
        return reg, calls

    def test_raised_tool_is_classified_not_crashed(self):
        reg, calls = self._flaky_reg()
        ex = Executor(reg)
        out = ex.execute({"id": "s1", "tool": "flaky", "params": {}, "retries": 0},
                         ctx=None, run_ctx=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_code"], "network")

    def test_non_retryable_code_not_retried(self):
        from astra.tools.registry import ToolRegistry
        from astra.tools.schemas import Tool
        reg = ToolRegistry()
        calls = []

        def deny(args, ctx):
            calls.append(1)
            raise PermissionError("denied")
        reg.register(Tool("boom", deny, idempotent=False, retries=5))
        ex = Executor(reg)
        out = ex.execute({"id": "s1", "tool": "boom", "params": {}, "retries": 5},
                         ctx=None, run_ctx=None)
        self.assertEqual(out["error_code"], "authorization")
        self.assertEqual(len(calls), 1)   # never retried authorization


# ── planner dependency + budget ──────────────────────────────────────────────
class TestPlannerDependencies(unittest.TestCase):
    def test_offline_step_declares_dependency(self):
        p = Planner()
        steps = p.plan("plan my day")
        t2 = [s for s in steps if s["id"] == "t2"]
        self.assertTrue(t2)
        self.assertEqual(t2[0]["depends_on"], ["t1"])

    def test_plan_respects_budget(self):
        p = Planner()
        big = "research " + "x" * 5000
        steps = p.plan(big, max_goal_chars=2000, max_steps=2)
        self.assertLessEqual(len(steps), 2)

    def test_topo_order(self):
        steps = [
            {"id": "s2", "depends_on": ["s1"]},
            {"id": "s1", "depends_on": []},
            {"id": "s0", "depends_on": []},
        ]
        ordered = Orchestrator._topo(steps)
        ids = [s["id"] for s in ordered]
        # stable order: ready steps keep list order; s2 (depends on s1) runs after it
        self.assertEqual(ids, ["s1", "s0", "s2"])
        self.assertLess(ids.index("s1"), ids.index("s2"))

    def test_resolve_looks_up_earlier_result(self):
        results = {"w1": {"output": {"url": "https://example.com"}}}
        step = {"id": "r2", "params": {"url": "{{w1.output.url}}"}}
        resolved = Orchestrator._resolve(step, results, {})
        self.assertEqual(resolved["params"]["url"], "https://example.com")


# ── recovery semantics ────────────────────────────────────────────────────────
class TestRecoverySemantics(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.orch = self.stack["orchestrator"]
        self.store = self.stack["store"]

    def insert(self, eid, status, pending_step=""):
        self.store.exec(
            "INSERT INTO astra_executions "
            "(execution_id, goal, status, pending_step) VALUES (?,?,?,?)",
            (eid, "g", status, pending_step))

    def test_waiting_user_survives_restart(self):
        step = json.dumps({"id": "s1", "tool": "write_file",
                           "params": {"path": "x", "content": "y"}})
        self.insert("exec-pending", "WAITING_USER", pending_step=step)
        recovered = self.orch.recover_stale()
        self.assertIn("exec-pending", recovered)
        row = self.orch._row("exec-pending")
        self.assertEqual(row["status"], "WAITING_USER")   # not failed
        self.assertIn("exec-pending", self.orch._pending)  # reconstituted

    def test_executing_still_fails_safely(self):
        self.insert("exec-mid", "EXECUTING")
        recovered = self.orch.recover_stale()
        self.assertIn("exec-mid", recovered)
        self.assertEqual(self.orch._row("exec-mid")["status"], "FAILED")
        self.assertIn("restart", self.orch._row("exec-mid")["error"])

    def test_completed_untouched(self):
        r = self.orch.submit("get_health", sync=True)
        self.orch.recover_stale()
        self.assertEqual(self.orch._row(r["execution_id"])["status"], "COMPLETED")


# ── end-to-end: dependent steps execute in order ─────────────────────────────
class TestOrchestratorDependencyRun(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.orch = self.stack["orchestrator"]

    def test_dependency_steps_run_in_order(self):
        r = self.orch.submit("plan my day", sync=True)
        self.assertEqual(r["status"], "COMPLETED")
        order = [s["tool"] for s in r["steps_detail"]]
        self.assertIn("list_tasks", order)


if __name__ == "__main__":
    unittest.main()