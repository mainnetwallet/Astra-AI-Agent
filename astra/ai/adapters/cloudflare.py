"""Cloudflare Workers AI adapter — multi-account deterministic selection.

CLOUDFLARE_ACCOUNT_IDS holds a comma-separated list of account ids; each
request deterministically round-robins across them (per Astra's routing
neutrality), and the API token travels as the Bearer header.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from .base import (CompatibleAdapter, DEFAULT_TIMEOUT,
                   image_result_to_data_uri)
from astra.ai.provider import close_http_error
from astra.core.exceptions import ProviderError


class CloudflareAdapter(CompatibleAdapter):
    name = "cloudflare"
    base_url = "https://api.cloudflare.com/client/v4"
    models_env = "CLOUDFLARE_MODELS"
    api_keys_env = "CLOUDFLARE_API_KEYS"
    base_url_env = "CLOUDFLARE_BASE_URL"      # overrides the client/v4 root only;
                                               # the per-account /accounts/<id>/ai/v1
                                               # suffix below is always appended.
    image_models_env = "CLOUDFLARE_IMAGE_MODELS"
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

    # ── image generation (Workers AI /ai/run/<model>) ─────────────────────
    def _run_url(self, model: str, account: str) -> str:
        return f"{self.base_url}/accounts/{account}/ai/run/{model}"

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image via Cloudflare Workers AI (FLUX and friends).

        Workers AI image models are served by `/accounts/<id>/ai/run/<model>`,
        NOT by the OpenAI-compatible chat path, so this is a separate call.
        The response is normally JSON (`{"result": {"image": "<b64>"}}`) but
        some models stream raw image bytes; both are normalized to a data URI.
        """
        model = model or self._default_image_model()
        if not model:
            raise ProviderError("cloudflare: no image model configured")
        if not self._accounts:
            raise ProviderError(
                "cloudflare: no account ids configured (CLOUDFLARE_ACCOUNT_IDS)")
        cred = self._pick(model)
        if cred is None:
            raise self._no_credential_error()
        with self._aidx_lock:
            account = self._accounts[self._aidx % len(self._accounts)]
            self._aidx += 1
        body = {"prompt": prompt}
        low = model.lower()
        if "flux-1-schnell" in low or "flux-2" in low:
            try:
                w, h = (int(x) for x in str(size).lower().split("x"))
            except (ValueError, AttributeError):
                w, h = 1024, 1024
            body["width"] = max(256, min(w, 2048))
            body["height"] = max(256, min(h, 2048))
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._run_url(model, account), data=payload,
            headers=self._headers(cred))
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                raw = resp.read()
                ctype = (resp.headers.get("Content-Type") or "") if hasattr(resp, "headers") else ""
        except urllib.error.HTTPError as e:
            code = e.code
            close_http_error(e)
            self._classify_http(e, cred)
            raise ProviderError(f"cloudflare image http {code}")
        except urllib.error.URLError as e:
            self._done(cred, True, reason=f"network: {getattr(e, 'reason', e)}")
            raise ProviderError(
                f"cloudflare network error: {getattr(e, 'reason', e)}") from e
        self._done(cred)
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        uri = ""
        if raw[:1] == b"{":
            try:
                uri = image_result_to_data_uri(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                uri = ""
        if not uri and raw[:1] != b"{":
            # raw image bytes straight from the model
            import base64 as _b64
            from .base import _guess_image_mime
            mime = _guess_image_mime(raw, ctype)
            uri = f"data:{mime};base64," + _b64.b64encode(raw).decode("ascii")
        if not uri:
            raise ProviderError(
                "cloudflare: image generation returned no image data")
        return uri
