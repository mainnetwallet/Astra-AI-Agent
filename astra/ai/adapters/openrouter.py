"""OpenRouter adapter."""
from __future__ import annotations

from .base import CompatibleAdapter


class OpenRouterAdapter(CompatibleAdapter):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    models_env = "OPENROUTER_MODELS"
    base_url_env = "OPENROUTER_BASE_URL"
    api_keys_env = "OPENROUTER_API_KEYS"
    image_models_env = "OPENROUTER_IMAGE_MODELS"
    capabilities = ["chat", "stream", "tools", "json", "vision"]
    extra_headers = {
        "HTTP-Referer": "https://github.com/mainnetwallet/Astra-AI-Agent",
        "X-Title": "Astra AI Agent",
    }

    #: OpenRouter's documented image-model discovery endpoint. Its returned
    #: output modalities are authoritative for the exact ids it lists.
    IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=image"

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        self._discovered_image_models = None
        self._discovered_at = 0.0

    def _discover_image_models(self, *, discover: bool = True) -> list:
        """LIVE image-model discovery (cached 10 min, failures included)."""
        import json
        import time
        import urllib.request
        now = time.time()
        fresh = (self._discovered_image_models is not None
                 and now - self._discovered_at < 600)
        if discover and not fresh:
            found = []
            try:
                req = urllib.request.Request(
                    self.IMAGE_MODELS_URL,
                    headers={"User-Agent": "astra-ai-agent"})
                # Discovery is best-effort: a short timeout keeps an
                # offline OpenRouter from stalling the request pipeline.
                with urllib.request.urlopen(req, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                for item in (payload.get("data") or []):
                    mid = (item or {}).get("id")
                    if not mid:
                        continue
                    arch = item.get("architecture") or {}
                    outs = arch.get("output_modalities") or []
                    if outs and "image" not in outs:
                        continue
                    found.append(mid)
            except Exception:
                found = []
            self._discovered_image_models = found
            self._discovered_at = now
        return list(self._discovered_image_models or [])

    def live_image_models(self, *, discover: bool = True) -> list:
        return self._discover_image_models(discover=discover)

    def list_image_models(self, *, discover: bool = True) -> list:
        ids = list(self.image_models)
        for mid in self._discover_image_models(discover=discover):
            if mid not in ids:
                ids.append(mid)
        return ids
