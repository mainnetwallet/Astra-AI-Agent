# -*- coding: utf-8 -*-
"""Focused regression tests: IMAGE_* / GW_* credential isolation.

Pins the required behavior (see task):

  IMAGE_* configured  -> image provider eligible
  IMAGE_* missing     -> provider SKIPPED
  IMAGE_* missing, GW_* configured -> NEVER use GW_* for image generation

Covers, one test class per scenario:
  1. IMAGE_* configured -> image provider eligible
  2. IMAGE_* missing + GW_* configured -> provider skipped (not eligible,
     and a direct call raises cleanly with NO HTTP request sent)
  3. Multiple image providers -> only the ones with dedicated IMAGE_*
     credentials are eligible
  4. Normal chat is unaffected -- it still uses GW_* credentials
  5. No image credential anywhere -> clean configuration error, no crash

No test performs a real network call (urllib is patched).
"""
from __future__ import annotations

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
from astra.core.exceptions import ProviderError

FLUX = "@cf/black-forest-labs/flux-1-schnell"
GEMINI_IMG = "gemini-2.5-flash-image"
OR_LIVE_FREE = "example/discovered-free-image:free"


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


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(b"{}"))


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


# ── 1. IMAGE_* configured -> eligible ────────────────────────────────────
class TestImageConfiguredIsEligible(unittest.TestCase):
    def test_gemini_eligible_with_dedicated_key(self):
        conn = AstraGatewayGemini(_cfg(
            GW_GEMINI_API_KEYS="chat-key", GEMINI_IMAGE_MODELS=GEMINI_IMG,
            IMAGE_GEMINI_API_KEY="image-key"))
        self.assertTrue(conn.image_credentials_configured())

    def test_openrouter_eligible_with_dedicated_key(self):
        conn = AstraGatewayOpenRouter(_cfg(
            GW_OPENROUTER_API_KEYS="chat-key",
            IMAGE_OPENROUTER_API_KEY="image-key"))
        self.assertTrue(conn.image_credentials_configured())

    def test_cloudflare_eligible_with_dedicated_key_and_account(self):
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            IMAGE_CLOUDFLARE_API_KEY="image-token",
            IMAGE_CLOUDFLARE_ACCOUNT_ID="image-acct"))
        self.assertTrue(conn.image_credentials_configured())

    def test_cloudflare_not_eligible_with_key_but_no_dedicated_account(self):
        # Both dedicated pieces are required -- a dedicated key alone,
        # still riding the chat account id, must NOT be eligible.
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            IMAGE_CLOUDFLARE_API_KEY="image-token"))
        self.assertFalse(conn.image_credentials_configured())


# ── 2. IMAGE_* missing + GW_* configured -> provider SKIPPED ─────────────
class TestImageMissingSkipsProviderNeverFallsBack(unittest.TestCase):
    def _assert_skipped_no_request(self, conn, **generate_kwargs):
        self.assertFalse(conn.image_credentials_configured())
        behavior = lambda req, n: _Resp(json.dumps(
            {"result": {"image": "AA=="}, "success": True,
             "data": [{"b64_json": "AA=="}],
             "candidates": [{"content": {"parts": [
                 {"inlineData": {"mimeType": "image/png", "data": "AA=="}}]}}]}))
        with _Capture(behavior) as cap:
            with self.assertRaises(ProviderError):
                conn.generate_image("a cat", **generate_kwargs)
        self.assertEqual(cap.count, 0,
                          "provider must be skipped, never attempted")

    def test_gemini(self):
        # IMAGE_GEMINI_API_KEY=  (missing)  /  GW_GEMINI_API_KEYS=... (set)
        conn = AstraGatewayGemini(_cfg(
            GW_GEMINI_API_KEYS="chat-key", GEMINI_IMAGE_MODELS=GEMINI_IMG))
        self._assert_skipped_no_request(conn, model=GEMINI_IMG)

    def test_openrouter(self):
        # IMAGE_OPENROUTER_API_KEY=  (missing) / GW_OPENROUTER_API_KEYS=...
        conn = AstraGatewayOpenRouter(_cfg(GW_OPENROUTER_API_KEYS="chat-key"))
        self._assert_skipped_no_request(conn, model=OR_LIVE_FREE)

    def test_cloudflare(self):
        # IMAGE_CLOUDFLARE_API_KEY= / IMAGE_CLOUDFLARE_ACCOUNT_ID= (missing)
        # GW_CLOUDFLARE_API_KEYS=... / GW_CLOUDFLARE_ACCOUNT_IDS=... (set)
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct"))
        self._assert_skipped_no_request(conn, model=FLUX)

    def test_cloudflare_dedicated_key_alone_never_borrows_chat_account(self):
        # A dedicated IMAGE_CLOUDFLARE_API_KEY with no dedicated account id
        # must still be skipped -- never runs against GW_CLOUDFLARE_ACCOUNT_IDS.
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            IMAGE_CLOUDFLARE_API_KEY="image-token"))
        self._assert_skipped_no_request(conn, model=FLUX)

    def test_rejected_dedicated_image_key_still_never_touches_chat_pool(self):
        """A configured-but-bad IMAGE_* key fails on its own; the chat pool
        must stay untouched (isolation holds in both directions)."""
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            IMAGE_CLOUDFLARE_API_KEY="bad-image-token",
            IMAGE_CLOUDFLARE_ACCOUNT_ID="image-acct"))
        behavior = lambda req, n: _http_error(req.full_url, 401)
        with _Capture(behavior):
            with self.assertRaises(ProviderError):
                conn.generate_image("a cat", model=FLUX)
        self.assertFalse(bool(conn.image_pool))
        self.assertTrue(bool(conn.pool))


