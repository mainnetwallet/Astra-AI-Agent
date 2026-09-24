"""A provider with no usable credential must fail fast.

Regression: "no healthy credential configured" was a plain retryable
ProviderError, so the router retried it 3x with 1s + 2s backoff (~4s lost per
request) before falling over to a provider that actually had a key.
"""
import unittest
from unittest import mock

from astra.ai.adapters.base import CompatibleAdapter
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.exceptions import ProviderError
from tests.test_ai import FakeAIProvider


class _NoKeyPool:
    def pick(self, model=None):
        return None

    def __bool__(self):
        return True


class _NoKeyAdapter(CompatibleAdapter):
    name = "nokey"
    models = ["m0"]
    base_url = "https://example.invalid/v1"


class TestNoCredentialFailsFast(unittest.TestCase):
    def test_adapter_error_is_not_retryable_for_every_entry_point(self):
        a = _NoKeyAdapter(pool=_NoKeyPool())
        calls = [lambda: a.chat([{"role": "user", "content": "hi"}]),
                 lambda: list(a.stream([{"role": "user", "content": "hi"}])),
                 lambda: a.generate_image("cat"),
                 lambda: a.text_to_speech("hi")]
        for call in calls:
            with self.assertRaises(ProviderError) as cm:
                call()
            self.assertFalse(cm.exception.retryable)
            self.assertIn("no healthy credential configured", str(cm.exception))

    def test_ordinary_provider_errors_are_still_retryable(self):
        self.assertTrue(ProviderError("boom").retryable)

    def test_router_tries_it_once_without_sleeping_then_falls_over(self):
        class NoKey(FakeAIProvider):
            def chat(self, messages, model=None, max_tokens=500, response_format=None):
                self._calls += 1
                err = ProviderError("nokey: no healthy credential configured")
                err.retryable = False
                raise err

        bad = NoKey(name="nokey", models=["a"])
        good = FakeAIProvider(name="good", models=["b"])
        r = AstraRouter([bad, good])           # default max_retries=2
        with mock.patch("astra.ai.router.time.sleep") as slept:
            rr = r.route_request(RoutingRequest(
                messages=[{"role": "user", "content": "hi"}],
                preferred_provider="nokey", preferred_model="a"))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "good")
        self.assertEqual(bad._calls, 1)          # not 3
        slept.assert_not_called()                # no 1s + 2s backoff

    def test_transient_errors_are_still_retried_with_backoff(self):
        flaky = FakeAIProvider(name="flaky", models=["a"], fail_first=2)
        r = AstraRouter([flaky])
        with mock.patch("astra.ai.router.time.sleep") as slept:
            rr = r.route_request(RoutingRequest(
                messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(flaky._calls, 3)
        self.assertEqual(slept.call_count, 2)


if __name__ == "__main__":
    unittest.main()
