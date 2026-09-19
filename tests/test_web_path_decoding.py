"""Model ids with '/', '@' or ':' (Groq, OpenRouter, Cloudflare) are sent
percent-encoded by the UI; the handler must decode each path segment so the
router sees the real model id, not e.g. 'openai%2Fgpt-oss-120b'."""
from __future__ import annotations

import unittest

from astra.web import AstraHandler


def _parts(path: str) -> list[str]:
    h = AstraHandler.__new__(AstraHandler)  # skip socket setup
    h.path = path
    return h._path_parts()


class PathDecodingTests(unittest.TestCase):
    def test_slash_model_id_stays_one_segment(self):
        self.assertEqual(
            _parts("/api/v1/providers/groq/test/openai%2Fgpt-oss-120b")[-1],
            "openai/gpt-oss-120b")

    def test_cloudflare_at_prefix(self):
        p = _parts("/api/v1/gateway/cloudflare/test/"
                   "%40cf%2Fmeta%2Fllama-3.3-70b-instruct-fp8-fast")
        self.assertEqual(len(p), 6)
        self.assertEqual(p[-1], "@cf/meta/llama-3.3-70b-instruct-fp8-fast")

    def test_colon_suffix(self):
        self.assertEqual(
            _parts("/api/v1/providers/openrouter/test/minimax%2Fminimax-m3%3Afree")[-1],
            "minimax/minimax-m3:free")

    def test_plain_ids_unchanged(self):
        self.assertEqual(
            _parts("/api/v1/providers/gemini/test/gemini-3.5-flash")[-1],
            "gemini-3.5-flash")


if __name__ == "__main__":
    unittest.main()
