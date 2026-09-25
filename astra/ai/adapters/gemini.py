"""Gemini adapter.

Chat/stream/discovery use Gemini's OpenAI-compatible endpoint (the shared
compatible base handles them unchanged). Image generation does NOT: the
OpenAI compatibility layer does not produce images, so `generate_image()`
calls Gemini's native `:generateContent` with
`responseModalities: ["IMAGE"]` and extracts the returned inline image
part. Only the documented image models (see astra.ai.image_models) are
ever dispatched here.
"""
from __future__ import annotations

import time

from .base import CompatibleAdapter, image_result_to_data_uri
from astra.core.exceptions import ProviderError


class GeminiAdapter(CompatibleAdapter):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    models_env = "GEMINI_MODELS"
    base_url_env = "GEMINI_BASE_URL"
    api_keys_env = "GEMINI_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json", "vision"]

    image_models_env = "GEMINI_IMAGE_MODELS"

    #: Native (non-OpenAI) REST root used for image output.
    native_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _native_base(self) -> str:
        """Derive the native REST root from the configured (OpenAI-compat)
        base URL, honouring a GEMINI_BASE_URL override."""
        base = (self._api_base() or "").rstrip("/")
        if base.endswith("/openai"):
            return base[: -len("/openai")]
        return base or self.native_base_url

    @staticmethod
    def _inline_image(data) -> str:
        import base64 as _b64
        if not isinstance(data, dict):
            return ""
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline:
                continue
            payload = inline.get("data")
            if not payload:
                continue
            mime = (inline.get("mimeType") or inline.get("mime_type")
                    or "image/png")
            try:
                raw = _b64.b64decode(payload)
            except Exception:
                continue
            return "data:%s;base64,%s" % (mime, _b64.b64encode(raw).decode("ascii"))
        return ""

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image via the native Gemini image API.

        Gemini's OpenAI-compatibility endpoint does not return image output,
        so image models are called through the documented native
        `models/<id>:generateContent` route with an explicit
        `responseModalities` of IMAGE.
        """
        model = model or self._default_image_model()
        if not model:
            raise ProviderError("gemini: no image model configured")
        cred = self._pick(model)
        if cred is None:
            raise self._no_credential_error()
        secret = self.pool.get_secret_for(cred)
        # 2.5-series image models were documented with [TEXT, IMAGE]; the
        # 3.x image models emit the image directly.
        mods = ["TEXT", "IMAGE"] if "2.5" in model else ["IMAGE"]
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": mods}}
        url = "%s/models/%s:generateContent" % (self._native_base(), model)
        t0 = time.perf_counter()
        data = self._post_json(url, body, cred, headers={
            "content-type": "application/json",
            "x-goog-api-key": secret})
        self._done(cred)
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        uri = self._inline_image(data)
        if not uri:
            raise ProviderError(
                "gemini: image generation returned no image data")
        return uri
