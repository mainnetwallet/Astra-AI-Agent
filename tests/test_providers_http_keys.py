"""End-to-end over real HTTP: the AI Providers health panel's per-key flow —
list keys, test one model through one specific key, see the result saved."""
from __future__ import annotations

import json
import os
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from astra.ai.adapters.base import CompatibleAdapter
from astra.core.exceptions import ProviderError

from tests.helpers import make_stack

MODEL = "openai/gpt-oss-120b"     # has a "/" -> travels percent-encoded


class ProviderKeysHttpTests(unittest.TestCase):
    def setUp(self):
        from tests.helpers import LiveServer
        env = {"GROQ_API_KEYS": "good-key,bad-key", "GROQ_MODELS": MODEL}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.stack = make_stack()
        self.srv = LiveServer(stack=self.stack)
        self.port = self.srv.port

        def fake_post(adapter, url, body, cred):
            if adapter.pool.get_secret_for(cred) == "bad-key":
                adapter._done(cred, True, reason="http 403", auth_failure=True)
                raise ProviderError(f"{adapter.name} authorization denied")
            return {"choices": [{"message": {"content": "pong"}}]}
        self._post_patch = mock.patch.object(CompatibleAdapter, "_post", fake_post)
        self._post_patch.start()

    def tearDown(self):
        self._post_patch.stop()
        self._env.stop()
        self.srv.stop()

    def _req(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=b"{}" if method == "POST" else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_list_keys_test_each_key_and_see_saved_results(self):
        groq = self._req("/api/providers")["data"]["providers"]["groq"]
        keys = {k["label"]: k["key_id"] for k in groq["keys"]}
        self.assertEqual(sorted(keys), ["key 1", "key 2"])
        self.assertNotIn("good-key", json.dumps(groq))          # no secrets over the wire
        self.assertNotIn("bad-key", json.dumps(groq))

        enc = urllib.parse.quote(MODEL, safe="")
        res = {}
        for label, kid in keys.items():
            res[label] = self._req(
                f"/api/v1/providers/groq/test/{enc}?key={kid}", "POST")["data"]
        self.assertTrue(res["key 1"]["ok"])
        self.assertEqual(res["key 1"]["model"], MODEL)
        self.assertFalse(res["key 2"]["ok"])
        self.assertIn("authorization denied", res["key 2"]["error"])

        saved = self._req("/api/providers")["data"]["providers"]["groq"]["key_results"][MODEL]
        self.assertTrue(saved[keys["key 1"]]["ok"])
        self.assertFalse(saved[keys["key 2"]]["ok"])


if __name__ == "__main__":
    unittest.main()
