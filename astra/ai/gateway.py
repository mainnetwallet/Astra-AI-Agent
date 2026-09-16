"""Astra AI Gateway — a separate multi-service AI gateway with automatic fallback.

Replaces the old third-party AI gateway service with a set of four
independent AI connections:

    Astra AI Gateway
    ├── Gemini      ← GW_GEMINI_* config
    ├── Groq        ← GW_GROQ_* config
    ├── Cloudflare  ← GW_CLOUDFLARE_* config
    └── Bedrock     ← GW_BEDROCK_* config

Each connection has completely independent credentials, models and
endpoints/base URLs — separate from the existing Provider system:
GW_GEMINI_API_KEYS ≠ GEMINI_API_KEYS, etc. The Gateway is NOT a provider:
it is never added to ProviderRegistry and is reported separately in
health/dashboard output, never inside the provider table.

Architecture note: this module is fully self-contained. It does NOT import,
subclass, or instantiate the existing Provider adapter classes
(GeminiAdapter, GroqAdapter, CloudflareAdapter, BedrockAdapter in
astra/ai/adapters/*) — those remain exclusively the Provider system's. Each
Gateway connection below (AstraGatewayGemini, AstraGatewayGroq,
AstraGatewayCloudflare, AstraGatewayBedrock) implements its own request/
response/auth plumbing against its own GW_-prefixed configuration. Nothing
here reads GEMINI_*/GROQ_*/CLOUDFLARE_*/BEDROCK_* or touches a Provider
adapter instance, client, or config object.

Fallback: if Gemini fails → Groq → Cloudflare → Bedrock. The same task,
messages, context and system instructions carry over unchanged between
services (the full request is re-sent with the next connection until one
succeeds) — no restart, no task duplication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from astra.ai.credentials import CredentialPool
from astra.core.exceptions import ProviderError, TimeoutError

GW_DEFAULT_TIMEOUT = 60
GW_STREAM_TIMEOUT = 120


# ── shared OpenAI-compatible plumbing (Gateway-only; not the Provider base) ──
# Gemini, Groq and Cloudflare all speak the same `{model, messages, max_tokens}`
# → `choices[].message.content` dialect. This base is private to the Gateway
# module — it does not derive from astra.ai.adapters.base.CompatibleAdapter or
# astra.ai.provider.AIProvider, and every connection built on it reads only
# its own GW_* env vars via its own CredentialPool instance.
class _GatewayCompatibleConnection:
    name: str = "astra-gw-compatible"
    models_env: str = ""
    api_keys_env: str = ""
    base_url_env: str = ""
    base_url: str = ""
    capabilities: list[str] = ["chat", "stream"]

    def __init__(self, config=None, events=None, pool: CredentialPool | None = None):
        self.config = config
        self.events = events
        self.pool = pool or CredentialPool.from_env(config, self.api_keys_env, self.name)
        self.models = (config.getlist(self.models_env, default=[])
                       if config and self.models_env else list(self.models
                       if hasattr(self, "models") else []))
        raw = (config.get(self.base_url_env, None) if config and self.base_url_env else None)
        self.base_url = (raw or self.base_url or "").rstrip("/")
        self._last_usage: dict = {}

    # -- credentials ------------------------------------------------------
    def _pick(self):
        return self.pool.pick()

    def _done(self, cred=None, errored=False, reason="", *, rate_limited=False,
              auth_failure=False, cooldown_s: float = 30.0) -> None:
        if cred is None:
            return
        if errored:
            self.pool.report_failure(cred, reason=reason, rate_limited=rate_limited,
                                     auth_failure=auth_failure, cooldown_s=cooldown_s)
        else:
            self.pool.report_success(cred)

    def _headers(self, cred) -> dict:
        h = {"content-type": "application/json"}
        secret = self.pool.get_secret_for(cred) if cred else ""
        if secret:
            h["Authorization"] = f"Bearer {secret}"
        return h

    # -- HTTP ---------------------------------------------------------------
    def _post(self, url: str, body: dict, cred) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(cred))
        try:
            with urllib.request.urlopen(req, timeout=GW_DEFAULT_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            self._classify_http(e, cred)
            raise
        except urllib.error.URLError as e:
            self._done(cred, True, reason=f"network: {getattr(e, 'reason', e)}")
            raise ProviderError(f"{self.name} network error: {getattr(e, 'reason', e)}") from e
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            raise ProviderError(f"{self.name} bad json response") from e

    def _classify_http(self, e: urllib.error.HTTPError, cred) -> None:
        code = getattr(e, "code", 0)
        rate_limited = code in (408, 429)
        auth = code in (401, 403)
        self._done(cred, True, reason=f"http {code}", rate_limited=rate_limited,
                   auth_failure=auth, cooldown_s=(45 if rate_limited else 30))
        if code == 408:
            raise TimeoutError(f"{self.name} timed out")
        if code == 429:
            raise ProviderError(f"{self.name} rate limit reached")
        if code in (401, 403):
            raise ProviderError(f"{self.name} authentication failed")
        raise ProviderError(f"{self.name} http {code}")

    def _read_sse(self, resp) -> list[dict]:
        results = []
        raw = resp.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for line in raw.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
        return results

    # -- interface ------------------------------------------------------------
    def chat(self, messages, model=None, max_tokens=500) -> str:
        cred = self._pick()
        if cred is None:
            raise ProviderError(f"{self.name}: no healthy credential configured")
        body = {"model": model or (self.models[0] if self.models else ""),
                "max_tokens": max_tokens, "messages": messages}
        data = self._post(f"{self.base_url}/chat/completions", body, cred)
        self._done(cred)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        self._last_usage = data.get("usage", {})
        return text.strip() or "(no reply)"

    def stream(self, messages, model=None, max_tokens=500):
        cred = self._pick()
        if cred is None:
            raise ProviderError(f"{self.name}: no healthy credential configured")
        if self.events:
            self.events.emit("ai.started", agent="gateway", provider=self.name,
                             model=model or (self.models[0] if self.models else ""))
        body = {"model": model or (self.models[0] if self.models else ""),
                "max_tokens": max_tokens, "messages": messages, "stream": True}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}/chat/completions",
                                     data=data, headers=self._headers(cred))
        full = ""
        try:
            with urllib.request.urlopen(req, timeout=GW_STREAM_TIMEOUT) as resp:
                for chunk in self._read_sse(resp):
                    choices = chunk.get("choices", [])
                    delta = choices[0].get("delta", {}) if choices else {}
                    text = delta.get("content", "")
                    if text:
                        full += text
                        yield text
        except urllib.error.HTTPError as e:
            self._classify_http(e, cred)
            raise
        except urllib.error.URLError as e:
            self._done(cred, True, reason="stream network error")
            raise ProviderError(f"{self.name} stream network error") from e
        self._done(cred)
        if self.events:
            self.events.emit("ai.completed", agent="gateway", provider=self.name,
                             length=len(full))

    def health_check(self) -> bool:
        return bool(self.pool)

    def credential_summary(self) -> dict:
        return self.pool.summary()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# ── Gateway connections ─────────────────────────────────────────────────────
# Each service has its own credentials / model list / endpoint, read only
# from its own GW_-prefixed env vars — completely independent of the
# existing Provider system's adapters, config and clients.

class AstraGatewayGemini(_GatewayCompatibleConnection):
    """Astra AI Gateway / Gemini connection (independent of GeminiAdapter)."""
    name = "astra-gw-gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    models_env = "GW_GEMINI_MODELS"
    api_keys_env = "GW_GEMINI_API_KEYS"
    base_url_env = "GW_GEMINI_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json", "vision"]


class AstraGatewayGroq(_GatewayCompatibleConnection):
    """Astra AI Gateway / Groq connection (independent of GroqAdapter)."""
    name = "astra-gw-groq"
    base_url = "https://api.groq.com/openai/v1"
    models_env = "GW_GROQ_MODELS"
    api_keys_env = "GW_GROQ_API_KEYS"
    base_url_env = "GW_GROQ_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json"]


class AstraGatewayCloudflare(_GatewayCompatibleConnection):
    """Astra AI Gateway / Cloudflare connection (independent of
    CloudflareAdapter). Implements its own per-account round-robin over the
    independent GW_CLOUDFLARE_ACCOUNT_IDS list.
    """
    name = "astra-gw-cloudflare"
    base_url = "https://api.cloudflare.com/client/v4"
    models_env = "GW_CLOUDFLARE_MODELS"
    api_keys_env = "GW_CLOUDFLARE_API_KEYS"
    base_url_env = "GW_CLOUDFLARE_BASE_URL"
    account_ids_env = "GW_CLOUDFLARE_ACCOUNT_IDS"
    capabilities = ["chat", "stream", "tools", "json"]

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        accounts = (config.getlist(self.account_ids_env) if config else []) or []
        self._accounts = accounts or []
        self._aidx = 0

    def _account_base(self) -> str:
        if not self._accounts:
            raise ProviderError(
                f"{self.name}: no account ids configured ({self.account_ids_env})")
        acc = self._accounts[self._aidx % len(self._accounts)]
        self._aidx += 1
        return f"{self.base_url}/accounts/{acc}/ai/v1"

    def chat(self, messages, model=None, max_tokens=500) -> str:
        base = self.base_url
        self.base_url = self._account_base()
        try:
            return super().chat(messages, model, max_tokens)
        finally:
            self.base_url = base

    def stream(self, messages, model=None, max_tokens=500):
        base = self.base_url
        self.base_url = self._account_base()
        try:
            yield from super().stream(messages, model, max_tokens)
        finally:
            self.base_url = base


# ── Gateway Bedrock connection (independent of BedrockAdapter) ──────────────
def _gw_hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _gw_sign_v4(access_key: str, secret_key: str, region: str, service: str,
                method: str, url: str, content_type: str, payload: bytes) -> dict:
    """Gateway-local AWS SigV4 signer — duplicated here (not imported from
    astra.ai.adapters.bedrock) so the Gateway has zero dependency on the
    Provider Bedrock adapter module."""
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
        f"content-type:{content_type}\n"
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
    k_date = _gw_hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _gw_hmac(k_date, region)
    k_service = _gw_hmac(k_region, service)
    k_signing = _gw_hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    return {"Content-Type": content_type, "Host": host,
            "X-Amz-Date": amz_date, "Authorization": auth}


class _GatewayBedrockCredentialPool(CredentialPool):
    """Parses `access_key:secret_key` pairs from GW_BEDROCK_CREDENTIALS.
    A Gateway-local pool class — not astra.ai.adapters.bedrock.BedrockCredentialPool."""

    @classmethod
    def from_env(cls, config, env_name: str, provider: str | None = None):
        pairs: list[str] = []
        text = getattr(config, "get", lambda _k, d="": d)(env_name, "") or ""
        for line in text.replace(",", "\n").splitlines():
            line = line.strip()
            if line and ":" in line:
                pairs.append(line)
        pool = cls(provider or "astra-gw-bedrock", [])
        for entry in pairs:
            ak, _, sk = entry.partition(":")
            if ak and sk:
                pool.add(entry)
        return pool

    def add(self, secret: str):
        ak, _, sk = secret.partition(":")
        return super().add(f"{ak}:{sk}")


class AstraGatewayBedrock:
    """Astra AI Gateway / Bedrock connection (independent of BedrockAdapter).

    Own two auth modes — GW_BEDROCK_API_KEYS (bearer) or GW_BEDROCK_CREDENTIALS
    (SigV4 access_key:secret_key pairs) — own region (GW_BEDROCK_REGION, no
    fallback to the shared AWS_REGION/AWS_ACCESS_KEY_ID env vars the Provider
    Bedrock adapter reads) and own model list/base URL.
    """
    name = "astra-gw-bedrock"
    capabilities = ["chat", "stream", "tools", "json", "vision"]

    models_env = "GW_BEDROCK_MODELS"
    base_url_env = "GW_BEDROCK_BASE_URL"
    api_keys_env = "GW_BEDROCK_API_KEYS"
    credentials_env = "GW_BEDROCK_CREDENTIALS"
    region_env = "GW_BEDROCK_REGION"
    default_region = "us-east-1"

    def __init__(self, config=None, events=None, pool=None):
        self.config = config
        self.events = events
        if pool is not None:
            self.pool = pool
            self.auth_mode = "sigv4" if isinstance(pool, _GatewayBedrockCredentialPool) else "bearer"
        else:
            api_keys = config.getlist(self.api_keys_env, default=[]) if config else []
            if api_keys:
                self.auth_mode = "bearer"
                self.pool = CredentialPool.from_env(config, self.api_keys_env, self.name)
            else:
                self.auth_mode = "sigv4"
                self.pool = _GatewayBedrockCredentialPool.from_env(
                    config, self.credentials_env, self.name)
        self.region = (config.get(self.region_env) if config else None) or self.default_region
        raw = (config.get(self.base_url_env) if config else None) \
            or f"https://bedrock-runtime.{self.region}.amazonaws.com"
        self.base_url = raw.rstrip("/")
        self.models = config.getlist(self.models_env, default=[]) if config else []

    # -- signing --------------------------------------------------------------
    def _sign(self, cred, url: str, payload: bytes) -> dict:
        if self.auth_mode == "bearer":
            secret = self.pool.get_secret_for(cred)
            return {"Content-Type": "application/json",
                   "Authorization": f"Bearer {secret}"}
        ak, _, sk = self.pool.get_secret_for(cred).partition(":")
        return _gw_sign_v4(ak, sk, self.region, "bedrock", "POST", url,
                           "application/json", payload)

    def _post(self, url: str, body: dict, cred) -> dict:
        payload = json.dumps(body).encode("utf-8")
        headers = self._sign(cred, url, payload)
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self.pool.report_failure(cred, reason=f"{self.name} http {e.code}",
                                     auth_failure=e.code in (401, 403),
                                     rate_limited=e.code == 429,
                                     cooldown_s=(45 if e.code == 429 else 30))
            code = e.code
            if code in (401, 403):
                raise ProviderError(f"{self.name} authentication/authorization failed")
            if code == 429:
                raise ProviderError(f"{self.name} rate limit reached")
            raise ProviderError(f"{self.name} http {code}")
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.name} network: {getattr(e, 'reason', e)}") from e
        self.pool.report_success(cred)
        return data

    # -- Converse shapes ------------------------------------------------------
    def _converse_body(self, messages, model, max_tokens) -> dict:
        system = "\n".join(m.get("content", "") for m in messages
                           if m.get("role") == "system")
        convo = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            content = m.get("content", "")
            if isinstance(content, list):
                blocks = [{"text": (b.get("text") if isinstance(b, dict) else str(b))}
                          for b in content]
            else:
                blocks = [{"text": str(content)}]
            convo.append({"role": role, "content": blocks})
        body = {"modelId": model, "messages": convo,
                "inferenceConfig": {"maxTokens": max_tokens}}
        if system:
            body["system"] = [{"text": system}]
        return body

    def chat(self, messages, model=None, max_tokens=500) -> str:
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError(f"{self.name}: no model configured")
        cred = self.pool.pick()
        if cred is None:
            raise ProviderError(f"{self.name}: no healthy credential configured")
        body = self._converse_body(messages, model, max_tokens)
        data = self._post(f"{self.base_url}/model/{model}/converse", body, cred)
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip() or "(no reply)"

    def stream(self, messages, model=None, max_tokens=500):
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError(f"{self.name}: no model configured")
        cred = self.pool.pick()
        if cred is None:
            raise ProviderError(f"{self.name}: no healthy credential configured")
        if self.events:
            self.events.emit("ai.started", agent="gateway", provider=self.name, model=model)
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
            raise ProviderError(f"{self.name} stream http {e.code}") from e
        self.pool.report_success(cred)
        if self.events:
            self.events.emit("ai.completed", agent="gateway", provider=self.name,
                             length=len(full))

    def health_check(self) -> bool:
        return self.pool.healthy_count > 0

    def credential_summary(self) -> dict:
        return self.pool.summary()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# Canonical fallback order: Gemini → Groq → Cloudflare → Bedrock
GATEWAY_CONNECTIONS = (
    AstraGatewayGemini,
    AstraGatewayGroq,
    AstraGatewayCloudflare,
    AstraGatewayBedrock,
)


def _build_connection(cls, config=None):
    """Build one gateway connection iff its own credentials are configured.

    Returns None when the connection's GW_*_API_KEYS / GW_*_CREDENTIALS are
    unset/blank, so an unconfigured connection simply drops out of the
    fallback chain — same convention as every provider adapter.
    """
    if config is None:
        return None
    # gateway connections are independent of provider config; only GW_* counts
    keys = config.getlist(cls.api_keys_env, default=[])
    creds = getattr(cls, "credentials_env", None)
    if not keys and creds:
        keys = config.getlist(creds, default=[])
    if not keys:
        return None
    try:
        return cls(config=config)
    except Exception:
        return None


class AstraAIGateway:
    """Multi-provider, multi-model intelligent-routing gateway.

    Astra AI Gateway has exactly four connections (Gemini, Groq, Cloudflare,
    Bedrock), each of which may expose multiple models (§1). For every
    request the Gateway:

        1. classifies the request (simple/general/reasoning/coding/
           long_context/structured_output/tool_use/vision) — locally,
           deterministically, without spending a model call on it (§10);
        2. filters the full provider+model catalog down to targets that are
           actually suitable (capability/context) and not in cooldown (§2,
           §6, §8);
        3. prefers the last successful target when it's still suitable and
           healthy, otherwise ranks the rest by capability fit, health,
           latency and configured priority (§3, §5, §9, §15);
        4. attempts targets in that order — a model-level failure only
           deprioritizes that (provider, model) pair, never the whole
           connection (§6/§7) — until one succeeds or every suitable target
           has been tried (bounded; no infinite retries).

    A caller may still request a *specific* model explicitly (`model=...`);
    that bypasses the intelligent selection entirely and falls back only
    across connections that actually serve it, in configured order (§17) —
    exactly the original, simpler fallback behavior this class always had.

    See astra/ai/gateway_routing.py for the actual classification/
    scoring/health/persistence logic — this class only owns HTTP execution
    and the attempt loop.
    """

    def __init__(self, connections: list | None = None, config=None,
                store=None, events=None):
        self.name = "astra_ai_gateway"
        self.config = config
        self.events = events
        self.connections = []
        self.last_connection = ""     # name of the most recent successful call
        self.last_model = ""
        self.last_attempts = 0
        self.last_category = ""       # classification of the most recent request
        if connections is not None:
            self.connections = list(connections)
        elif config is not None:
            for cls in GATEWAY_CONNECTIONS:
                conn = _build_connection(cls, config)
                if conn is not None:
                    self.connections.append(conn)
        from astra.ai.gateway_routing import (GatewayRoutingState,
                                              build_gateway_catalog)
        self.routing_state = GatewayRoutingState(store)
        self._catalog = build_gateway_catalog(self.connections)
        # §2-§5, §18: task-level execution recovery for the EXISTING
        # Provider system's own catalog — a completely separate namespace
        # from `self.routing_state` above (which only ever tracks this
        # Gateway's own four GW_* connections). See gateway_recovery.py.
        from astra.ai.gateway_recovery import GatewayExecutionRecovery
        self.execution_recovery = GatewayExecutionRecovery(store=store,
                                                            events=events)

    def attach_events(self, events) -> None:
        self.events = events

    # -- capability -----------------------------------------------------------
    @property
    def models(self) -> list[str]:
        out = []
        for c in self.connections:
            out.extend(c.models or [])
        return out

    def is_usable(self) -> bool:
        """At least one connection has healthy credentials."""
        for c in self.connections:
            pool = getattr(c, "pool", None)
            if pool is not None and bool(pool):
                return True
            try:
                if c.health_check():
                    return True
            except Exception:
                continue
        return bool(self.connections)

    # -- explicit-model fallback (§17: user-requested model is respected) ----
    def _try_connections(self, fn, *, model, messages, max_tokens):
        """Run `fn` over the connections in order; first success wins.

        Used only when the caller names a specific model. Tracks which
        connection/model served the request (`last_connection`,
        `last_model`) and how many connections were attempted before
        success (`last_attempts`) so callers can report the real serving
        path.
        """
        last_error = ""
        attempts = 0
        for conn in self.connections:
            attempts += 1
            self.last_attempts = attempts
            try:
                call_model = model
                if call_model and (conn.models or []) and \
                        call_model not in conn.models:
                    # this connection doesn't serve the requested model —
                    # skip it rather than fail it (fallback intent is a
                    # different model on the next service).
                    continue
                result = fn(conn, call_model)
                self.last_connection = conn.name
                self.last_model = call_model or (conn.models[0] if conn.models else "")
                return result
            except (ProviderError, TimeoutError) as e:
                last_error = getattr(e, "message", None) or str(e)
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue
        self.last_connection = ""
        self.last_model = ""
        raise ProviderError(
            f"Astra AI Gateway: all four services failed —— {last_error}")

    # -- intelligent multi-provider/multi-model selection (§2/§15) -----------
    def _classification_inputs(self, messages) -> tuple[str, int, bool]:
        """Classification text comes ONLY from user-role content — a
        system/instruction message (e.g. GATEWAY_UNDERSTANDING_SYSTEM_PROMPT
        below, or any caller's own system prompt) is Gateway/Provider
        control text, not the user's actual request, and must never leak
        into category detection (a system prompt that merely *mentions*
        "structured"/"code" must not force a structured_output/coding hard
        capability filter). `context_tokens`, by contrast, sums every
        message — the full context genuinely has to fit the model."""
        texts, vision, chars = [], False, 0
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            content = m.get("content", "")
            is_user = m.get("role", "user") == "user"
            if isinstance(content, str):
                chars += len(content)
                if is_user:
                    texts.append(content)
            elif isinstance(content, list):
                # multi-part content (e.g. text + image blocks) — a list
                # content shape is the one clear, non-guessy vision signal
                # available at this layer without inventing capabilities.
                vision = True
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        chars += len(block["text"])
                        if is_user:
                            texts.append(block["text"])
        text = " ".join(texts)
        context_tokens = chars // 4
        return text, context_tokens, vision

    def _select_order(self, messages, max_tokens):
        from astra.ai.gateway_routing import (classify_gateway_request,
                                              eligible_targets, rank_targets,
                                              prefer_last_successful)
        text, context_tokens, vision = self._classification_inputs(messages)
        category = classify_gateway_request(text, vision=vision,
                                            context_tokens=context_tokens)
        self.last_category = category
        targets = eligible_targets(self._catalog, self.routing_state,
                                   category=category,
                                   context_tokens=context_tokens)
        ranked = rank_targets(targets, category=category)
        ranked = prefer_last_successful(ranked, self.routing_state.last_successful())
        return category, ranked

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="gateway", **data)
            except Exception:
                pass

    def chat(self, messages, model=None, max_tokens=500) -> str:
        if model:
            return self._try_connections(
                lambda c, m: c.chat(messages, model=m, max_tokens=max_tokens),
                model=model, messages=messages, max_tokens=max_tokens)

        category, ranked = self._select_order(messages, max_tokens)
        self._emit("astra_gateway.request", category=category,
                   candidates=len(ranked))
        if not ranked:
            self.last_connection = ""
            self.last_model = ""
            self.last_attempts = 0
            raise ProviderError(
                f"Astra AI Gateway: no suitable provider+model available "
                f"for this request (category={category})")

        last_error = ""
        attempts = 0
        for conn, target_model, health in ranked:
            attempts += 1
            self.last_attempts = attempts
            start = time.perf_counter()
            try:
                result = conn.chat(messages, model=target_model.model_id,
                                   max_tokens=max_tokens)
            except (ProviderError, TimeoutError) as e:
                last_error = getattr(e, "message", None) or str(e)
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=last_error)
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=last_error)
                continue
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.routing_state.record_success(target_model.provider,
                                              target_model.model_id, latency_ms)
            self.last_connection = conn.name
            self.last_model = target_model.model_id
            self._emit("astra_gateway.success", provider=target_model.provider,
                      model=target_model.model_id, latency_ms=round(latency_ms, 1))
            return result
        self.last_connection = ""
        self.last_model = ""
        raise ProviderError(
            f"Astra AI Gateway: all suitable targets failed —— {last_error}")

    def stream(self, messages, model=None, max_tokens=500):
        if model:
            # Generator fallback: first connection's stream that starts wins
            # (unchanged explicit-model path — §17).
            for conn in self.connections:
                try:
                    call_model = model
                    if call_model and (conn.models or []) and \
                            call_model not in conn.models:
                        continue
                    for chunk in conn.stream(messages, model=call_model,
                                             max_tokens=max_tokens):
                        yield chunk
                    return
                except (ProviderError, TimeoutError):
                    continue
                except Exception:
                    continue
            return

        category, ranked = self._select_order(messages, max_tokens)
        self._emit("astra_gateway.request", category=category,
                   candidates=len(ranked))
        # §23 streaming recovery: once ANY chunk has reached the caller for
        # this call, we must never silently start a second, independent
        # stream on the next connection — that would concatenate an
        # unrelated model's full response after the partial one already
        # sent, or duplicate it outright. So a target is only ever retried
        # here while emitted_any is still False; once it flips True an
        # interruption ends the call cleanly instead of falling over.
        emitted_any = False
        for conn, target_model, health in ranked:
            start = time.perf_counter()
            try:
                for chunk in conn.stream(messages, model=target_model.model_id,
                                         max_tokens=max_tokens):
                    emitted_any = True
                    yield chunk
            except (ProviderError, TimeoutError) as e:
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                reason = getattr(e, "message", None) or str(e)
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=reason)
                if emitted_any:
                    self._emit("astra_gateway.stream_interrupted",
                              provider=target_model.provider,
                              model=target_model.model_id, reason=reason,
                              partial_content_sent=True)
                    return
                continue
            except Exception as e:
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                reason = f"{type(e).__name__}: {e}"
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=reason)
                if emitted_any:
                    self._emit("astra_gateway.stream_interrupted",
                              provider=target_model.provider,
                              model=target_model.model_id, reason=reason,
                              partial_content_sent=True)
                    return
                continue
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.routing_state.record_success(target_model.provider,
                                              target_model.model_id, latency_ms)
            self.last_connection = conn.name
            self.last_model = target_model.model_id
            self._emit("astra_gateway.success", provider=target_model.provider,
                      model=target_model.model_id, latency_ms=round(latency_ms, 1))
            return
        self.last_connection = ""
        self.last_model = ""

    # -- health (reported separately, never as a provider) --------------------
    def health(self) -> dict:
        model_health_by_conn: dict[str, dict] = {}
        for conn, model in self._catalog:
            h = self.routing_state.get_health(model.provider, model.model_id)
            model_health_by_conn.setdefault(conn.name, {})[model.model_id] = h.to_dict()
        out = {}
        for c in self.connections:
            pool = getattr(c, "pool", None)
            if pool is not None:
                state = ("healthy" if bool(pool)
                         else ("not_configured" if pool.count == 0 else "degraded"))
            else:
                state = "not_configured"
            out[c.name] = {
                "state": state,
                "models": list(c.models or []),
                "base_url": getattr(c, "base_url", ""),
                "model_health": model_health_by_conn.get(c.name, {}),
            }
        return out

    def routing_status(self) -> dict:
        """Gateway-routing-specific status: last successful target + every
        tracked (provider, model) health row. Separate from `health()` so
        that method's shape (consumed by AstraRouter.gateway_health()) never
        has to change."""
        return {
            "last_category": self.last_category,
            "last_connection": self.last_connection,
            "last_model": self.last_model,
            "last_attempts": self.last_attempts,
            **self.routing_state.snapshot(),
        }

    # ═══════════════════════════════════════════════════════════════════
    # §3-§5, §18: Gateway recovery API for the EXISTING Provider system.
    #
    # These four methods are the entire surface the Provider system needs
    # to let the Gateway own routing/recovery *decisions* while the
    # Provider system keeps owning actual execution + credentials (§2,
    # §15, §21). Every argument/return value here is a
    # `ProviderExecutionTarget` (astra/ai/gateway_contract.py) — plain
    # provider_id/model_id/capabilities metadata. No adapter, credential,
    # HTTP client or ProviderRegistry object ever crosses this boundary.
    # ═══════════════════════════════════════════════════════════════════
    def select_execution_target(self, candidates, *, required_capabilities=(),
                                exclude=None):
        """Pick the best eligible (provider_id, model_id) target for the
        Existing Provider system to execute against, or None if nothing in
        `candidates` is currently eligible (out of cooldown, capability
        match, not already excluded)."""
        return self.execution_recovery.select_execution_target(
            candidates, required_capabilities=required_capabilities,
            exclude=exclude)

    def report_execution_success(self, target, latency_ms: float = 0.0) -> None:
        """Record that `target` (a ProviderExecutionTarget) just succeeded —
        clears its cooldown and becomes the new soft last-successful
        preference (§12)."""
        self.execution_recovery.report_execution_success(target, latency_ms)

    def report_execution_failure(self, target, category: str,
                                 cooldown_s: float | None = None) -> None:
        """Record that `target` just failed with §7 category `category` —
        cools down that (provider, model) pair only (§6/§8), never the
        whole provider."""
        self.execution_recovery.report_execution_failure(
            target, category, cooldown_s=cooldown_s)

    def recover_execution_target(self, candidates, failed_target, category, *,
                                 required_capabilities=(), exclude=None):
        """Report `failed_target`'s failure, then select the next suitable
        target from `candidates` (excluding `failed_target` and anything in
        `exclude`) — the single call the Existing Provider system needs on
        a mid-task AI-provider failure to keep going without restarting the
        task (§5, §9)."""
        return self.execution_recovery.recover_execution_target(
            candidates, failed_target, category,
            required_capabilities=required_capabilities, exclude=exclude)


def build_astra_ai_gateway(config=None, store=None,
                          events=None) -> AstraAIGateway | None:
    """Build the Astra AI Gateway iff at least one connection is configured.

    Returns None when no GW_* connection credentials are set, mirroring the
    old "unconfigured means absent" convention. The returned object is the
    router's `gateway`; it is never added to ProviderRegistry.

    `store`, when given, is the project's existing SQLite Store — used only
    to persist last-successful-target and per-model health (§14) across
    restarts; no new database is introduced, and no credentials are ever
    written to it.
    """
    from astra.core.config import Config
    config = config or Config()
    gw = AstraAIGateway(config=config, store=store, events=events)
    if not gw.connections:
        return None
    return gw


# ═══════════════════════════════════════════════════════════════════════════
# Request Intelligence — the Gateway as an AI Request Intelligence Layer
# ═══════════════════════════════════════════════════════════════════════════
#
#     User -> Assistant -> Astra AI Gateway -> Request Understanding
#          -> Request Enrichment/Structuring -> Provider-Ready Prompt
#          -> Existing Provider System -> Provider AI -> Final Response
#          -> Assistant -> User
#
# The Gateway's only job here is to turn a raw, possibly short/incomplete/
# poorly-structured/mixed-language user message into a clearer, better
# structured instruction — using its OWN four connections (GW_* config,
# AstraAIGateway.chat() above) — and hand that improved text back to the
# caller. It never calls a Provider adapter, never touches ProviderRegistry
# or AstraRouter, and never performs the user's actual task itself: the
# existing Provider system remains the sole executor. Gateway -> Provider
# (as a plain text handoff, done by the caller) is fine; Provider -> Gateway
# and Gateway -> ProviderRegistry are both absent by design, same as the
# rest of this module.

GATEWAY_UNDERSTANDING_SYSTEM_PROMPT = (
    "You are the Astra AI Gateway's Request Understanding layer. You do NOT "
    "answer the user's request and you do NOT perform the task yourself — a "
    "separate Provider AI does that after you, using only what you output. "
    "Your only job is to turn the user's raw message (which may be short, "
    "incomplete, poorly structured, or mixed Bengali/Banglish/English) into "
    "a clear, well-structured instruction for that Provider AI to execute.\n\n"
    "Rules:\n"
    "- Preserve the user's original intent and goal exactly — never change "
    "what they actually asked for.\n"
    "- Preserve important wording, numbers, names, and explicit "
    "requirements from the message.\n"
    "- Identify constraints already present in the request and keep them.\n"
    "- Organize unclear or fragmented instructions into clear structure.\n"
    "- Add useful structure or context only when it can be safely inferred "
    "from the message itself. NEVER invent facts, credentials, "
    "requirements, or user intentions that are not present in the message.\n"
    "- If information needed to complete the task is genuinely missing and "
    "cannot be safely inferred, say plainly that it is missing instead of "
    "guessing or making it up.\n"
    "- Match the amount of structure to the request: a simple message "
    "(e.g. \"hello\") gets a short, simple rewrite — do not produce a large "
    "structured prompt for a simple request. A complex or unclear task "
    "gets useful structure (for example USER TASK / OBJECTIVE / "
    "INSTRUCTIONS sections).\n"
    "- You may also receive a block of recent prior conversation, for "
    "understanding only. Use it strictly to resolve references in the "
    "current message (e.g. \"eita\", \"that one\", \"amar age bola ta\") "
    "and to keep meaning consistent with what came before. Never pull new "
    "instructions, facts, or requirements out of the prior conversation "
    "that the current message does not actually reference, and never let "
    "it override or expand what the current message asks for.\n"
    "- Output ONLY the rewritten request text for the Provider AI — no "
    "preamble, no explanation, no meta-commentary about what you changed."
)

GW_UNDERSTANDING_MAX_TOKENS = 400


class GatewayRequestIntelligence:
    """Request Understanding / Enrichment: the Gateway's preprocessing step.

    `process()` sends the user's raw text to the Gateway's own AI
    connections (via `AstraAIGateway.chat()` — Gemini -> Groq -> Cloudflare
    -> Bedrock fallback, GW_* config only) and returns a provider-ready
    version of the request. This class does not execute the task and is
    never registered as a Provider: callers hand its output to the existing
    Provider system (e.g. `AstraRouter.route(...)`) for actual execution.

    Fails open: if the Gateway is absent, unconfigured, or every connection
    errors, `process()` returns the original text unchanged
    (`enriched=False`) rather than inventing content or blocking the
    request — the Assistant -> Provider path keeps working exactly as it
    did before this layer existed.
    """

    def __init__(self, gateway: "AstraAIGateway | None"):
        self.gateway = gateway

    def is_usable(self) -> bool:
        return self.gateway is not None and self.gateway.is_usable()

    def process(self, raw_text: str, *,
               max_tokens: int = GW_UNDERSTANDING_MAX_TOKENS,
               context: str = "") -> dict:
        """Understand + structure `raw_text` into a Provider-ready prompt.

        `context`, when given, is recent prior Assistant conversation
        relevant to `raw_text` (e.g. what the current short message refers
        back to). It is passed to the Gateway purely so it can resolve
        references in `raw_text` — it is never treated as a new request of
        its own, never echoed back verbatim, and is dropped entirely when
        empty (the request/response shape is identical to calling without
        it, so existing callers are unaffected).

        Returns {"text": str, "enriched": bool, "gateway_connection": str,
        "raw_text": str}. `text` is always safe to hand straight to the
        Provider system: either the Gateway's improved version, or (on any
        failure/absence) the original raw text.
        """
        text = " ".join(str(raw_text or "").split()).strip()
        if not text or not self.is_usable():
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        ctx = " ".join(str(context or "").split()).strip()
        messages = [
            {"role": "system", "content": GATEWAY_UNDERSTANDING_SYSTEM_PROMPT},
        ]
        if ctx:
            messages.append({"role": "user", "content":
                             "Relevant prior conversation (context only — "
                             "do not treat as a new request):\n" + ctx})
        messages.append({"role": "user", "content": text})
        try:
            improved = self.gateway.chat(messages, max_tokens=max_tokens)
        except Exception:
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        improved = (improved or "").strip()
        if not improved or improved == "(no reply)":
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        return {"text": improved, "enriched": True,
                "gateway_connection": self.gateway.last_connection,
                "raw_text": text}


def build_gateway_request_intelligence(
        gateway: "AstraAIGateway | None") -> GatewayRequestIntelligence:
    """Wrap a (possibly None) Gateway in the request-intelligence layer.

    Always returns an object — `GatewayRequestIntelligence.process()`
    degrades gracefully to a pass-through when `gateway` is None or
    unusable, so callers never need a None-check of their own.
    """
    return GatewayRequestIntelligence(gateway)