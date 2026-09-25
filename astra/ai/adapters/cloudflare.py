"""Cloudflare Workers AI adapter — multi-account deterministic selection.

CLOUDFLARE_ACCOUNT_IDS holds a comma-separated list of account ids; each
request deterministically round-robins across them (per Astra's routing
neutrality), and the API token travels as the Bearer header.

Two APIs live on the same account/token:

* chat/completions (and streaming) under ``/accounts/<id>/ai/v1``, and
* real image GENERATION under ``/accounts/<id>/ai/run/<model>`` — the Workers
  AI image models (Stable Diffusion, FLUX, Leonardo) are invoked there and
  answer with raw image bytes or a small base64 envelope. Cloudflare does not
  expose the OpenAI-compatible ``/images/generations`` endpoint, so
  ``generate_image`` is overridden here instead of inherited.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request

from astra.ai.models import PROVIDER_IMAGE_MODELS
from astra.ai.provider import close_http_error
from astra.core.exceptions import ProviderError

from .base import CompatibleAdapter

# Image generation is a single long call; give it a wider window than chat.
IMAGE_TIMEOUT = 120


def _image_payload(raw: bytes, ctype: str) -> tuple[str, str]:
    """Normalise a Workers AI image response to ``(base64, mime)``.

    Workers AI answers with either the raw image bytes (content-type
    image/png|jpeg) or a JSON envelope carrying base64 (``result`` /
    ``result.image`` / ``images[0]``). Returns ``("", "")`` when the response
    holds no usable image, so the caller fails honestly.
    """
    mime = "image/jpeg" if ("jpeg" in ctype or "jpg" in ctype) else "image/png"
    looks_json = "json" in ctype or raw[:1] == b"{"
    if looks_json:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return "", ""
        if not isinstance(data, dict) or data.get("success") is False:
            return "", ""
        result = data.get("result", data)
        if isinstance(result, str):
            value = result
        elif isinstance(result, dict):
            value = (result.get("image") or result.get("b64_json")
                     or result.get("data") or "")
        elif isinstance(result, list) and result:
            first = result[0]
            value = first if isinstance(first, str) else (
                first.get("image", "") if isinstance(first, dict) else "")
        else:
            value = ""
        value = str(value or "")
        if not value:
            return "", ""
        if value.startswith("data:"):        # already a data URI
            head, _, payload = value.partition(",")
            if "image/jpeg" in head:
                mime = "image/jpeg"
            return payload, mime
        return value, mime
    if not raw:
        return "", ""
    return base64.b64encode(raw).decode("ascii"), mime


class CloudflareAdapter(CompatibleAdapter):
    name = "cloudflare"
    base_url = "https://api.cloudflare.com/client/v4"
    models_env = "CLOUDFLARE_MODELS"
    api_keys_env = "CLOUDFLARE_API_KEYS"
    base_url_env = "CLOUDFLARE_BASE_URL"      # overrides the client/v4 root only;
                                               # the per-account /accounts/<id>/ai/v1
                                               # suffix below is always appended.
    capabilities = ["chat", "stream", "tools", "json", "image"]
    account_ids_env = "CLOUDFLARE_ACCOUNT_IDS"

    # Real Workers AI image-generation models, served through the provider's
    # own `/ai/run/<model>` API (see `generate_image`). Canonical list lives
    # in astra.ai.models so the registry and the adapter never drift apart.
    image_models = PROVIDER_IMAGE_MODELS["cloudflare"]

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        accounts = (config.getlist(self.account_ids_env) if config else []) or []
        self._accounts = accounts or []
        self._aidx = 0
        self._aidx_lock = threading.Lock()

    def _next_account(self) -> str:
        """One deterministic round-robin account pick for this request."""
        if not self._accounts:
            raise ValueError("cloudflare: no account ids configured (CLOUDFLARE_ACCOUNT_IDS)")
        with self._aidx_lock:
            acc = self._accounts[self._aidx % len(self._accounts)]
            self._aidx += 1
        return acc

    def _api_base(self) -> str:
        """Per-request account pick (deterministic round-robin).

        Must NOT mutate ``self.base_url``: the adapter is shared by concurrent
        request threads (the UI fires one test per model in parallel), and
        the old save/mutate/restore dance made them stack
        ``/accounts/<id>/ai/v1`` onto each other's URLs, so most calls hit a
        malformed path and came back "authentication failed"."""
        return f"{self.base_url}/accounts/{self._next_account()}/ai/v1"

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate a real image through Cloudflare Workers AI.

        Invoked at ``/accounts/<id>/ai/run/<model>`` — NOT the
        OpenAI-compatible ``/images/generations`` the base class targets,
        which Workers AI does not expose. The response is normalised to a
        ``data:`` URI so it flows through the existing artifact pipeline.
        """
        model = model or (self.image_models[0] if self.image_models else "")
        if not model:
            raise ProviderError("cloudflare: no image model configured")
        cred = self._pick(model)
        if cred is None:
            raise self._no_credential_error()
        url = f"{self.base_url}/accounts/{self._next_account()}/ai/run/{model}"
        body = json.dumps({"prompt": prompt}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers(cred))
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
                raw = resp.read()
                ctype = (resp.headers.get("content-type") or "").lower()
        except urllib.error.HTTPError as e:
            close_http_error(e)
            self._classify_http(e, cred)
            raise
        except urllib.error.URLError as e:
            self._done(cred, True, reason=f"network: {getattr(e, 'reason', e)}")
            raise ProviderError(
                f"cloudflare network error: {getattr(e, 'reason', e)}") from e
        self._done(cred)
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        b64, mime = _image_payload(raw, ctype)
        if not b64:
            raise ProviderError(
                "cloudflare: image generation returned no image data")
        return f"data:{mime};base64,{b64}"
