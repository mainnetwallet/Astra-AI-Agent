# -*- coding: utf-8 -*-
"""Image-provider availability: the REAL API contract of every supported
image provider, verified against the code that actually issues the request.

These pin the things a "provider unavailable" report must not be confused
with:
  * Cloudflare - account id + API token loading, the Workers AI
    ``/accounts/<id>/ai/run/<model>`` endpoint, the Bearer header, and the
    fact that a rejected token (HTTP 401) is a CONNECTION/CREDENTIAL failure
    and never a per-model one;
  * Gemini - a 429 stays a provider rate-limit failure (no text/vision
    fallback);
  * OpenRouter - a stale/nonexistent image id stays "model unavailable" and
    is never silently replaced by a paid model.

No test performs a real network call (urllib is patched).
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.gateway import (AstraGatewayCloudflare, AstraGatewayGemini,
                              AstraGatewayOpenRouter, build_astra_ai_gateway)
from astra.core.config import Config
from astra.core.events import EventBus
from astra.core.exceptions import ProviderError, TimeoutError
from astra.store import Store

FLUX = "@cf/black-forest-labs/flux-1-schnell"
LUCID = "@cf/leonardo/lucid-origin"
GEMINI_IMG = "gemini-2.5-flash-image"
OR_FREE = ("black-forest-labs/flux-1-schnell:free",
           "google/gemini-2.5-flash-image-preview:free",
           "sourceful/riverflow-v2.5-pro:free")
#: OpenRouter image models that are NOT free -- must never be substituted in.
OR_PAID = ("google/gemini-2.5-flash-image", "google/gemini-3-pro-image",
           "openai/gpt-5-image")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
B64 = base64.b64encode(PNG).decode()
DATA_URI = "data:image/png;base64," + B64


def _cfg(**env):
    c = Config()
    for k, v in env.items():
        c._runtime[k] = v
    return c


class _Resp:
    def __init__(self, body, ctype="application/json"):
        self._b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _http_error(url, code, reason="err"):
    return urllib.error.HTTPError(url, code, reason, {},
                                  io.BytesIO(b'{"success":false,"errors":'
                                             b'[{"code":10000,'
                                             b'"message":"Authentication error"}]}'))


class _Capture:
    """Records every request the code under test actually sends."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.requests = []

    def __enter__(self):
        self._patch = mock.patch("urllib.request.urlopen", self._side)
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()
        return False

    def _side(self, req, timeout=None):
        self.requests.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": json.loads(req.data.decode()) if req.data else None,
        })
        out = self.behavior(req, len(self.requests))
        if isinstance(out, Exception):
            raise out
        return out

    @property
    def count(self):
        return len(self.requests)


CF_ENV = dict(GW_CLOUDFLARE_API_KEYS="cf-token-under-test",
              GW_CLOUDFLARE_ACCOUNT_IDS="acct-1111",
              GW_CLOUDFLARE_IMAGE_MODELS=FLUX + "," + LUCID)
GEMINI_ENV = dict(GW_GEMINI_API_KEYS="gem-key-under-test",
                  GW_GEMINI_IMAGE_MODELS=GEMINI_IMG)
OR_ENV = dict(GW_OPENROUTER_API_KEYS="or-key-under-test",
              GW_OPENROUTER_IMAGE_MODELS=",".join(OR_FREE))


def _bus():
    return EventBus(Store(":memory:"))


def _data(bus, kind):
    rows = [e for e in bus.history(limit=400) if e["kind"] == kind]
    return [e["data"] for e in sorted(rows, key=lambda e: e["id"])]


