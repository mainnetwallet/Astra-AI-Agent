"""Astra AI Gateway — task-level execution recovery for the EXISTING
Provider system (FINAL FIX PROMPT: §2-5, §7-12, §18, isolation §1/§3).

Covers:
  - centralized failure classification (§7)
  - model-level cooldown never disables the whole provider (§6/§8)
  - provider-level fallback when every model of a provider fails (§9)
  - bounded recovery / no infinite loops (§10)
  - last-successful is a soft preference, never a lock (§12)
  - persistence across "restart" via the same Store (§18)
  - the sanitized handoff contract carries no credentials (§3)
  - AstraRouter reports outcomes to an attached Gateway without ever
    executing against it, and without changing behavior when no Gateway
    is attached (isolation, §1)
  - explicit no-fallback model request fails honestly (§11)
"""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_contract import (AUTH_FAILURE, CONTEXT_TOO_LARGE,
                                       MODEL_UNAVAILABLE, NETWORK,
                                       ProviderExecutionTarget, QUOTA_EXCEEDED,
                                       RATE_LIMIT, TIMEOUT, UNKNOWN,
                                       classify_execution_failure)
from astra.ai.gateway_recovery import GatewayExecutionRecovery
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.exceptions import ProviderError, TimeoutError as AstraTimeout
from astra.store import Store


class _ShimPool:
    def __init__(self, ok):
        self.ok = ok

    def __bool__(self):
        return self.ok


class FakeAIProvider:
    """Minimal fake provider adapter (name/models/pool/chat surface) —
    local copy of tests/test_ai.py's fixture so this module has no
    cross-test-file import dependency."""
    name = "fake"

    def __init__(self, name=None, models=None, healthy=True, fail_first=0):
        self.name = name or "fake"
        self.models = models or ["m0"]
        self.pool = _ShimPool(healthy)
        self._calls = 0
        self.fail_first = fail_first

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self._calls += 1
        if self._calls <= self.fail_first:
            raise ProviderError("simulated failure")
        return f"reply-from-{self.name}"

    def health_check(self):
        return bool(self.pool)


def _t(provider_id, model_id, caps=()):
    return ProviderExecutionTarget(provider_id=provider_id, model_id=model_id,
                                   capabilities=tuple(caps))


class TestFailureClassification(unittest.TestCase):
    """§7: one centralized classifier, never a second competing brain."""

    def test_rate_limit_vs_quota(self):
        self.assertEqual(classify_execution_failure(message="HTTP 429 rate limit reached"),
                         RATE_LIMIT)
        self.assertEqual(classify_execution_failure(message="quota exceeded for this key"),
                         QUOTA_EXCEEDED)

    def test_timeout_network_auth(self):
        self.assertEqual(classify_execution_failure(message="request timed out"), TIMEOUT)
        self.assertEqual(classify_execution_failure(message="connection refused"), NETWORK)
        self.assertEqual(classify_execution_failure(message="authentication failed"),
                         AUTH_FAILURE)

    def test_model_unavailable_and_context_too_large(self):
        self.assertEqual(classify_execution_failure(message="model_unavailable: model X"),
                         MODEL_UNAVAILABLE)
        self.assertEqual(
            classify_execution_failure(message="context length exceeds the model limit"),
            CONTEXT_TOO_LARGE)

    def test_unknown_fallback(self):
        self.assertEqual(classify_execution_failure(message="something bizarre happened"),
                         UNKNOWN)

    def test_exception_objects_classify_too(self):
        self.assertEqual(classify_execution_failure(ProviderError("rate limit reached")),
                         RATE_LIMIT)
        self.assertEqual(classify_execution_failure(AstraTimeout("timed out")), TIMEOUT)


class TestExecutionTargetContract(unittest.TestCase):
    """§3: sanitized metadata only — no secrets, no adapter objects."""

    def test_target_has_no_credential_fields(self):
        target = _t("gemini", "model-a", ["chat", "tools"])
        d = target.to_dict()
        self.assertEqual(set(d.keys()), {"provider_id", "model_id",
                                         "capabilities", "metadata"})
        blob = str(d).lower()
        for forbidden in ("api_key", "secret", "token", "authorization"):
            self.assertNotIn(forbidden, blob)

    def test_target_is_hashable_by_provider_and_model(self):
        a = _t("gemini", "model-a")
        b = _t("gemini", "model-a")
        self.assertEqual(a.key(), b.key())
        self.assertEqual({a, b}, {a})


