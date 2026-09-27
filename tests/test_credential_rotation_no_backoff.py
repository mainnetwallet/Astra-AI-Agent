"""Credential rotation within one Gateway target must not pay transient-retry
backoff: switching to a fresh, never-tried key is not the same thing as
waiting out a rate limit on the same key, and should incur no sleep. Backoff
still applies once every configured key has had a turn (a genuine retry) or
when there is no credential pool at all (unchanged single-key behavior).
"""
import unittest
from unittest import mock

from astra.ai.adapters.groq import GroqAdapter
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.config import Config
from astra.core.exceptions import ProviderError


def _adapter(keys):
    cfg = Config()
    cfg._runtime.update(GROQ_API_KEYS=keys, GROQ_MODELS="m1")
    return GroqAdapter(config=cfg)


class CredentialRotationBackoffTests(unittest.TestCase):
    def test_all_keys_tried_with_no_sleep_when_max_retries_zero(self):
        a = _adapter("k1,k2,k3")
        calls = []

        def fake_post(self, url, body, cred):
            secret = self.pool.get_secret_for(cred)
            calls.append(secret)
            self._done(cred, True, reason="http 401", auth_failure=True)
            raise ProviderError("groq: unauthorized")

        with mock.patch.object(type(a), "_post", fake_post):
            r = AstraRouter([a], max_retries=0, backoff_s=1.0)
            with mock.patch("astra.ai.router.time.sleep") as slept:
                rr = r.route_request(RoutingRequest(
                    messages=[{"role": "user", "content": "hi"}]))
        self.assertEqual(len(calls), 3)      # every configured key got a turn
        self.assertFalse(rr.ok)
        slept.assert_not_called()            # no wasted backoff between keys

    def test_working_key_found_without_any_sleep(self):
        a = _adapter("k1,k2,k3")
        calls = []

        def fake_post(self, url, body, cred):
            secret = self.pool.get_secret_for(cred)
            calls.append(secret)
            if secret == "k2":
                return {"choices": [{"message": {"content": "pong"}}]}
            self._done(cred, True, reason="http 401", auth_failure=True)
            raise ProviderError("groq: unauthorized")

        with mock.patch.object(type(a), "_post", fake_post):
            r = AstraRouter([a], max_retries=0, backoff_s=1.0)
            with mock.patch("astra.ai.router.time.sleep") as slept:
                rr = r.route_request(RoutingRequest(
                    messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        slept.assert_not_called()

    def test_single_key_transient_retry_still_backs_off(self):
        """Unchanged behavior: with only one credential, repeating it is a
        genuine transient retry and must still wait between attempts."""
        a = _adapter("only-key")
        calls = []

        def fake_post(self, url, body, cred):
            calls.append(1)
            if len(calls) >= 3:
                return {"choices": [{"message": {"content": "pong"}}]}
            raise ProviderError("groq: rate limited")

        with mock.patch.object(type(a), "_post", fake_post):
            r = AstraRouter([a], max_retries=2, backoff_s=1.0)
            with mock.patch("astra.ai.router.time.sleep") as slept:
                rr = r.route_request(RoutingRequest(
                    messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(slept.call_count, 2)


if __name__ == "__main__":
    unittest.main()
