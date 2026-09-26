"""Regression tests for shared-health logging.

A Provider/Gateway health probe may reuse a fresh result without making an
upstream HTTP request. The reuse path must persist local health state but must
not emit a second event that looks like an API call.
"""
from __future__ import annotations

import unittest
from unittest import mock

from astra.ai.adapters.base import CompatibleAdapter
from astra.ai.adapters.groq import GroqAdapter
from astra.ai.gateway import AstraAIGateway, AstraGatewayGroq, _GatewayCompatibleConnection
from astra.ai.router import AstraRouter
from astra.core.config import Config
from astra.store import Store


class _Events:
    def __init__(self):
        self.rows = []

    def emit(self, kind, **data):
        self.rows.append((kind, data))
        return {"kind": kind, "data": data}


def _cfg(**kw):
    cfg = Config()
    cfg._runtime.update(**kw)
    return cfg


def _post(calls):
    def fake(self, url, body, cred):
        secret = self.pool.get_secret_for(cred)
        calls.append((secret, body["model"]))
        return {"choices": [{"message": {"content": "pong"}}]}
    return fake


class SharedHealthLoggingTests(unittest.TestCase):
    def test_reused_gateway_probe_does_not_emit_api_test_event(self):
        calls = []
        events = _Events()
        provider = GroqAdapter(config=_cfg(GROQ_API_KEYS="secret-A", GROQ_MODELS="m1"))
        conn = AstraGatewayGroq(config=_cfg(GW_GROQ_API_KEYS="secret-A", GW_GROQ_MODELS="m1"))
        store = Store(":memory:")
        gateway = AstraAIGateway(connections=[conn], store=store, events=events)
        router = AstraRouter([provider], gateway=gateway, store=store)

        with mock.patch.object(CompatibleAdapter, "_post", _post(calls)), \
             mock.patch.object(_GatewayCompatibleConnection, "_post", _post(calls)):
            first = router.test_provider_model("groq", "m1")
            reused = gateway.test_connection_model(conn, "m1")

        self.assertTrue(first["ok"])
        self.assertTrue(reused["reused"])
        self.assertEqual(len(calls), 1)
        self.assertEqual([k for k, _ in events.rows if k == "astra_gateway.test"], [])


    def test_shared_result_overrides_older_gateway_health(self):
        """A fresh Provider result must survive Gateway refresh even when an
        older direct Gateway probe already exists for the same model."""
        from astra.ai.gateway import AstraAIGateway, AstraGatewayGroq
        from astra.ai.shared_health import SharedHealthIdentity
        conn = AstraGatewayGroq(config=_cfg(GW_GROQ_API_KEYS="secret-A", GW_GROQ_MODELS="m1"))
        store = Store(":memory:")
        gateway = AstraAIGateway(connections=[conn], store=store, events=_Events())

        gateway.routing_state.record_success("groq", "m1", 9999.0)
        gateway.shared_health._save_locked(
            SharedHealthIdentity("groq", "m1"),
            {"ok": False, "error": "provider-failed", "latency_ms": 123.0},
        )
        # The saved shared row has a fresh timestamp and must be reflected in
        # the Gateway health payload instead of the older local success.
        health = gateway.health()["astra-gw-groq"]["model_health"]["m1"]
        self.assertEqual(health["failure_count"], 1)
        self.assertEqual(health["last_failure"] != "", True)

    def test_real_gateway_probe_still_emits_test_event(self):
        calls = []
        events = _Events()
        conn = AstraGatewayGroq(config=_cfg(GW_GROQ_API_KEYS="secret-A", GW_GROQ_MODELS="m1"))
        gateway = AstraAIGateway(connections=[conn], store=Store(":memory:"), events=events)

        with mock.patch.object(_GatewayCompatibleConnection, "_post", _post(calls)):
            result = gateway.test_connection_model(conn, "m1")

        self.assertTrue(result["ok"])
        self.assertFalse(result["reused"])
        self.assertEqual(len(calls), 1)
        tests = [d for k, d in events.rows if k == "astra_gateway.test"]
        self.assertEqual(len(tests), 1)
        self.assertFalse(tests[0]["reused"])


if __name__ == "__main__":
    unittest.main()