# ── 3. Multiple image providers -> only dedicated-IMAGE_* ones eligible ──
class TestMultipleProvidersOnlyDedicatedEligible(unittest.TestCase):
    def test_only_gemini_has_dedicated_key(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_GEMINI_API_KEYS="chat-key-gemini",
            GEMINI_IMAGE_MODELS=GEMINI_IMG,
            IMAGE_GEMINI_API_KEY="image-key-gemini",
            GW_CLOUDFLARE_API_KEYS="chat-key-cf",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct-cf",
            CLOUDFLARE_IMAGE_MODELS=FLUX,
            GW_OPENROUTER_API_KEYS="chat-key-or"))
        targets = gw.image_targets(discover=False)
        connection_names = {conn.name for conn, _model, _health in targets}
        self.assertIn("astra-gw-gemini", connection_names)
        self.assertNotIn("astra-gw-cloudflare", connection_names)
        self.assertNotIn("astra-gw-openrouter", connection_names)

    def test_gemini_and_cloudflare_both_have_dedicated_keys(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_GEMINI_API_KEYS="chat-key-gemini",
            GEMINI_IMAGE_MODELS=GEMINI_IMG,
            IMAGE_GEMINI_API_KEY="image-key-gemini",
            GW_CLOUDFLARE_API_KEYS="chat-key-cf",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct-cf",
            CLOUDFLARE_IMAGE_MODELS=FLUX,
            IMAGE_CLOUDFLARE_API_KEY="image-key-cf",
            IMAGE_CLOUDFLARE_ACCOUNT_ID="image-acct-cf",
            GW_OPENROUTER_API_KEYS="chat-key-or"))
        targets = gw.image_targets(discover=False)
        connection_names = {conn.name for conn, _model, _health in targets}
        self.assertIn("astra-gw-gemini", connection_names)
        self.assertIn("astra-gw-cloudflare", connection_names)
        self.assertNotIn("astra-gw-openrouter", connection_names)


# ── 4. Normal chat still uses GW_* credentials, unaffected ──────────────
class TestNormalChatUnaffected(unittest.TestCase):
    def test_gemini_chat_uses_gw_key_with_no_image_key_configured(self):
        conn = AstraGatewayGemini(_cfg(GW_GEMINI_API_KEYS="chat-key"))
        behavior = lambda req, n: _Resp(json.dumps(
            {"choices": [{"message": {"content": "hello"}}]}))
        with _Capture(behavior) as cap:
            text = conn.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "hello")
        self.assertEqual(cap.requests[0]["headers"].get("authorization"),
                         "Bearer chat-key")

    def test_gemini_chat_uses_gw_key_even_with_dedicated_image_key_set(self):
        # Presence of a totally separate IMAGE_* credential must not change
        # normal chat routing/credentials at all.
        conn = AstraGatewayGemini(_cfg(
            GW_GEMINI_API_KEYS="chat-key", IMAGE_GEMINI_API_KEY="image-key"))
        behavior = lambda req, n: _Resp(json.dumps(
            {"choices": [{"message": {"content": "hello"}}]}))
        with _Capture(behavior) as cap:
            conn.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cap.requests[0]["headers"].get("authorization"),
                         "Bearer chat-key")


# ── 5. No image credential anywhere -> clean configuration error ────────
class TestNoImageCredentialCleanError(unittest.TestCase):
    def test_image_targets_empty_without_any_image_credential(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_GEMINI_API_KEYS="chat-key", GEMINI_IMAGE_MODELS=GEMINI_IMG,
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            CLOUDFLARE_IMAGE_MODELS=FLUX))
        self.assertEqual(gw.image_targets(discover=False), [])

    def test_generate_image_raises_clean_provider_error_not_a_crash(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_GEMINI_API_KEYS="chat-key", GEMINI_IMAGE_MODELS=GEMINI_IMG))
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("image", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
