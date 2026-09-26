"""Regression tests for manual upstream health-check deduplication between
the Provider system (astra.ai.router.AstraRouter.test_provider_model) and
the Astra AI Gateway (astra.ai.gateway.AstraAIGateway.test_connection_model),
coordinated by astra.ai.shared_health.SharedHealthCoordinator.

Provider and Gateway stay fully independent systems; only the manual
"test connection" upstream HTTP probe is deduplicated when both would
otherwise send an identical real request (same canonical upstream provider +
same credential + same model).
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from astra.ai.adapters.base import CompatibleAdapter
from astra.ai.adapters.groq import GroqAdapter
from astra.ai.gateway import AstraAIGateway, AstraGatewayGroq, AstraGatewayOpenRouter, \
    _GatewayCompatibleConnection
from astra.ai.router import AstraRouter
from astra.ai.shared_health import SharedHealthCoordinator
from astra.core.config import Config
from astra.core.exceptions import ProviderError
from astra.store import Store


def _cfg(**kw):
    cfg = Config()
    cfg._runtime.update(**kw)
    return cfg


def _fake_post(calls, bad_secrets=()):
    """A `_post` usable as both `CompatibleAdapter._post` (Provider) and
    `_GatewayCompatibleConnection._post` (Gateway) — both share the same
    (self, url, body, cred) shape and `self.pool` attribute. Records every
    REAL upstream call it sees into the shared `calls` list."""
    def post(self, url, body, cred):
        secret = self.pool.get_secret_for(cred)
        calls.append((secret, body["model"]))
        if secret in bad_secrets:
            self._done(cred, True, reason="http 401", auth_failure=True)
            raise ProviderError(f"{self.name} unauthorized")
        return {"choices": [{"message": {"content": "pong"}}]}
    return post


def _provider(key="secret-A", model="m1"):
    return GroqAdapter(config=_cfg(GROQ_API_KEYS=key, GROQ_MODELS=model))


def _gw_conn(key="secret-A", model="m1", cls=AstraGatewayGroq,
            keys_env="GW_GROQ_API_KEYS", models_env="GW_GROQ_MODELS"):
    return cls(config=_cfg(**{keys_env: key, models_env: model}))


def _stack(store=None, provider=None, conn=None):
    provider = provider or _provider()
    conn = conn or _gw_conn()
    gateway = AstraAIGateway(connections=[conn], store=store)
    router = AstraRouter([provider], gateway=gateway, store=store)
    return router, gateway, provider, conn


_PATCHES = (mock.patch.object(CompatibleAdapter, "_post"),
           mock.patch.object(_GatewayCompatibleConnection, "_post"))


class _Base(unittest.TestCase):
    def _patched(self, calls, bad_secrets=()):
        p1 = mock.patch.object(CompatibleAdapter, "_post", _fake_post(calls, bad_secrets))
        p2 = mock.patch.object(_GatewayCompatibleConnection, "_post", _fake_post(calls, bad_secrets))
        p1.start(); p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)


class OrderingTests(_Base):
    """TEST 1 / TEST 2 — order shouldn't matter, one real call either way."""

    def test_provider_then_gateway_share_one_call(self):
        calls = []
        self._patched(calls)
        router, gateway, provider, conn = _stack()
        r1 = router.test_provider_model("groq", "m1")
        r2 = gateway.test_connection_model(conn, "m1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])

    def test_gateway_then_provider_share_one_call(self):
        calls = []
        self._patched(calls)
        router, gateway, provider, conn = _stack()
        r1 = gateway.test_connection_model(conn, "m1")
        r2 = router.test_provider_model("groq", "m1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])


class NonSharingTests(_Base):
    """TEST 3 / 4 / 5 — never over-deduplicate."""

    def test_different_keys_are_two_calls(self):
        calls = []
        self._patched(calls)
        provider = _provider(key="secret-A")
        conn = _gw_conn(key="secret-B")           # different credential
        router, gateway, provider, conn = _stack(provider=provider, conn=conn)
        router.test_provider_model("groq", "m1")
        gateway.test_connection_model(conn, "m1")
        self.assertEqual(len(calls), 2)

    def test_different_models_are_two_calls(self):
        calls = []
        self._patched(calls)
        provider = _provider(model="m1,m2")
        conn = _gw_conn(model="m1,m2")
        router, gateway, provider, conn = _stack(provider=provider, conn=conn)
        router.test_provider_model("groq", "m1")
        gateway.test_connection_model(conn, "m2")   # different model
        self.assertEqual(len(calls), 2)

    def test_different_providers_are_two_calls(self):
        calls = []
        self._patched(calls)
        provider = _provider(key="same-secret")
        # Same literal secret, but a DIFFERENT canonical upstream provider.
        conn = _gw_conn(key="same-secret", cls=AstraGatewayOpenRouter,
                        keys_env="GW_OPENROUTER_API_KEYS", models_env="GW_OPENROUTER_MODELS")
        router, gateway, provider, conn = _stack(provider=provider, conn=conn)
        router.test_provider_model("groq", "m1")
        gateway.test_connection_model(conn, "m1")
        self.assertEqual(len(calls), 2)


class FailureSharingTests(_Base):
    """TEST 6 — a shared failure is reused too, never converted or retried."""

    def test_failure_is_shared_not_retried(self):
        calls = []
        self._patched(calls, bad_secrets=("secret-A",))
        router, gateway, provider, conn = _stack()
        r1 = router.test_provider_model("groq", "m1")
        r2 = gateway.test_connection_model(conn, "m1")
        self.assertEqual(len(calls), 1)
        self.assertFalse(r1["ok"])
        self.assertFalse(r2["ok"])
        self.assertIn("unauthorized", r1["error"])
        self.assertEqual(r1["error"], r2["error"])


class ConcurrencyTests(_Base):
    """TEST 7 — concurrent Provider + Gateway probes for the same identity
    must still make exactly one real upstream call. Mandatory."""

    def test_concurrent_provider_and_gateway_make_one_call(self):
        calls = []
        release = threading.Event()

        def slow_post(self, url, body, cred):
            # Only the coordinator's chosen OWNER ever reaches this real
            # upstream call; holding it open here gives the other (waiter)
            # thread time to arrive at the coordinator and find the probe
            # already in flight, which is the race this test exercises.
            release.wait(timeout=5)
            secret = self.pool.get_secret_for(cred)
            calls.append((secret, body["model"]))
            return {"choices": [{"message": {"content": "pong"}}]}

        p1 = mock.patch.object(CompatibleAdapter, "_post", slow_post)
        p2 = mock.patch.object(_GatewayCompatibleConnection, "_post", slow_post)
        p1.start(); p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

        router, gateway, provider, conn = _stack()
        results = {}

        def run_provider():
            results["provider"] = router.test_provider_model("groq", "m1")

        def run_gateway():
            results["gateway"] = gateway.test_connection_model(conn, "m1")

        t1 = threading.Thread(target=run_provider)
        t2 = threading.Thread(target=run_gateway)
        t1.start(); t2.start()
        # Both threads must actually be inside the probe (racing) before
        # either is allowed to finish it -- otherwise this would degrade
        # into a sequential, not concurrent, test.
        time.sleep(0.05)
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(calls), 1)
        self.assertTrue(results["provider"]["ok"])
        self.assertTrue(results["gateway"]["ok"])
        self.assertEqual(results["provider"]["ok"], results["gateway"]["ok"])


class FreshnessTests(_Base):
    """TEST 8 / 9 — fresh reuse, and a real probe again once expired."""

    def test_fresh_result_reused_zero_new_calls(self):
        calls = []
        self._patched(calls)
        router, gateway, provider, conn = _stack()
        provider_result = router.test_provider_model("groq", "m1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(provider_result["key"], "key 1")
        self.assertEqual(provider_result["key_label"], "key 1")
        gateway_result = gateway.test_connection_model(conn, "m1")   # same identity, still fresh
        self.assertEqual(len(calls), 1)              # no new upstream call
        self.assertEqual(gateway_result["key"], "key 1")
        self.assertEqual(gateway_result["key_label"], "key 1")
        self.assertTrue(gateway_result["reused"])

    def test_expired_result_triggers_a_new_call(self):
        calls = []
        self._patched(calls)
        provider = _provider()
        conn = _gw_conn()
        gateway = AstraAIGateway(connections=[conn])
        router = AstraRouter([provider], gateway=gateway,
                             shared_health=SharedHealthCoordinator(ttl_s=0.05))
        router.test_provider_model("groq", "m1")
        self.assertEqual(len(calls), 1)
        time.sleep(0.1)                               # TTL expires
        gateway.test_connection_model(conn, "m1")
        self.assertEqual(len(calls), 2)                # a new probe is allowed


class PersistenceTests(_Base):
    """TEST 10 — a fresh shared result survives re-initializing the health
    components against the same Store (e.g. a process restart)."""

    def test_shared_result_survives_recreated_components(self):
        calls = []
        self._patched(calls)
        store = Store(":memory:")
        router, gateway, provider, conn = _stack(store=store)
        router.test_provider_model("groq", "m1")
        self.assertEqual(len(calls), 1)

        # Recreate Provider/Gateway/Coordinator against the SAME store, as a
        # fresh process would.
        provider2 = _provider()
        conn2 = _gw_conn()
        gateway2 = AstraAIGateway(connections=[conn2], store=store)
        router2 = AstraRouter([provider2], gateway=gateway2, store=store)
        result = router2.test_provider_model("groq", "m1")
        self.assertEqual(len(calls), 1)                # no duplicate upstream call
        self.assertTrue(result["ok"])


class IsolationTests(_Base):
    """§13/§15/§16 — local health state, image generation and normal
    routing are all untouched by this dedup layer."""

    def test_local_health_state_is_still_populated_for_both_systems(self):
        calls = []
        self._patched(calls)
        router, gateway, provider, conn = _stack()
        router.test_provider_model("groq", "m1")
        gateway.test_connection_model(conn, "m1")
        # Provider-side per-key health still recorded.
        self.assertTrue(router.key_health("groq")["m1"])
        # Gateway-side routing/health state still recorded.
        self.assertIn("astra-gw-groq", gateway.health())

    def test_image_router_is_unaffected(self):
        router, gateway, provider, conn = _stack()
        self.assertIsNotNone(gateway.image_router)
        self.assertFalse(hasattr(gateway.image_router, "shared_health"))


class SecurityTests(_Base):
    """§18 — no secret ever leaves the shared-health layer."""

    def test_no_secret_or_fingerprint_leaks_into_results_or_repr(self):
        calls = []
        self._patched(calls)
        router, gateway, provider, conn = _stack(
            provider=_provider(key="super-secret-value"),
            conn=_gw_conn(key="super-secret-value"))
        r1 = router.test_provider_model("groq", "m1")
        r2 = gateway.test_connection_model(conn, "m1")
        blob = repr(r1) + repr(r2) + repr(router.health()) + repr(gateway.health())
        self.assertNotIn("super-secret-value", blob)
        # The coordinator's own persisted/cached row never holds the secret
        # or an Authorization-header-shaped value either.
        row = router.shared_health.get_fresh(
            next(iter(router.shared_health._cache.keys())))
        self.assertNotIn("super-secret-value", repr(row))
        self.assertNotIn("Authorization", repr(row))


if __name__ == "__main__":
    unittest.main()