class TestGatewayExecutionRecovery(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.rec = GatewayExecutionRecovery(store=self.store)

    # §6/§8: a model-specific failure must not disable the whole provider
    def test_model_level_failure_does_not_disable_provider(self):
        a = _t("gemini", "model-a")
        b = _t("gemini", "model-b")
        self.rec.report_execution_failure(a, RATE_LIMIT)
        selected = self.rec.select_execution_target([a, b])
        self.assertEqual(selected.model_id, "model-b")

    # §9: only when EVERY model of a provider has failed does routing move
    # on to the next provider — proven here by exhausting both of Gemini's
    # models and confirming only Groq's model remains selectable.
    def test_provider_level_fallback_when_all_models_fail(self):
        g_a = _t("gemini", "model-a")
        g_b = _t("gemini", "model-b")
        q_a = _t("groq", "model-c")
        self.rec.report_execution_failure(g_a, RATE_LIMIT)
        self.rec.report_execution_failure(g_b, RATE_LIMIT)
        selected = self.rec.select_execution_target([g_a, g_b, q_a])
        self.assertEqual(selected.provider_id, "groq")

    # §10: bounded — never loops forever; eventually returns None honestly
    def test_no_infinite_loop_all_targets_exhausted(self):
        candidates = [_t("gemini", "a"), _t("groq", "b")]
        target = candidates[0]
        seen = set()
        attempts = 0
        while target is not None and attempts < 10:
            attempts += 1
            seen.add(target.key())
            target = self.rec.recover_execution_target(
                candidates, target, RATE_LIMIT, exclude=seen)
        self.assertIsNone(target)
        self.assertLessEqual(attempts, len(candidates))

    def test_recover_execution_target_reports_and_selects_next(self):
        a, b = _t("gemini", "model-a"), _t("groq", "model-b")
        nxt = self.rec.recover_execution_target([a, b], a, RATE_LIMIT)
        self.assertEqual(nxt.key(), b.key())
        # the failed target is now in cooldown
        self.assertFalse(self.rec.is_eligible(a))

    # §12: last successful is a *soft* preference, never a permanent lock
    def test_last_successful_is_soft_preference_not_lock(self):
        a, b = _t("gemini", "model-a"), _t("groq", "model-b")
        self.rec.report_execution_success(a, latency_ms=50)
        # `a` still eligible and last-successful -> stays preferred
        self.assertEqual(self.rec.select_execution_target([a, b]).key(), a.key())
        # once `a` fails, `b` must win even though `a` was "last successful"
        self.rec.report_execution_failure(a, RATE_LIMIT)
        self.assertEqual(self.rec.select_execution_target([a, b]).key(), b.key())

    def test_required_capabilities_filter(self):
        a = _t("gemini", "model-a", caps=["chat"])
        b = _t("gemini", "model-b", caps=["chat", "vision"])
        selected = self.rec.select_execution_target(
            [a, b], required_capabilities=["vision"])
        self.assertEqual(selected.key(), b.key())

    # §18: reuse the existing Store — health/cooldown survive a "restart"
    def test_persistence_across_restart(self):
        target = _t("gemini", "model-a")
        self.rec.report_execution_failure(target, RATE_LIMIT, cooldown_s=120)
        reloaded = GatewayExecutionRecovery(store=self.store)
        self.assertFalse(reloaded.is_eligible(target))

    # namespace isolation: the Gateway's OWN four-connection catalog health
    # must never be touched by execution-recovery bookkeeping for the
    # Existing Provider system, even when both happen to use the literal
    # provider name "gemini".
    def test_namespaced_apart_from_gateways_own_catalog_health(self):
        gw = AstraAIGateway(connections=[], store=self.store)
        gw.execution_recovery.report_execution_failure(
            _t("gemini", "model-a"), RATE_LIMIT)
        # the Gateway's own routing_state (its 4 GW_* connections) is
        # untouched — still healthy, because the keys never collide.
        own_health = gw.routing_state.get_health("gemini", "model-a")
        self.assertTrue(own_health.healthy)


class TestAstraRouterGatewayIntegration(unittest.TestCase):
    """AstraRouter reports outcomes to an attached Gateway (§2-§5) but the
    Gateway never executes anything and is never a fallback target itself
    (isolation, preserved exactly as before this change)."""

    def test_router_works_unchanged_with_no_gateway_attached(self):
        good = FakeAIProvider(name="p1")
        router = AstraRouter(providers=[good])
        provider, model, text = router.route([{"role": "user", "content": "hi"}])
        self.assertEqual(provider, "p1")
        self.assertEqual(text, "reply-from-p1")

    def test_fallback_reports_failure_and_success_to_gateway(self):
        failing = FakeAIProvider(name="p1", fail_first=99)  # always fails
        good = FakeAIProvider(name="p2")
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[failing, good], gateway=gw)
        rr = router.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "p2")
        self.assertTrue(rr.fallback_used)
        # the failing target is now cooled down from the Gateway's point of
        # view, and the successful one is the recorded last-successful
        # target — proving reports actually reached the recovery API.
        self.assertFalse(gw.execution_recovery.is_eligible(_t("p1", "m0")))
        last = gw.execution_recovery.routing_state.last_successful()
        self.assertEqual(last["provider"], "existing::p2")

    def test_gateway_is_never_executed_against(self):
        """Even with a Gateway attached and every real provider failing,
        AstraRouter must fail honestly rather than dropping into the
        Gateway's own chat()."""
        class _TripwireGateway(AstraAIGateway):
            def chat(self, *a, **kw):
                raise AssertionError("AstraRouter must never call gateway.chat()")

        failing = FakeAIProvider(name="p1", fail_first=99)
        gw = _TripwireGateway(connections=[])
        router = AstraRouter(providers=[failing], gateway=gw)
        rr = router.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertFalse(rr.ok)

    def test_no_fallback_requested_model_fails_honestly(self):
        failing = FakeAIProvider(name="p1", models=["exact-model"], fail_first=99)
        good = FakeAIProvider(name="p2", models=["other-model"])
        router = AstraRouter(providers=[failing, good])
        req = RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                             preferred_provider="p1", preferred_model="exact-model",
                             no_fallback=True)
        rr = router.route_request(req)
        self.assertFalse(rr.ok)
        self.assertEqual(good._calls, 0)  # never substituted


if __name__ == "__main__":
    unittest.main()
