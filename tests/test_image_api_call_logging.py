# -*- coding: utf-8 -*-
"""Image-generation API-call logging.

The Activity Log must show the ACTUALLY EXECUTED provider API request for
every image-generation attempt -- START -> SUCCESS/FAILED -- using the SAME
existing api-call contract chat/text calls use ("astra_gateway.*" at the
Gateway execution point, "ai.*" at the router/adapter execution point).

These tests drive the real execution path with mocked HTTP (no network) and
assert provider/model/status/duration/reason are present, that each fallback
model produces its OWN call pair, and that no secret or raw base64 image data
ever reaches the log.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.gateway import AstraAIGateway
from astra.core.events import EventBus
from astra.core.exceptions import ProviderError, TimeoutError
from astra.store import Store
from tests.test_image_generation import (DATA_URI, FLUX, GEMINI_IMG,
                                         LIGHTNING, LUCID, OR_DEAD_FREE_IDS,
                                         _FakeConn, _http_error)

GEMINI = ("astra-gw-gemini", "gemini", GEMINI_IMG)
CLOUDFLARE = ("astra-gw-cloudflare", "cloudflare", FLUX)
#: OpenRouter contributes NO routable FREE model (its live catalog was
#: verified 2026-09-26 to contain zero `:free` image models), so it cannot be
#: exercised through the free-pool call contract here; its API contract is
#: covered by tests/test_image_generation.py::TestOpenRouterImagesApi with a
#: mocked HTTP layer.
CLOUDFLARE_LUCID = ("astra-gw-cloudflare", "cloudflare", LUCID)


def _bus():
    return EventBus(Store(":memory:"))


def _events(bus, kind):
    """Persisted events of one kind, oldest first."""
    rows = [e for e in bus.history(limit=300) if e["kind"] == kind]
    return sorted(rows, key=lambda e: e["id"])


def _data(bus, kind):
    return [e["data"] for e in _events(bus, kind)]


class TestGatewayImageApiCallLog(unittest.TestCase):
    """The Gateway image execution point (conn.generate_image -> provider API)
    must emit the existing astra_gateway.* api-call lifecycle."""

    def _gw(self, *conns):
        bus = _bus()
        return AstraAIGateway(connections=list(conns), events=bus), bus

    def test_start_is_logged_for_the_actual_provider_api_call(self):
        cf = _FakeConn(CLOUDFLARE[0], CLOUDFLARE[1], image_models=[FLUX])
        gw, bus = self._gw(cf)
        gw.generate_image("akta cat photo", discover=False, trace="req-1")

        starts = _events(bus, "astra_gateway.request")
        self.assertEqual(len(starts), 1)
        d = starts[0]["data"]
        self.assertEqual(d["provider"], "cloudflare")
        self.assertEqual(d["model"], FLUX)
        self.assertEqual(d["category"], "image_generation")
        self.assertEqual(d["attempt"], 1)
        self.assertEqual(d["trace"], "req-1")
        self.assertIn("akta cat photo", d["input"])
        # START is not a terminal event.
        self.assertNotEqual(d.get("terminal"), True)

    def test_success_logs_provider_model_duration_and_http_status(self):
        cf = _FakeConn(CLOUDFLARE[0], CLOUDFLARE[1], image_models=[FLUX])
        gw, bus = self._gw(cf)
        gw.generate_image("a cat", discover=False, trace="req-2")

        ok = _events(bus, "astra_gateway.success")
        self.assertEqual(len(ok), 1)
        d = ok[0]["data"]
        self.assertEqual(d["provider"], "cloudflare")
        self.assertEqual(d["model"], FLUX)
        self.assertEqual(d["status_code"], 200)
        self.assertIn("duration_ms", d)
        self.assertIn("latency_ms", d)
        self.assertEqual(d["trace"], "req-2")
        self.assertTrue(d["terminal"])
        # The START and the SUCCESS resolve the SAME api call.
        self.assertEqual(_data(bus, "astra_gateway.request")[0]["op"], d["op"])

    def test_failed_call_logs_http_status_duration_and_safe_reason(self):
        cf = _FakeConn(CLOUDFLARE[0], CLOUDFLARE[1],
                       image_models=[FLUX, LIGHTNING],
                       outcomes=[_http_error("u", 429)])
        gw, bus = self._gw(cf)
        gw.generate_image("a cat", discover=False, trace="req-3")

        errs = _events(bus, "astra_gateway.error")
        self.assertEqual(len(errs), 1)
        d = errs[0]["data"]
        self.assertEqual(d["provider"], "cloudflare")
        self.assertEqual(d["model"], FLUX)
        self.assertEqual(d["status_code"], 429)
        self.assertEqual(d["reason"], "429 rate limit")
        self.assertIn("duration_ms", d)
        self.assertTrue(d["terminal"])
        self.assertEqual(_data(bus, "astra_gateway.request")[0]["op"], d["op"])

    def test_every_fallback_model_gets_its_own_call_pair(self):
        gem = _FakeConn(GEMINI[0], GEMINI[1], image_models=[GEMINI_IMG],
                        outcomes=[_http_error("u", 429)])
        cf = _FakeConn(CLOUDFLARE_LUCID[0], CLOUDFLARE_LUCID[1],
                       image_models=[LUCID])
        gw, bus = self._gw(gem, cf)
        gw.generate_image("a cat", discover=False, trace="req-4")

        starts = _data(bus, "astra_gateway.request")
        self.assertEqual([(d["provider"], d["model"]) for d in starts],
                         [("gemini", GEMINI_IMG), ("cloudflare", LUCID)])
        # One api-call op PER attempted model -- not one for the whole request.
        self.assertEqual(len({d["op"] for d in starts}), 2)

        errs = _data(bus, "astra_gateway.error")
        oks = _data(bus, "astra_gateway.success")
        self.assertEqual(len(errs), 1)
        self.assertEqual(len(oks), 1)
        self.assertEqual(errs[0]["op"], starts[0]["op"])
        self.assertEqual(oks[0]["op"], starts[1]["op"])
        self.assertEqual((errs[0]["provider"], errs[0]["status_code"]),
                         ("gemini", 429))
        self.assertEqual((oks[0]["provider"], oks[0]["status_code"]),
                         ("cloudflare", 200))

    def test_all_supported_image_providers_use_the_same_call_contract(self):
        for name, short, model in (GEMINI, CLOUDFLARE, CLOUDFLARE_LUCID):
            with self.subTest(provider=short):
                conn = _FakeConn(name, short, image_models=[model])
                gw, bus = self._gw(conn)
                gw.generate_image("a cat", discover=False)

                start = _data(bus, "astra_gateway.request")
                ok = _data(bus, "astra_gateway.success")
                self.assertEqual(len(start), 1)
                self.assertEqual(len(ok), 1)
                self.assertEqual(
                    (start[0]["provider"], start[0]["model"]), (short, model))
                self.assertEqual(
                    (ok[0]["provider"], ok[0]["model"], ok[0]["status_code"]),
                    (short, model, 200))
                self.assertEqual(start[0]["op"], ok[0]["op"])

    def test_dead_openrouter_ids_never_produce_an_api_call_row(self):
        # OpenRouter's static FREE pool is empty (verified 2026-09-26), so an
        # operator-configured dead id must yield NO provider API call and NO
        # astra_gateway.* row -- never a fabricated call for a model that was
        # not actually requested.
        orc = _FakeConn("astra-gw-openrouter", "openrouter",
                        image_models=list(OR_DEAD_FREE_IDS))
        gw, bus = self._gw(orc)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False, trace="req-dead")
        self.assertEqual(orc.image_calls, [])
        for kind in ("astra_gateway.request", "astra_gateway.success",
                     "astra_gateway.error"):
            self.assertEqual(_data(bus, kind), [], kind)

    def test_network_failure_is_logged_with_a_zero_status_and_reason(self):
        cf = _FakeConn(CLOUDFLARE[0], CLOUDFLARE[1],
                       image_models=[FLUX, LIGHTNING],
                       outcomes=[TimeoutError("timed out")])
        gw, bus = self._gw(cf)
        gw.generate_image("a cat", discover=False)

        err = _data(bus, "astra_gateway.error")[0]
        self.assertEqual(err["status_code"], 0)
        self.assertEqual(err["reason"], "timeout")
        self.assertIn("duration_ms", err)

    def test_api_call_events_never_carry_credentials_or_image_base64(self):
        cf = _FakeConn(CLOUDFLARE[0], CLOUDFLARE[1], image_models=[FLUX])
        gw, bus = self._gw(cf)
        gw.generate_image("a cat", discover=False)
        # The fake connection returned a real data URI; the log must not keep it.
        self.assertTrue(DATA_URI.startswith("data:image/png;base64,"))

        blob = json.dumps([(e["kind"], e["data"]) for e in bus.history(limit=300)])
        for secret in ("base64", "Authorization", "Bearer", "ghp_", "sk-",
                       "x-api-key", DATA_URI[-40:]):
            self.assertNotIn(secret, blob)

    def test_existing_image_lifecycle_events_are_unchanged(self):
        cf = _FakeConn(CLOUDFLARE[0], CLOUDFLARE[1],
                       image_models=[FLUX, LIGHTNING],
                       outcomes=[_http_error("u", 429)])
        gw, bus = self._gw(cf)
        gw.generate_image("a cat", discover=False)
        kinds = {e["kind"] for e in bus.history(limit=300)}
        for kind in ("image.generation.start", "image.generation.attempt",
                     "image.generation.failure", "image.generation.fallback",
                     "image.generation.success"):
            self.assertIn(kind, kinds)




class TestProviderErrorCarriesHttpStatus(unittest.TestCase):
    """Both HTTP stacks attach the real status code so api-call logging can
    report it without parsing message text."""

    def _raised(self, call):
        try:
            call()
        except Exception as e:                # noqa: BLE001 - test probe
            return e
        raise AssertionError("expected an exception")

    def test_gateway_connection_classify_http_attaches_status(self):
        from astra.ai.gateway import _GatewayCompatibleConnection
        conn = _GatewayCompatibleConnection.__new__(_GatewayCompatibleConnection)
        conn.name = "astra-gw-probe"

        err = self._raised(lambda: conn._classify_http(_http_error("u", 429), None))
        self.assertIsInstance(err, ProviderError)
        self.assertEqual(err.code, 429)

        err = self._raised(lambda: conn._classify_http(_http_error("u", 503), None))
        self.assertEqual(err.code, 503)

        err = self._raised(lambda: conn._classify_http(_http_error("u", 408), None))
        self.assertIsInstance(err, TimeoutError)
        self.assertEqual(err.code, 408)

    def test_provider_adapter_classify_http_attaches_status(self):
        from astra.ai.adapters.base import CompatibleAdapter
        adapter = CompatibleAdapter.__new__(CompatibleAdapter)
        adapter.name = "probe"

        err = self._raised(lambda: adapter._classify_http(_http_error("u", 429), None))
        self.assertIsInstance(err, ProviderError)
        self.assertEqual(err.code, 429)

        err = self._raised(lambda: adapter._classify_http(_http_error("u", 404), None))
        self.assertEqual(err.code, 404)

        err = self._raised(lambda: adapter._classify_http(_http_error("u", 408), None))
        self.assertIsInstance(err, TimeoutError)
        self.assertEqual(err.code, 408)


if __name__ == "__main__":
    unittest.main()

