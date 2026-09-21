"""Regressions from the "AI Providers health" panel:

1. Cloudflare adapters mutated shared `base_url` per call, so concurrent
   per-model tests stacked `/accounts/<id>/ai/v1` onto each other's URLs and
   most calls came back "authentication failed".
2. A 404 "model not found" / 400 on ONE model put the provider's only API key
   in cooldown, so every other model on that provider failed with
   "no healthy credential configured".
"""
from __future__ import annotations

import io
import threading
import time
import unittest
import urllib.error
from unittest import mock

from astra.core.config import Config
from astra.core.exceptions import ProviderError


def _cfg(**env):
    cfg = Config()
    cfg._runtime.update(env)
    return cfg


class CloudflareConcurrencyTests(unittest.TestCase):
    URL = "https://api.cloudflare.com/client/v4/accounts/acctA/ai/v1/chat/completions"

    def _hammer(self, adapter, patch_target):
        seen = []

        def fake_post(self_, url, body, cred):
            seen.append(url)
            time.sleep(0.01)
            return {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch(patch_target, fake_post):
            ts = [threading.Thread(
                target=lambda: adapter.chat([{"role": "user", "content": "x"}], model="m"))
                for _ in range(30)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        return seen

    def test_provider_adapter_urls_stay_well_formed(self):
        from astra.ai.adapters.cloudflare import CloudflareAdapter
        ad = CloudflareAdapter(config=_cfg(CLOUDFLARE_API_KEYS="k",
                                           CLOUDFLARE_ACCOUNT_IDS="acctA"))
        seen = self._hammer(ad, "astra.ai.adapters.base.CompatibleAdapter._post")
        self.assertEqual(set(seen), {self.URL})
        self.assertEqual(ad.base_url, "https://api.cloudflare.com/client/v4")

    def test_gateway_connection_urls_stay_well_formed(self):
        from astra.ai.gateway import AstraGatewayCloudflare
        ad = AstraGatewayCloudflare(config=_cfg(GW_CLOUDFLARE_API_KEYS="k",
                                                GW_CLOUDFLARE_ACCOUNT_IDS="acctA"))
        seen = self._hammer(ad, "astra.ai.gateway._GatewayCompatibleConnection._post")
        self.assertEqual(set(seen), {self.URL})
        self.assertEqual(ad.base_url, "https://api.cloudflare.com/client/v4")

    def test_round_robin_across_accounts(self):
        from astra.ai.adapters.cloudflare import CloudflareAdapter
        ad = CloudflareAdapter(config=_cfg(CLOUDFLARE_API_KEYS="k",
                                           CLOUDFLARE_ACCOUNT_IDS="a1,a2"))
        got = [ad._api_base() for _ in range(4)]
        self.assertEqual([g.split("/accounts/")[1].split("/")[0] for g in got],
                         ["a1", "a2", "a1", "a2"])


class RequestLevelErrorsDoNotPoisonKeyTests(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"{}"))

    def _check(self, adapter, patch_target, code, expect_key_usable=True):
        with mock.patch(patch_target, side_effect=self._http_error(code)):
            with self.assertRaises(ProviderError):
                adapter.chat([{"role": "user", "content": "x"}], model="gone")
        self.assertEqual(adapter.pool.pick() is not None, expect_key_usable)

    def test_404_and_400_keep_key_usable(self):
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        for code in (400, 404):
            ad = OpenRouterAdapter(config=_cfg(OPENROUTER_API_KEYS="k"))
            self._check(ad, "urllib.request.urlopen", code)

    def test_gateway_connection_404_keeps_key_usable(self):
        from astra.ai.gateway import AstraGatewayGroq
        ad = AstraGatewayGroq(config=_cfg(GW_GROQ_API_KEYS="k"))
        self._check(ad, "urllib.request.urlopen", 404)

    def test_rate_limit_and_auth_still_block_the_key(self):
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        for code in (429, 401):
            ad = OpenRouterAdapter(config=_cfg(OPENROUTER_API_KEYS="k"))
            self._check(ad, "urllib.request.urlopen", code, expect_key_usable=False)


if __name__ == "__main__":
    unittest.main()