class TestCloudflareImageApiContract(unittest.TestCase):
    """Verify: credential loading, account id, endpoint and request format."""

    def test_credentials_and_account_ids_are_loaded_by_the_image_adapter(self):
        gw = build_astra_ai_gateway(_cfg(**CF_ENV))
        conn = [c for c in gw.connections if c.name == "astra-gw-cloudflare"]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].pool.count, 1)          # key loaded
        self.assertEqual(conn[0]._accounts, ["acct-1111"])  # account loaded
        # the image pool comes from the GW_* image env, not the chat env
        self.assertEqual(sorted(conn[0].image_models), sorted([FLUX, LUCID]))
        targets = [(t[0].name, t[1].model_id) for t in gw.image_targets(
            discover=False) if t[0].name == "astra-gw-cloudflare"]
        self.assertEqual(sorted(m for _n, m in targets), sorted([FLUX, LUCID]))

    def test_no_cloudflare_connection_without_a_gateway_api_key(self):
        # Provider-style CLOUDFLARE_* vars are a DIFFERENT system and must
        # never make the Gateway image path look configured.
        gw = build_astra_ai_gateway(_cfg(
            CLOUDFLARE_API_KEYS="provider-only-token",
            CLOUDFLARE_ACCOUNT_IDS="acct-1111",
            CLOUDFLARE_IMAGE_MODELS=FLUX))
        self.assertEqual([c for c in gw.connections
                          if c.name == "astra-gw-cloudflare"], [])

    def test_request_is_the_documented_workers_ai_image_endpoint(self):
        gw = build_astra_ai_gateway(_cfg(**CF_ENV))
        behavior = lambda req, n: _Resp(json.dumps({"result": {"image": B64},
                                                    "success": True}))
        with _Capture(behavior) as cap:
            uri = gw.generate_image("a cat photo", model=FLUX, discover=False)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(cap.count, 1)
        r = cap.requests[0]
        self.assertEqual(
            r["url"],
            "https://api.cloudflare.com/client/v4/accounts/acct-1111"
            "/ai/run/@cf/black-forest-labs/flux-1-schnell")
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["headers"].get("authorization"),
                         "Bearer cf-token-under-test")
        # flux-1-schnell's published schema accepts prompt only.
        self.assertEqual(r["body"], {"prompt": "a cat photo"})

    def test_a_known_good_model_with_the_same_credential_succeeds(self):
        gw = build_astra_ai_gateway(_cfg(**CF_ENV))
        behavior = lambda req, n: _Resp(json.dumps({"result": {"image": B64},
                                                    "success": True}))
        with _Capture(behavior):
            uri = gw.generate_image("a cat", model=FLUX, discover=False)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, FLUX)

    def test_rejected_token_is_a_credential_error_with_the_real_status(self):
        bus = _bus()
        gw = build_astra_ai_gateway(_cfg(**CF_ENV), events=bus)
        behavior = lambda req, n: _http_error(req.full_url, 401)
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError):
                gw.generate_image("a cat", model=FLUX, discover=False)
        self.assertEqual(cap.count, 1)
        err = _data(bus, "astra_gateway.error")[0]
        self.assertEqual(err["status_code"], 401)
        self.assertEqual(err["reason"],
                         "provider authentication/credential failure")


class TestCredentialFailureIsNeverPerModel(unittest.TestCase):
    """A dead token must not be reported as each model failing on its own."""

    CF7 = (FLUX + "," + "@cf/stabilityai/stable-diffusion-xl-base-1.0,"
           "@cf/bytedance/stable-diffusion-xl-lightning,"
           "@cf/lykon/dreamshaper-8-lcm,"
           "@cf/runwayml/stable-diffusion-v1-5-inpainting," + LUCID + ","
           "@cf/leonardo/phoenix-1.0")

    def _run(self, behavior):
        bus = _bus()
        gw = build_astra_ai_gateway(_cfg(
            GW_CLOUDFLARE_API_KEYS="rejected-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="acct-1111",
            GW_CLOUDFLARE_IMAGE_MODELS=self.CF7), events=bus)
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError):
                gw.generate_image("a cat", discover=False)
        return gw, bus, cap

    def test_every_model_reports_the_real_401_not_a_phantom_failure(self):
        gw, bus, cap = self._run(lambda req, n: _http_error(req.full_url, 401))
        # routing is untouched: all 7 models are still attempted, in order,
        # but the provider is only really called once.
        self.assertEqual(gw.last_attempts, 7)
        self.assertEqual(cap.count, 1)

        starts = _data(bus, "astra_gateway.request")
        errors = _data(bus, "astra_gateway.error")
        self.assertEqual(len(starts), 7)
        self.assertEqual(len(errors), 7)
        # Every reported failure is the SAME real credential rejection.
        for d in errors:
            self.assertEqual(d["status_code"], 401)
            self.assertEqual(d["reason"],
                             "provider authentication/credential failure")
        # The models after the first were never sent, so they must not claim a
        # status of their own (0 would imply "unknown model failure").
        self.assertTrue(all(d["status_code"] != 0 for d in errors))

    def test_cooled_down_pool_still_keeps_the_real_rejection(self):
        # A single 401 must be enough: the cached rejection survives the
        # subsequent pick() returning nothing.
        gw, bus, cap = self._run(lambda req, n: _http_error(req.full_url, 403))
        for d in _data(bus, "astra_gateway.error"):
            self.assertEqual(d["status_code"], 403)
            self.assertEqual(d["reason"],
                             "provider authentication/credential failure")
        self.assertEqual(cap.count, 1)

    def test_no_healthy_credential_message_is_not_used_as_a_model_failure(self):
        gw, bus, cap = self._run(lambda req, n: _http_error(req.full_url, 401))
        blob = json.dumps(_data(bus, "astra_gateway.error"))
        self.assertNotIn("no healthy credential configured", blob)


