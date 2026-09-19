"""Model ids with '/', '@' or ':' (Groq, OpenRouter, Cloudflare) are sent
percent-encoded by the UI; the router must decode each path segment so it sees
the real model id, not e.g. 'openai%2Fgpt-oss-120b'.

This is also a regression test for the ASGI bridge: uvicorn percent-decodes
`scope["path"]`, so an encoded "/" would arrive as a real path separator
unless the bridge reads `scope["raw_path"]` instead.
"""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from astra.web import split_path

from tests.helpers import LiveServer, make_stack


class PathDecodingTests(unittest.TestCase):
    def test_slash_model_id_stays_one_segment(self):
        self.assertEqual(
            split_path("/api/v1/providers/groq/test/openai%2Fgpt-oss-120b")[-1],
            "openai/gpt-oss-120b")

    def test_cloudflare_at_prefix(self):
        p = split_path("/api/v1/gateway/cloudflare/test/"
                       "%40cf%2Fmeta%2Fllama-3.3-70b-instruct-fp8-fast")
        self.assertEqual(len(p), 6)
        self.assertEqual(p[-1], "@cf/meta/llama-3.3-70b-instruct-fp8-fast")

    def test_colon_suffix(self):
        self.assertEqual(
            split_path("/api/v1/providers/openrouter/test/minimax%2Fminimax-m3%3Afree")[-1],
            "minimax/minimax-m3:free")

    def test_plain_ids_unchanged(self):
        self.assertEqual(
            split_path("/api/v1/providers/gemini/test/gemini-3.5-flash")[-1],
            "gemini-3.5-flash")


class LivePathDecodingTests(unittest.TestCase):
    """The real uvicorn server must not turn an encoded "/" into a route
    separator before the router sees it."""

    def setUp(self):
        self.srv = LiveServer(stack=make_stack())

    def tearDown(self):
        self.srv.stop()

    def _error(self, path):
        try:
            with urllib.request.urlopen(self.srv.base + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_encoded_separator_stays_in_one_segment(self):
        # "abc%2Fdef" is a single (decoded) segment, so the router reaches the
        # transactions handler with tx_id "abc/def" and reports it missing.
        status, body = self._error("/api/v1/web3/transactions/abc%2Fdef")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "transaction not found")

    def test_literal_separator_is_two_segments(self):
        # the same characters sent unencoded are 5 path segments — a route the
        # router does not have, so this must be a plain "unknown route"
        status, body = self._error("/api/v1/web3/transactions/abc/def")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown route")


if __name__ == "__main__":
    unittest.main()
