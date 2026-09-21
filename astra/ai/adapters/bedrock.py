"""Amazon Bedrock Converse adapter.

Two auth modes, tried in this order:

1. `BEDROCK_API_KEYS` — a Bedrock *API key* (bearer token, generated in the
   AWS Console under Bedrock > API keys). Sent as a plain
   `Authorization: Bearer <key>` header, same shape as every other
   OpenAI-compatible adapter in this codebase. No request signing needed.
2. `BEDROCK_CREDENTIALS` — classic `access_key:secret_key` IAM pairs
   (one per line / comma-separated), signed per-request with AWS
   Signature V4 (stdlib only — no botocore dependency).

Either is enough on its own; if both are set, the bearer-token API key
takes priority since it's the simpler, purpose-built auth path. `AWS_REGION`
/ `AWS_ACCESS_KEY_ID` fallbacks are still honoured for the SigV4 path.

Calls hit `bedrock-runtime.<region>.amazonaws.com/model/<model_id>/converse`.
Responses follow the Converse shape (`output.message.content[].text`).
Streaming uses Converse's `converseStream` with the `content_block_delta`
delta variant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from datetime import datetime, timezone

from astra.ai.credentials import CredentialPool
from astra.ai.provider import AIProvider
from astra.core.exceptions import ProviderError

SERVICE = "bedrock"


def _hmac(key, msg) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_v4(access_key: str, secret_key: str, region: str, service: str,
            method: str, url: str, headers: dict, payload: bytes) -> dict:
    """Return the request headers with the AWS Signature V4 Authorization."""
    from urllib.parse import urlsplit, quote
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    parts = urlsplit(url)
    host = parts.netloc
    canonical_uri = quote(parts.path, safe="/-_.~") or "/"
    canonical_querystring = parts.query or ""

    signed_headers = "content-type;host;x-amz-date"
    canonical_headers = (
        f"content-type:{headers['content-type']}\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n")
    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_request = "\n".join([
        method, canonical_uri, canonical_querystring, canonical_headers,
        signed_headers, payload_hash])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    return {"Content-Type": headers["content-type"], "Host": host,
            "X-Amz-Date": amz_date, "Authorization": auth}


class BedrockCredentialPool(CredentialPool):
    """Parses `access_key:secret_key` pairs into Credential-like entries."""

    @classmethod
    def from_env(cls, config, env_name: str, provider: str | None = None) -> "BedrockCredentialPool":
        pairs: list[str] = []
        text = getattr(config, "get", lambda _k, d="": d)(env_name, "") or ""
        for line in text.replace(",", "\n").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            pairs.append(line.strip())
        if not pairs:
            ak = getattr(config, "get", lambda _k, d="": d)("AWS_ACCESS_KEY_ID", "") or ""
            sk = getattr(config, "get", lambda _k, d="": d)("AWS_SECRET_ACCESS_KEY", "") or ""
            if ak and sk:
                pairs = [f"{ak}:{sk}"]
        pool = cls(provider or "bedrock", [])
        for entry in pairs:
            ak, _, sk = entry.partition(":")
            if ak and sk:
                pool.add(f"{ak}:{sk}")
        return pool

    def add(self, secret: str):
        ak, _, sk = secret.partition(":")
        return super().add(f"{ak}:{sk}")


class BedrockAdapter(AIProvider):
    name = "bedrock"
    capabilities = ["chat", "stream", "tools", "json", "vision"]

    models_env = "BEDROCK_MODELS"
    base_url_env = "BEDROCK_BASE_URL"
    api_keys_env = "BEDROCK_API_KEYS"
    credentials_env = "BEDROCK_CREDENTIALS"
    default_region = "us-east-1"

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config)
        self.events = events
        if pool is not None:
            self.pool = pool
            # caller-supplied pool: keep whichever auth mode its class implies
            self.auth_mode = "bearer" if not isinstance(pool, BedrockCredentialPool) else "sigv4"
        else:
            api_keys = config.getlist(self.api_keys_env, default=[]) if config else []
            if api_keys:
                # Bearer-token Bedrock API key — plain Authorization header,
                # no SigV4 signing, same as every other adapter here.
                self.auth_mode = "bearer"
                self.pool = CredentialPool.from_env(config, self.api_keys_env, "bedrock")
            else:
                # Classic access_key:secret_key IAM pair, SigV4-signed.
                self.auth_mode = "sigv4"
                self.pool = BedrockCredentialPool.from_env(config, self.credentials_env)
        self.region = (config.get("AWS_REGION") if config else None) or self.default_region
        raw = (config.get(self.base_url_env) if config else None) or "https://bedrock-runtime.us-east-1.amazonaws.com"
        self.base_url = raw.rstrip("/")
        self.models = self._configured_models()

    def _configured_models(self) -> list[str]:
        if self.config:
            return self.config.getlist(self.models_env, default=[])
        return []

    # -- signing --------------------------------------------------------------
    def _sign(self, cred, url: str, payload: bytes) -> dict:
        if self.auth_mode == "bearer":
            secret = self.pool.get_secret_for(cred)
            return {"Content-Type": "application/json",
                   "Authorization": f"Bearer {secret}"}
        ak, _, sk = self.pool.get_secret_for(cred).partition(":")
        return sign_v4(ak, sk, self.region, SERVICE, "POST", url,
                       {"content-type": "application/json"}, payload)

    def _post(self, url: str, body: dict, cred) -> dict:
        payload = json.dumps(body).encode("utf-8")
        headers = self._sign(cred, url, payload)
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self.pool.report_failure(cred, reason=f"bedrock http {e.code}",
                                     auth_failure=e.code in (401, 403))
            code = e.code
            if code in (401, 403):
                raise ProviderError("bedrock authentication/authorization failed")
            if code == 429:
                self.pool.report_failure(cred, reason="bedrock throttled", rate_limited=True, cooldown_s=45)
                raise ProviderError("bedrock rate limit reached")
            if code in (502, 503, 504):
                raise ProviderError("bedrock temporary error")
            raise ProviderError(f"bedrock http {code}")
        except urllib.error.URLError as e:
            raise ProviderError(f"bedrock network: {getattr(e, 'reason', e)}") from e
        self.pool.report_success(cred)
        return data

    # -- Converse shapes ------------------------------------------------------
    def _converse_body(self, messages, model, max_tokens) -> dict:
        system = "\n".join(
            m.get("content", "") for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str))
        convo = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            content = m.get("content", "")
            if isinstance(content, list):
                blocks = self._converse_content_blocks(content)
            else:
                blocks = [{"text": str(content)}]
            convo.append({"role": role, "content": blocks})
        body = {"modelId": model, "messages": convo,
                "inferenceConfig": {"maxTokens": max_tokens}}
        if system:
            body["system"] = [{"text": system}]
        return body

    @staticmethod
    def _converse_content_blocks(content_parts: list) -> list:
        blocks = []
        for part in content_parts:
            if not isinstance(part, dict):
                blocks.append({"text": str(part)})
                continue
            if part.get("type") == "text":
                blocks.append({"text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    import base64 as b64mod
                    header, _, b64data = url.partition(",")
                    mime = header.split(";")[0].replace("data:", "")
                    fmt = "png" if "png" in mime else "jpeg" if "jpeg" in mime else \
                          "gif" if "gif" in mime else "webp" if "webp" in mime else "png"
                    try:
                        raw = b64mod.b64decode(b64data)
                        blocks.append({"image": {"format": fmt,
                                                  "source": {"bytes": raw}}})
                    except Exception:
                        blocks.append({"text": "[image decode failed]"})
                else:
                    blocks.append({"text": f"[image: {url}]"})
            else:
                blocks.append({"text": part.get("text", str(part))})
        return blocks or [{"text": ""}]

    def chat(self, messages, model=None, max_tokens=500,
              response_format: str | None = None) -> str:
        # Bedrock's Converse API has no OpenAI-style response_format
        # knob — accepted-and-ignored so callers that ask every adapter
        # for JSON mode (astra.ai.router._attempt) don't crash here; JSON
        # compliance on Bedrock still comes from the prompt + Gateway's
        # own validate/correct loop, same as before this parameter existed.
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError("bedrock: no model configured")
        cred = self.pool.pick(model)
        if cred is None:
            raise ProviderError("bedrock: no healthy credential configured")
        body = self._converse_body(messages, model, max_tokens)
        data = self._post(f"{self.base_url}/model/{model}/converse", body, cred)
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip() or "(no reply)"

    def stream(self, messages, model=None, max_tokens=500):
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError("bedrock: no model configured")
        cred = self.pool.pick(model)
        if cred is None:
            raise ProviderError("bedrock: no healthy credential configured")
        if self.events:
            self.events.emit("ai.started", agent="provider", provider=self.name, model=model)
        body = self._converse_body(messages, model, max_tokens)
        payload = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}/model/{model}/converse-stream"
        req = urllib.request.Request(url, data=payload,
                                     headers=self._sign(cred, url, payload))
        import io
        full = ""
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:"):].strip())
                    except ValueError:
                        continue
                    if event.get("contentBlockDelta", {}).get("delta", {}).get("text"):
                        text = event["contentBlockDelta"]["delta"]["text"]
                        full += text
                        yield text
        except urllib.error.HTTPError as e:
            if self.events:
                self.events.emit("ai.failed", agent="provider", provider=self.name,
                                 model=model, error=f"http {e.code}")
            raise ProviderError(f"bedrock stream http {e.code}") from e
        self.pool.report_success(cred)
        if self.events:
            self.events.emit("ai.completed", agent="provider", provider=self.name,
                             model=model, length=len(full))

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image via Bedrock InvokeModel (Titan/Stability)."""
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError("bedrock: no model configured for image generation")
        cred = self.pool.pick()
        if cred is None:
            raise ProviderError("bedrock: no healthy credential configured")
        try:
            w, h = (int(x) for x in size.split("x"))
        except (ValueError, AttributeError):
            w, h = 1024, 1024
        low = model.lower()
        if "titan" in low:
            body = {
                "taskType": "TEXT_IMAGE",
                "textToImageParams": {"text": prompt},
                "imageGenerationConfig": {
                    "numberOfImages": min(n, 1),
                    "width": w, "height": h,
                },
            }
        else:
            body = {
                "text_prompts": [{"text": prompt}],
                "cfg_scale": 7, "steps": 30,
                "width": w, "height": h,
            }
        url = f"{self.base_url}/model/{model}/invoke"
        data = self._post(url, body, cred)
        b64 = ""
        if "images" in data and data["images"]:
            b64 = data["images"][0]
        elif "artifacts" in data and data["artifacts"]:
            b64 = data["artifacts"][0].get("base64", "")
        if not b64:
            raise ProviderError("bedrock: image generation returned no image data")
        return f"data:image/png;base64,{b64}"

    def health_check(self) -> bool:
        return self.pool.healthy_count > 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def estimate_cost(self, text: str) -> float:
        return self.count_tokens(text) * 0.25e-6 * 9.0

    def credential_summary(self) -> dict:
        return self.pool.summary()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities
