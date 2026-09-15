"""Cloudflare Workers AI adapter — multi-account deterministic selection.

CLOUDFLARE_ACCOUNT_IDS holds a comma-separated list of account ids; each
request deterministically round-robins across them (per Astra's routing
neutrality), and the API token travels as the Bearer header.
"""
from __future__ import annotations

from .base import CompatibleAdapter


class CloudflareAdapter(CompatibleAdapter):
    name = "cloudflare"
    base_url = "https://api.cloudflare.com/client/v4"
    models_env = "CLOUDFLARE_MODELS"
    api_keys_env = "CLOUDFLARE_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json"]
    account_ids_env = "CLOUDFLARE_ACCOUNT_IDS"

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        accounts = (config.getlist(self.account_ids_env) if config else []) or []
        self._accounts = accounts or []
        self._aidx = 0

    def _base(self) -> str:
        if not self._accounts:
            raise ValueError("cloudflare: no account ids configured (CLOUDFLARE_ACCOUNT_IDS)")
        acc = self._accounts[self._aidx % len(self._accounts)]
        self._aidx += 1
        return f"{self.base_url}/accounts/{acc}/ai/v1"

    def chat(self, messages, model=None, max_tokens=500) -> str:
        base = self.base_url
        self.base_url = self._base()
        try:
            return super().chat(messages, model, max_tokens)
        finally:
            self.base_url = base

    def stream(self, messages, model=None, max_tokens=500):
        base = self.base_url
        self.base_url = self._base()
        try:
            yield from super().stream(messages, model, max_tokens)
        finally:
            self.base_url = base

    def list_models(self) -> list[str]:
        base = self.base_url
        self.base_url = self._base()
        try:
            return super().list_models()
        finally:
            self.base_url = base