class TestGeminiRateLimitIsPreserved(unittest.TestCase):
    def test_429_stays_a_provider_rate_limit_failure(self):
        bus = _bus()
        gw = build_astra_ai_gateway(_cfg(**GEMINI_ENV), events=bus)
        behavior = lambda req, n: _http_error(req.full_url, 429)
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError):
                gw.generate_image("a cat", discover=False)
        self.assertEqual(cap.count, 1)
        self.assertIn("/models/gemini-2.5-flash-image:generateContent",
                      cap.requests[0]["url"])
        err = _data(bus, "astra_gateway.error")[0]
        self.assertEqual(err["status_code"], 429)
        self.assertEqual(err["reason"], "429 rate limit")
        self.assertEqual(err["model"], GEMINI_IMG)

    def test_rate_limit_never_falls_back_to_a_text_or_vision_model(self):
        bus = _bus()
        gw = build_astra_ai_gateway(_cfg(**GEMINI_ENV), events=bus)
        behavior = lambda req, n: _http_error(req.full_url, 429)
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError):
                gw.generate_image("a cat", discover=False)
        # the ONLY request is the image API; no chat/vision call is ever made
        self.assertTrue(all(":generateContent" in r["url"] for r in cap.requests))
        self.assertTrue(all("chat/completions" not in r["url"]
                            for r in cap.requests))


class TestOpenRouterUnavailableModelIsNotReplaced(unittest.TestCase):
    def test_unknown_model_id_is_model_unavailable_with_404(self):
        bus = _bus()
        gw = build_astra_ai_gateway(_cfg(**OR_ENV), events=bus)
        behavior = lambda req, n: _http_error(req.full_url, 404)
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError):
                gw.generate_image("a cat", discover=False)
        # every configured FREE id was really tried, at /images
        self.assertEqual(cap.count, len(OR_FREE))
        self.assertTrue(all(r["url"].endswith("/images") for r in cap.requests))
        self.assertEqual(sorted(r["body"]["model"] for r in cap.requests),
                         sorted(OR_FREE))
        for d in _data(bus, "astra_gateway.error"):
            self.assertEqual(d["status_code"], 404)
            self.assertEqual(d["reason"], "model unavailable")

    def test_paid_model_is_never_substituted_for_a_free_one(self):
        gw = build_astra_ai_gateway(_cfg(**OR_ENV))
        or_targets = [t[1].model_id for t in gw.image_targets(discover=False)
                      if t[0].name == "astra-gw-openrouter"]
        self.assertEqual(sorted(or_targets), sorted(OR_FREE))
        for paid in OR_PAID:
            self.assertNotIn(paid, or_targets)


class TestProviderAdapterCredentialFailure(unittest.TestCase):
    """The router/adapter image path must attribute a dead key to the
    provider, not to each model."""

    def _adapter(self, **env):
        from astra.ai.adapters.cloudflare import CloudflareAdapter
        return CloudflareAdapter(config=_cfg(**env))

    def test_adapter_reports_the_real_status_and_reuses_it(self):
        a = self._adapter(CLOUDFLARE_API_KEYS="rejected",
                          CLOUDFLARE_ACCOUNT_IDS="acct-1111",
                          CLOUDFLARE_IMAGE_MODELS=FLUX + "," + LUCID)
        behavior = lambda req, n: _http_error(req.full_url, 401)
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError) as first:
                a.generate_image("a cat", model=FLUX)
            self.assertEqual(first.exception.code, 401)
            # Second model, same dead key: the pool is now unusable, and the
            # error must still be the real rejection -- with its status.
            with self.assertRaises(ProviderError) as second:
                a.generate_image("a cat", model=LUCID)
            self.assertEqual(second.exception.code, 401)
            self.assertNotIn("no healthy credential configured",
                             str(second.exception))
            self.assertFalse(second.exception.retryable)
        self.assertEqual(cap.count, 1)   # the 2nd call never hit the network

    def test_router_image_path_logs_the_credential_failure_per_model(self):
        from astra.ai.router import AstraRouter, RoutingRequest
        a = self._adapter(CLOUDFLARE_API_KEYS="rejected",
                          CLOUDFLARE_ACCOUNT_IDS="acct-1111",
                          CLOUDFLARE_IMAGE_MODELS=FLUX + "," + LUCID)
        bus = _bus()
        router = AstraRouter([a], max_retries=0)
        router.attach_events(bus)
        behavior = lambda req, n: _http_error(req.full_url, 401)
        with _Capture(behavior) as cap:
            rr = router.route_request(RoutingRequest(
                task_type="image_generation",
                messages=[{"role": "user", "content": "akta cat photo banao"}]))
        self.assertFalse(rr.ok)
        self.assertEqual(cap.count, 1)
        failed = [d for d in _data(bus, "ai.failed")
                  if not d.get("aggregate")]
        self.assertEqual(len(failed), 2)
        for d in failed:
            self.assertEqual(d["status_code"], 401)
            self.assertEqual(d["reason"],
                             "provider authentication/credential failure")

    def test_no_key_configured_still_fails_fast(self):
        # Unrelated to auth: an EMPTY pool keeps the original generic
        # non-retryable error (no real rejection to report).
        from astra.ai.adapters.cloudflare import CloudflareAdapter
        a = CloudflareAdapter(config=_cfg(CLOUDFLARE_ACCOUNT_IDS="acct-1111"))
        with self.assertRaises(ProviderError) as cm:
            a.generate_image("a cat", model=FLUX)
        self.assertFalse(cm.exception.retryable)
        self.assertIn("no healthy credential configured", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
