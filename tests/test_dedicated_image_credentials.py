# -*- coding: utf-8 -*-
"""Dedicated image-generation credentials (IMAGE_* env vars).

Image generation now reads its OWN credentials/base URL/account id, entirely
separate from the connection's normal chat GW_* pool:

  IMAGE_CLOUDFLARE_API_KEY / IMAGE_CLOUDFLARE_ACCOUNT_ID / IMAGE_CLOUDFLARE_BASE_URL
  IMAGE_OPENROUTER_API_KEY / IMAGE_OPENROUTER_BASE_URL
  IMAGE_GEMINI_API_KEY / IMAGE_GEMINI_BASE_URL

These tests pin:
  * when an IMAGE_* var IS set, the actual HTTP request uses it (never the
    chat GW_* value);
  * when an IMAGE_* var is left unset, image generation explicitly falls
    back to the connection's chat GW_* credential/base URL/account id
    (documented fallback, not a crash / not "unavailable");
  * the two credential pools are isolated: a rejected (401) dedicated image
    key does not mark the connection's chat pool unhealthy, and vice versa.

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
from astra.core.exceptions import ProviderError

FLUX = "@cf/black-forest-labs/flux-1-schnell"
GEMINI_IMG = "gemini-2.5-flash-image"
OR_FREE_ONE = "black-forest-labs/flux-1-schnell:free"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
B64 = base64.b64encode(PNG).decode()


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


class TestCloudflareDedicatedImageCredentials(unittest.TestCase):
    def test_dedicated_image_key_account_and_base_url_are_used(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            GW_CLOUDFLARE_IMAGE_MODELS=FLUX,
            IMAGE_CLOUDFLARE_API_KEY="image-token",
            IMAGE_CLOUDFLARE_ACCOUNT_ID="image-acct",
            IMAGE_CLOUDFLARE_BASE_URL="https://image.example.com/client/v4"))
        behavior = lambda req, n: _Resp(json.dumps(
            {"result": {"image": B64}, "success": True}))
        with _Capture(behavior) as cap:
            uri = gw.generate_image("a cat", model=FLUX, discover=False)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(cap.count, 1)
        r = cap.requests[0]
        self.assertEqual(
            r["url"],
            "https://image.example.com/client/v4/accounts/image-acct"
            "/ai/run/@cf/black-forest-labs/flux-1-schnell")
        self.assertEqual(r["headers"].get("authorization"),
                         "Bearer image-token")

    def test_missing_image_vars_fall_back_to_chat_credentials(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            GW_CLOUDFLARE_IMAGE_MODELS=FLUX))
        behavior = lambda req, n: _Resp(json.dumps(
            {"result": {"image": B64}, "success": True}))
        with _Capture(behavior) as cap:
            gw.generate_image("a cat", model=FLUX, discover=False)
        r = cap.requests[0]
        self.assertEqual(
            r["url"],
            "https://api.cloudflare.com/client/v4/accounts/chat-acct"
            "/ai/run/@cf/black-forest-labs/flux-1-schnell")
        self.assertEqual(r["headers"].get("authorization"), "Bearer chat-token")

    def test_rejected_dedicated_image_key_does_not_degrade_the_chat_pool(self):
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct",
            IMAGE_CLOUDFLARE_API_KEY="bad-image-token",
            IMAGE_CLOUDFLARE_ACCOUNT_ID="image-acct"))
        self.assertIsNot(conn.pool, conn.image_pool)
        behavior = lambda req, n: _http_error(req.full_url, 401)
        with _Capture(behavior):
            with self.assertRaises(ProviderError):
                conn.generate_image("a cat", model=FLUX)
        # the dedicated image pool took the hit ...
        self.assertFalse(bool(conn.image_pool))
        # ... but the separate chat pool is untouched.
        self.assertTrue(bool(conn.pool))


class TestGeminiDedicatedImageCredentials(unittest.TestCase):
    def test_dedicated_image_key_and_base_url_are_used(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_GEMINI_API_KEYS="chat-key",
            GW_GEMINI_IMAGE_MODELS=GEMINI_IMG,
            IMAGE_GEMINI_API_KEY="image-key",
            IMAGE_GEMINI_BASE_URL="https://image.example.com/v1beta/openai"))
        behavior = lambda req, n: _Resp(json.dumps({"candidates": [{
            "content": {"parts": [{"inlineData": {
                "mimeType": "image/png", "data": B64}}]}}]}))
        with _Capture(behavior) as cap:
            uri = gw.generate_image("a cat", model=GEMINI_IMG, discover=False)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        r = cap.requests[0]
        self.assertIn("https://image.example.com/v1beta/models/"
                      "gemini-2.5-flash-image:generateContent", r["url"])
        self.assertEqual(r["headers"].get("x-goog-api-key"), "image-key")

    def test_missing_image_vars_fall_back_to_chat_key(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_GEMINI_API_KEYS="chat-key",
            GW_GEMINI_IMAGE_MODELS=GEMINI_IMG))
        behavior = lambda req, n: _Resp(json.dumps({"candidates": [{
            "content": {"parts": [{"inlineData": {
                "mimeType": "image/png", "data": B64}}]}}]}))
        with _Capture(behavior) as cap:
            gw.generate_image("a cat", model=GEMINI_IMG, discover=False)
        self.assertEqual(cap.requests[0]["headers"].get("x-goog-api-key"),
                         "chat-key")


class TestOpenRouterDedicatedImageCredentials(unittest.TestCase):
    def test_dedicated_image_key_and_base_url_are_used(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_OPENROUTER_API_KEYS="chat-key",
            GW_OPENROUTER_IMAGE_MODELS=OR_FREE_ONE,
            IMAGE_OPENROUTER_API_KEY="image-key",
            IMAGE_OPENROUTER_BASE_URL="https://image.example.com/api/v1"))
        behavior = lambda req, n: _Resp(json.dumps(
            {"data": [{"b64_json": B64}]}))
        with _Capture(behavior) as cap:
            uri = gw.generate_image("a cat", model=OR_FREE_ONE, discover=False)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        r = cap.requests[0]
        self.assertEqual(r["url"], "https://image.example.com/api/v1/images")
        self.assertEqual(r["headers"].get("authorization"), "Bearer image-key")

    def test_missing_image_vars_fall_back_to_chat_key_and_base_url(self):
        gw = build_astra_ai_gateway(_cfg(
            GW_OPENROUTER_API_KEYS="chat-key",
            GW_OPENROUTER_IMAGE_MODELS=OR_FREE_ONE))
        behavior = lambda req, n: _Resp(json.dumps(
            {"data": [{"b64_json": B64}]}))
        with _Capture(behavior) as cap:
            gw.generate_image("a cat", model=OR_FREE_ONE, discover=False)
        r = cap.requests[0]
        self.assertEqual(r["url"], "https://openrouter.ai/api/v1/images")
        self.assertEqual(r["headers"].get("authorization"), "Bearer chat-key")


class TestImagePoolFallsBackToChatPoolObject(unittest.TestCase):
    """When no IMAGE_* credential is configured, `image_pool` IS the chat
    pool object (not a copy) -- confirms there is truly no separate,
    silently-empty pool masking the fallback."""

    def test_cloudflare_image_pool_is_chat_pool_without_image_key(self):
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="chat-token",
            GW_CLOUDFLARE_ACCOUNT_IDS="chat-acct"))
        self.assertIs(conn.image_pool, conn.pool)

    def test_gemini_image_pool_is_chat_pool_without_image_key(self):
        conn = AstraGatewayGemini(_cfg(GW_GEMINI_API_KEYS="chat-key"))
        self.assertIs(conn.image_pool, conn.pool)

    def test_openrouter_image_pool_is_chat_pool_without_image_key(self):
        conn = AstraGatewayOpenRouter(_cfg(GW_OPENROUTER_API_KEYS="chat-key"))
        self.assertIs(conn.image_pool, conn.pool)


if __name__ == "__main__":
    unittest.main()
