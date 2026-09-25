"""OpenRouter adapter.

OpenRouter's current, documented image generation API is the dedicated
**Images** API -- NOT the OpenAI-style ``/images/generations`` endpoint:

    * generate:  ``POST /api/v1/images``          (operationId: createImages)
    * discover:  ``GET  /api/v1/images/models``

The request body carries only documented Image API fields (``model`` and
``prompt`` required; ``n``, ``size`` optional) and never an OpenAI
``response_format`` knob. The response is
``{"data": [{"b64_json": ..., "media_type": ...}]}``.
"""
from __future__ import annotations

import time

from astra.ai.image_payload import image_result_to_data_uri
from astra.core.exceptions import ProviderError

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
    IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/images/models"

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        self._discovered_image_models = None
        self._discovered_at = 0.0

    def _discover_image_models(self, *, discover: bool = True) -> list:
        """LIVE image-model discovery (cached 10 min, failures included)."""
        import json
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
                    if not outs or "image" not in outs:
                        continue
                    # ":free" is OpenRouter's marker for the free variant;
                    # a paid image model must never enter the free pool.
                    if not mid.endswith(":free"):
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

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image via OpenRouter's dedicated Images API.

        ``POST {base}/images`` -- the current documented endpoint. Only
        documented image parameters are sent (``model``, ``prompt``, ``n``,
        ``size``); the OpenAI ``response_format`` field is deliberately NOT
        sent. The response's ``data[].b64_json`` is normalized into the
        ``data:<mime>;base64,<...>`` shape the artifact pipeline understands.
        A 429/5xx/network failure propagates as ProviderError so the Gateway's
        global serial fallback advances to the next eligible image model; it
        never silently degrades to chat().
        """
        model = model or self._default_image_model()
        # Credential check FIRST, so "no usable key" stays a non-retryable
        # fail-fast for every entry point (chat/stream/image/tts) -- see
        # tests/test_no_credential_no_retry.py.
        cred = self._pick(model)
        if cred is None:
            raise self._no_credential_error()
        if not model:
            # Nothing to route to and retrying cannot change that: a missing
            # image model is a configuration error, not a transient one.
            err = ProviderError(f"{self.name}: no image model configured")
            err.retryable = False
            raise err
        body = {"model": model, "prompt": prompt, "n": max(1, int(n or 1))}
        if size:
            body["size"] = size
        t0 = time.perf_counter()
        data = self._post(f"{self._api_base()}/images", body, cred)
        self._done(cred)
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        uri = image_result_to_data_uri(data)
        if not uri:
            raise ProviderError(
                f"{self.name}: image generation returned no image data")
        return uri
