"""Cloudflare Workers AI adapter — multi-account deterministic selection.

CLOUDFLARE_ACCOUNT_IDS holds a comma-separated list of account ids; each
request deterministically round-robins across them (per Astra's routing
neutrality), and the API token travels as the Bearer header.
"""
from __future__ import annotations

import threading

from .base import CompatibleAdapter


class CloudflareAdapter(CompatibleAdapter):
    name = "cloudflare"
    base_url = "https://api.cloudflare.com/client/v4"
    models_env = "CLOUDFLARE_MODELS"
    api_keys_env = "CLOUDFLARE_API_KEYS"
    base_url_env = "CLOUDFLARE_BASE_URL"      # overrides the client/v4 root only;
                                               # the per-account /accounts/<id>/ai/v1
                                               # suffix below is always appended.
    capabilities = ["chat", "stream", "tools", "json"]
    account_ids_env = "CLOUDFLARE_ACCOUNT_IDS"

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        accounts = (config.getlist(self.account_ids_env) if config else []) or []
        self._accounts = accounts or []
        self._aidx = 0
        self._aidx_lock = threading.Lock()

    def _api_base(self) -> str:
        """Per-request account pick (deterministic round-robin).

        Must NOT mutate ``self.base_url``: the adapter is shared by concurrent
        request threads (the UI fires one test per model in parallel), and
        the old save/mutate/restore dance made them stack
        ``/accounts/<id>/ai/v1`` onto each other's URLs, so most calls hit a
        malformed path and came back "authentication failed"."""
        if not self._accounts:
            raise ValueError("cloudflare: no account ids configured (CLOUDFLARE_ACCOUNT_IDS)")
        with self._aidx_lock:
            acc = self._accounts[self._aidx % len(self._accounts)]
            self._aidx += 1
        return f"{self.base_url}/accounts/{acc}/ai/v1"
