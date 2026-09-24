"""Regression tests for the provider-selection / gateway-routing fixes.

  1. AI_ROUTING_PREFERENCE (router config) is honoured; only an EXPLICIT
     per-request `user_preference` overrides it, and `rank()` mutates no
     shared state (thread/exception safe).
  2. The Gateway's last-successful nudge never overrides a clearly better
     router ranking ("fastest healthy first"), but still works as a
     tie-break / when no scores are attached.
  3. `classify()` uses whole-word matching and checks coding before research.
  4. Per-model latency is an EMA over SUCCESSFUL calls only.
"""
from __future__ import annotations

import unittest

from astra.ai.gateway_contract import ProviderExecutionTarget
from astra.ai.gateway_recovery import GatewayExecutionRecovery
from astra.ai.models import Model
from astra.ai.router import (AstraRouter, RoutingRequest, RoutingResult,
                             classify)
from astra.ai.routing_policy import RoutingDecisionPolicy


class _Adapter:
    def __init__(self, name):
        self.name = name
        self.health_info = {"state": "healthy"}


def _req(**kw):
    return RoutingRequest(task_type="simple_chat",
                          messages=[{"role": "user", "content": "hi"}], **kw)


class TestPreferenceHandling(unittest.TestCase):
    def test_request_default_preference_is_unset(self):
        self.assertIsNone(RoutingRequest().user_preference)

    def test_router_config_preference_is_used_when_request_has_none(self):
        pol = RoutingDecisionPolicy(preference="best_quality")
        self.assertEqual(pol.effective_preference(_req()), "best_quality")

    def test_explicit_request_preference_wins(self):
        pol = RoutingDecisionPolicy(preference="best_quality")
        self.assertEqual(
            pol.effective_preference(_req(user_preference="lowest_cost")),
            "lowest_cost")

    def test_preference_actually_changes_ranking(self):
        fast = (_Adapter("a"), Model("a", "fast", quality_class="fast",
                                     speed_class="fast", cost_class="cheap"))
        best = (_Adapter("b"), Model("b", "best", quality_class="high",
                                     speed_class="slow", cost_class="premium"))
        stats = {"a:fast": {"calls": 5, "success_rate": 1.0,
                            "avg_latency_ms": 200.0},
                 "b:best": {"calls": 5, "success_rate": 1.0,
                            "avg_latency_ms": 3500.0}}
        req = RoutingRequest(task_type="coding",
                             messages=[{"role": "user", "content": "x"}])
        fastest = RoutingDecisionPolicy(stats=stats, preference="fastest")
        quality = RoutingDecisionPolicy(stats=stats, preference="best_quality")
        self.assertEqual(fastest.rank([fast, best], req)[0][2].model_id, "fast")
        self.assertEqual(quality.rank([fast, best], req)[0][2].model_id, "best")

    def test_rank_does_not_mutate_or_leak_preference(self):
        pol = RoutingDecisionPolicy(preference="balanced")
        a = (_Adapter("a"), Model("a", "m"))
        pol.rank([a], _req(user_preference="fastest"))
        self.assertEqual(pol.preference, "balanced")

        class Boom(Model):
            @property
            def capabilities(self):
                raise RuntimeError("boom")

            @capabilities.setter
            def capabilities(self, v):
                pass

        with self.assertRaises(Exception):
            pol.rank([(_Adapter("x"), Boom("x", "y"))],
                     _req(user_preference="fastest"))
        self.assertEqual(pol.preference, "balanced")


class TestLastSuccessfulDoesNotOverrideRanking(unittest.TestCase):
    def _t(self, provider, model, score=None):
        md = {} if score is None else {"score": score}
        return ProviderExecutionTarget(provider, model, ("chat",), metadata=md)

    def test_slow_last_success_does_not_beat_clearly_better_target(self):
        rec = GatewayExecutionRecovery(store=None)
        fast = self._t("groq", "fast", score=9.0)
        slow = self._t("gemini", "slow", score=2.0)
        rec.report_execution_success(slow, latency_ms=4000)
        self.assertEqual(rec.select_execution_target([fast, slow]).model_id,
                         "fast")

    def test_last_success_still_wins_a_near_tie(self):
        rec = GatewayExecutionRecovery(store=None)
        a = self._t("groq", "a", score=5.00)
        b = self._t("gemini", "b", score=4.90)
        rec.report_execution_success(b, latency_ms=300)
        self.assertEqual(rec.select_execution_target([a, b]).model_id, "b")

    def test_without_scores_the_soft_nudge_is_unchanged(self):
        rec = GatewayExecutionRecovery(store=None)
        a, b = self._t("groq", "a"), self._t("gemini", "b")
        rec.report_execution_success(b, latency_ms=300)
        self.assertEqual(rec.select_execution_target([a, b]).model_id, "b")

    def test_cooled_down_target_is_still_skipped(self):
        rec = GatewayExecutionRecovery(store=None)
        a, b = self._t("groq", "a", score=9.0), self._t("gemini", "b", score=8.9)
        rec.report_execution_failure(a, "RATE_LIMIT")
        self.assertEqual(rec.select_execution_target([a, b]).model_id, "b")

    def test_router_attaches_score_to_gateway_targets(self):
        r = AstraRouter(providers=[])
        ranked = [(7.5, _Adapter("groq"), Model("groq", "m"))]
        (t,) = r._execution_targets(ranked)
        self.assertEqual(t.metadata.get("score"), 7.5)


class TestClassify(unittest.TestCase):
    def test_substring_no_longer_triggers_research(self):
        self.assertEqual(classify("fix this bug, I already tried everything"),
                         "coding")
        self.assertEqual(classify("this thread is long, refactor the repo"),
                         "coding")
        self.assertEqual(classify("my spreadsheet is broken"), "simple_chat")

    def test_coding_beats_research_when_both_match(self):
        self.assertEqual(classify("debug my code about login"), "coding")

    def test_research_still_detected(self):
        self.assertEqual(
            classify("research the top airdrops and write a report"),
            "research")
        self.assertEqual(classify("what is a blockchain"), "research")
        self.assertEqual(classify("please read this and compare them"),
                         "research")


class TestLatencyEma(unittest.TestCase):
    def _rr(self, ok, latency):
        return RoutingResult(ok=ok, provider="p", model="m", latency_ms=latency,
                             error="" if ok else "boom")

    def test_failed_call_does_not_change_latency(self):
        r = AstraRouter(providers=[])
        r._record_route(_req(), self._rr(True, 400))
        r._record_route(_req(), self._rr(False, 30000))   # timeout
        row = r.policy.stats["p:m"]
        self.assertEqual(row["avg_latency_ms"], 400.0)
        self.assertEqual(row["calls"], 2)
        self.assertEqual(row["success_rate"], 0.5)

    def test_average_tracks_recent_speed(self):
        r = AstraRouter(providers=[])
        r._record_route(_req(), self._rr(True, 4000))     # slow history
        for _ in range(12):
            r._record_route(_req(), self._rr(True, 300))  # now fast
        self.assertLess(r.policy.stats["p:m"]["avg_latency_ms"], 500.0)


if __name__ == "__main__":
    unittest.main()
