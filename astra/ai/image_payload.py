"""Provider-agnostic normalization of image-generation API responses.

Every image API Astra speaks returns image data in one of a few shapes:

    * OpenAI-compatible Images API -> {"data": [{"b64_json"|"url": ...}]}
    * Cloudflare Workers AI        -> {"result": {"image"|"images": ...}}
    * Bedrock (Stability)          -> {"artifacts": [{"base64": ...}]}
    * Bedrock (Nova Canvas/Titan)  -> {"images": ["<base64>", ...]}

This module turns all of them into the single `data:<mime>;base64,<...>`
shape the artifact pipeline (`astra.ai.artifact_extraction`) understands.

It is deliberately dependency-free (stdlib only, no astra imports) so it can
be used by both the Provider adapters and the isolated Astra AI Gateway.
"""
from __future__ import annotations

import base64
import urllib.request

URL_FETCH_TIMEOUT_S = 60


def _extract_image_payload(data) -> tuple | None:
    """Find image output in any provider's image response.

    Returns ("b64", <base64 str>) or ("url", <url>) or None. Handles:
      * OpenAI-compatible Images API  -> {"data": [{"b64_json"|"url": ...}]}
      * Cloudflare Workers AI         -> {"result": {"image": <b64>}}
                                         {"result": {"images": [{"image": ..}]}}
      * Bedrock Stability             -> {"artifacts": [{"base64": <b64>}]}
    """
    if not isinstance(data, dict):
        return None
    for item in (data.get("data") or []):
        if isinstance(item, dict):
            if isinstance(item.get("b64_json"), str) and item["b64_json"]:
                return ("b64", item["b64_json"])
            if isinstance(item.get("url"), str) and item["url"]:
                return ("url", item["url"])
    res = data.get("result")
    if isinstance(res, dict):
        if isinstance(res.get("image"), str) and res["image"]:
            return ("b64", res["image"])
        for it in (res.get("images") or []):
            if isinstance(it, dict) and isinstance(it.get("image"), str):
                return ("b64", it["image"])
            if isinstance(it, str) and it:
                return ("b64", it)
    # Bedrock Nova Canvas / Titan Image return a top-level `images` array of
    # base64 strings -> {"images": ["<b64>", ...], "error": null}
    for it in (data.get("images") or []):
        if isinstance(it, str) and it:
            return ("b64", it)
        if isinstance(it, dict):
            if isinstance(it.get("base64"), str) and it["base64"]:
                return ("b64", it["base64"])
            if isinstance(it.get("image"), str) and it["image"]:
                return ("b64", it["image"])
    for it in (data.get("artifacts") or []):
        if isinstance(it, dict) and isinstance(it.get("base64"), str) and it["base64"]:
            return ("b64", it["base64"])
    if isinstance(data.get("image"), str) and data["image"]:
        return ("b64", data["image"])
    return None


def _guess_image_mime(raw: bytes, hint: str = "") -> str:
    if hint:
        hint = hint.lower().strip()
        if hint.startswith("image/"):
            return hint
        if hint in ("png", "jpeg", "jpg", "webp", "gif"):
            return "image/jpeg" if hint in ("jpeg", "jpg") else "image/" + hint
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def image_result_to_data_uri(data, *, fallback_mime: str = "image/png") -> str:
    """Normalize any provider image response into a `data:` URI string.

    Everything downstream (astra.ai.artifact_extraction) already understands
    exactly this shape, so every provider-specific image API funnels through
    here rather than duplicating base64 handling.
    """
    import base64 as _b64
    found = _extract_image_payload(data)
    if not found:
        return ""
    kind, payload = found
    if kind == "b64":
        if payload.startswith("data:"):
            return payload
        try:
            raw = _b64.b64decode(payload)
        except Exception:
            return ""
        mime = _guess_image_mime(raw, fallback_mime)
        return f"data:{mime};base64," + _b64.b64encode(raw).decode("ascii")
    # url -> download the bytes once, so the artifact pipeline stays offline
    # and consistent across providers.
    try:
        req = urllib.request.Request(payload, headers={"User-Agent": "astra-ai-agent"})
        with urllib.request.urlopen(req, timeout=URL_FETCH_TIMEOUT_S) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "") if hasattr(resp, "headers") else ""
    except Exception:
        return ""
    if len(raw) < 100:
        return ""
    mime = _guess_image_mime(raw, ctype or fallback_mime)
    return f"data:{mime};base64," + _b64.b64encode(raw).decode("ascii")