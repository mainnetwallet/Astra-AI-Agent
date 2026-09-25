"""Shared OpenAI-compatible provider adapter.

Most Astra providers speak the OpenAI chat/completions dialect (or a superset
of it): Gemini, Groq, Mistral, OpenRouter, Cerebras, Cloudflare, SambaNova,
Cohere and Z.AI all accept `{model, messages, max_tokens}` and return
`choices[].message.content`, with SSE `data:` frames when `stream: true`.

This base adapter normalises request/response/streaming/errors/usage across
them: one interface for the AstraRouter, provider-specific details confined to
a small class per provider (name, base_url, model list, headers).

Credentials come from a CredentialPool (multi-key with health/cooldown) — a
secret is picked per call, used once, and reported back. Secrets never enter
logs, events or stats.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from astra.ai.credentials import CredentialPool
from astra.ai.image_payload import (image_result_to_data_uri,
                                    _guess_image_mime)
from astra.ai.provider import AIProvider, _read_sse, close_http_error
from astra.core.exceptions import ProviderError, TimeoutError

DEFAULT_TIMEOUT = 60
STREAM_TIMEOUT = 120




class CompatibleAdapter(AIProvider):
    """OpenAI-compatible provider with credential-pool + normalized errors."""

    name: str = "compatible"
    models: list[str] = []
    base_url: str = ""                       # e.g. https://api.groq.com/openai/v1
    capabilities: list[str] = ["chat", "stream", "tools", "json"]
    extra_headers: dict = {}                  # static headers e.g. api-key
    models_env: str = ""                      # env var holding the model list
    image_models: list[str] = []               # configured image-gen model ids
    image_models_env: str = ""                 # env var holding image model list
    api_keys_env: str = ""                    # env var holding the key list
    base_url_env: str = ""                    # env var overriding base_url

    def __init__(self, config=None, events=None, pool: CredentialPool | None = None):
        super().__init__(config)
        self.events = events
        self.pool = pool or CredentialPool.from_env(config, self.api_keys_env, self.name)
        self.models = self._configured_models()
        self.image_models = self._configured_image_models()
        self.base_url = self._configured_base_url()
        self._health = None
        self._health_at = 0.0

    # -- configuration --------------------------------------------------------
    def _configured_models(self) -> list[str]:
        if self.config and self.models_env:
            return self.config.getlist(self.models_env, default=[])
        return list(self.models)

    def _configured_image_models(self) -> list[str]:
        """Image-generation models are configured SEPARATELY from the chat
        list (an image model is not a chat model). Never inferred from the
        model id -- astra.ai.image_models decides what may run."""
        if self.config and self.image_models_env:
            return self.config.getlist(self.image_models_env, default=[])
        return list(self.image_models)

    def _default_image_model(self) -> str:
        return (self.image_models[0] if self.image_models else "")

    def _configured_base_url(self) -> str:
        """Env override wins when set, else the class default. Normalises a
        trailing slash so callers can safely do f"{base_url}/chat/completions"
        without producing "//chat/completions" or double "/v1/v1"."""
        raw = None
        if self.config and self.base_url_env:
            raw = self.config.get(self.base_url_env, None)
        raw = (raw or self.base_url or "").rstrip("/")
        return raw

    # -- credential handling --------------------------------------------------
    def _pick(self, model: str | None = None):
        return self.pool.pick(model)

    def _no_credential_error(self) -> ProviderError:
        """No usable key right now (none configured, or every key is cooling
        down for 30s+). Retrying the same provider within the router's 1-2s
        backoff cannot change that, so it is marked NON-retryable: the router
        moves straight to the next candidate instead of sleeping through
        pointless retries (the Gateway's own connections already do this)."""
        err = ProviderError(f"{self.name}: no healthy credential configured")
        err.retryable = False
        return err

    def _done(self, cred=None, errored=False, reason="", *, rate_limited=False,
              auth_failure=False, cooldown_s: float = 30.0) -> None:
        if cred is None:
            return
        if errored:
            self.pool.report_failure(cred, reason=reason, rate_limited=rate_limited,
                                     auth_failure=auth_failure, cooldown_s=cooldown_s)
        else:
            self.pool.report_success(cred)

    # -- request plumbing -----------------------------------------------------
    def _headers(self, cred) -> dict:
        h = {"content-type": "application/json", **dict(self.extra_headers)}
        secret = self.pool.get_secret_for(cred) if cred else ""
        if secret:
            h["Authorization"] = f"Bearer {secret}"
        return h

    def _post(self, url: str, body: dict, cred) -> dict:
        return self._post_json(url, body, cred, headers=self._headers(cred))

    def _post_json(self, url: str, body: dict, cred, *,
                   headers: dict | None = None,
                   timeout: int = DEFAULT_TIMEOUT) -> dict:
        """POST JSON with explicit headers (non-OpenAI auth, e.g. Gemini's
        x-goog-api-key) while keeping the SAME credential-health, rate-limit
        and error classification every other call uses."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=headers or self._headers(cred))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            close_http_error(e)
            self._classify_http(e, cred)
            raise
        except urllib.error.URLError as e:
            self._done(cred, True, reason=f"network: {getattr(e, 'reason', e)}")
            raise ProviderError(f"{self.name} network error: {getattr(e, 'reason', e)}") from e
        except TimeoutError as e:
            raise e
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            raise ProviderError(f"{self.name} bad json response") from e

    def _api_base(self) -> str:
        """Base URL for ONE request. Adapters that need a per-request base
        (e.g. Cloudflare's per-account path) override this instead of
        mutating ``self.base_url`` — the adapter is shared by concurrent
        request threads, so mutating shared state corrupts other calls."""
        return self.base_url

    def _classify_http(self, e: urllib.error.HTTPError, cred) -> None:
        code = getattr(e, "code", 0)
        rate_limited = code in (408, 429)
        auth = code in (401, 403)
        # 400/404/409/422 describe THIS request/model (bad params, model
        # removed, ...), not the API key. Cooling the key down for them
        # blocks every other model on the provider (single-key setups
        # then report "no healthy credential configured" for all of them).
        request_level = code in (400, 404, 409, 422)
        self._done(cred, True, reason=f"http {code}", rate_limited=rate_limited,
                   auth_failure=auth,
                   cooldown_s=(0.0 if request_level else 45 if rate_limited else 30))
        if code == 400:
            err = ProviderError(f"{self.name} invalid request")
        elif code == 401:
            err = ProviderError(f"{self.name} authentication failed")
        elif code == 403:
            err = ProviderError(f"{self.name} authorization denied")
        elif code in (404,):
            err = ProviderError(f"{self.name} model not found")
        elif code == 408:
            err = TimeoutError(f"{self.name} timed out")
        elif code == 409:
            err = ProviderError(f"{self.name} conflict")
        elif code == 429:
            err = ProviderError(f"{self.name} rate limit reached")
        elif code == 500:
            err = ProviderError(f"{self.name} provider error")
        elif code in (502, 503, 504):
            err = ProviderError(f"{self.name} temporary provider error")
        else:
            err = ProviderError(f"{self.name} http {code}")
        # Attach the real HTTP status so the Activity Log can report the
        # provider API's status code without parsing the message text (the
        # image-generation API-call logging reads `err.code`).
        err.code = int(code)
        raise err

    # -- interface ------------------------------------------------------------
    def chat(self, messages, model=None, max_tokens=None,
              response_format: str | None = None) -> str:
        cred = self._pick(model or (self.models[0] if self.models else ""))
        if cred is None:
            raise self._no_credential_error()
        body = {"model": model or (self.models[0] if self.models else ""),
                "messages": messages}
        # OpenAI-compatible APIs treat the output limit as optional: omitting
        # it lets the model use its own maximum, so an unset budget is left
        # out entirely rather than replaced with an invented number. See
        # astra.ai.token_limits.
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        # OpenAI-compatible JSON mode. Prompt text alone ("return ONLY
        # JSON") is not enough for chatty/"reasoning" free models (e.g.
        # nvidia/nemotron-3.5-lightning:free on OpenRouter) — they burn
        # the whole max_tokens budget narrating a "thinking process" and
        # never emit JSON at all, which drives the Gateway's correction
        # loop to exhaustion with nothing to show the user. When the
        # caller needs structured output, ask the API to enforce it at
        # the provider level too. Providers/models that don't support
        # this field ignore it; we never fail because of it.
        if response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        data = self._post(f"{self._api_base()}/chat/completions", body, cred)
        self._done(cred)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        self._last_usage = data.get("usage", {})
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        return text.strip() or "(no reply)"

    def stream(self, messages, model=None, max_tokens=None):
        used_model = model or (self.models[0] if self.models else "")
        cred = self._pick(used_model)
        if cred is None:
            raise self._no_credential_error()
        if self.events:
            self.events.emit("ai.started", agent="provider", provider=self.name,
                             model=used_model)
        body = {"model": used_model, "messages": messages, "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self._api_base()}/chat/completions",
                                     data=data, headers=self._headers(cred))
        full = ""
        try:
            with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT) as resp:
                for chunk in _read_sse(resp):
                    choices = chunk.get("choices", [])
                    delta = choices[0].get("delta", {}) if choices else {}
                    text = delta.get("content", "")
                    if text:
                        full += text
                        yield text
        except urllib.error.HTTPError as e:
            self._emit_stream_failed(used_model, f"http {getattr(e, 'code', '?')}")
            close_http_error(e)
            self._classify_http(e, cred)
            raise
        except urllib.error.URLError as e:
            self._emit_stream_failed(used_model, "stream network error")
            self._done(cred, True, reason="stream network error")
            raise ProviderError(f"{self.name} stream network error") from e
        self._done(cred)
        if self.events:
            self.events.emit("ai.completed", agent="provider", provider=self.name,
                             model=used_model, length=len(full))

    def _emit_stream_failed(self, model: str, error: str) -> None:
        """Terminal event for a failed streamed call (the success path already
        emits ai.completed) so the Logs panel counts it exactly once."""
        if self.events:
            self.events.emit("ai.failed", agent="provider", provider=self.name,
                             model=model, error=error)

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image through the provider's REAL image API.

        Default implementation: the OpenAI-compatible Images API
        (`POST /images/generations` + `response_format=b64_json`), the
        documented image API for Z.AI's GLM-Image/CogView. Providers with a
        different documented protocol override this method, so protocol
        ownership is explicit:
          * OpenRouter -> ``POST /api/v1/images`` (dedicated Images API)
          * Gemini     -> native ``models/<id>:generateContent``
          * Cloudflare -> Workers AI ``/ai/run/<model>``
          * Bedrock    -> ``InvokeModel``
        OpenRouter must NOT fall back to this OpenAI-compatible path.

        Returns a `data:<mime>;base64,<...>` string -- the exact shape
        `astra.ai.artifact_extraction` turns into a real image artifact.
        A 429/5xx/network failure propagates as ProviderError so the router
        can cool this (provider, model) down and fail over to the next
        eligible image target; it never silently degrades to chat().
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
        body = {
            "model": model,
            "prompt": prompt,
            "n": max(1, int(n or 1)),
            "size": size,
            "response_format": "b64_json",
        }
        t0 = time.perf_counter()
        data = self._post(f"{self._api_base()}/images/generations", body, cred)
        self._done(cred)
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        uri = image_result_to_data_uri(data)
        if not uri:
            raise ProviderError(
                f"{self.name}: image generation returned no image data")
        return uri

    def text_to_speech(self, text: str, model: str | None = None,
                       voice: str = "alloy") -> str:
        """Generate audio via /v1/audio/speech (OpenAI-compatible). Returns base64 data URI."""
        import base64 as b64mod
        cred = self._pick()
        if cred is None:
            raise self._no_credential_error()
        body = {
            "model": model or "tts-1",
            "input": text,
            "voice": voice,
        }
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._api_base()}/audio/speech",
            data=payload, headers=self._headers(cred))
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                audio_bytes = resp.read()
        except urllib.error.HTTPError as e:
            close_http_error(e)
            self._classify_http(e, cred)
            raise
        except urllib.error.URLError as e:
            self._done(cred, True, reason=f"network: {getattr(e, 'reason', e)}")
            raise ProviderError(f"{self.name} network error: {getattr(e, 'reason', e)}") from e
        self._done(cred)
        self._last_latency_ms = int((time.perf_counter() - t0) * 1000)
        if not audio_bytes or len(audio_bytes) < 100:
            raise ProviderError(f"{self.name}: TTS returned empty audio")
        b64 = b64mod.b64encode(audio_bytes).decode("ascii")
        return f"data:audio/mpeg;base64,{b64}"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def estimate_cost(self, text: str, usage: dict | None = None) -> float:
        usage = usage or getattr(self, "_last_usage", None) or {}
        inp = int(usage.get("prompt_tokens") or 0) or self.count_tokens(text)
        out = int(usage.get("completion_tokens") or 0) or max(1, len(text) // 4)
        # rough per-mil basis, normalised by family — presented as *estimate*
        return (inp + out) * 0.25e-6 * self.cost_multiplier_estimate

    @property
    def cost_multiplier_estimate(self) -> float:
        return 1.0

    def health_check(self) -> bool:
        if not self.pool:
            return False
        return True

    def list_models(self) -> list[str]:
        """Best-effort GET /models → ids; falls back to configured list."""
        cred = self.pick_for_discovery()
        try:
            req = urllib.request.Request(f"{self._api_base()}/models",
                                         headers=self._headers(cred))
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return list(self.models)

    def pick_for_discovery(self):
        return self.pool.pick()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def credential_summary(self) -> dict:
        return self.pool.summary()

    def stats(self) -> dict:
        return {"calls": self.pool.summary().get("calls", 0),
                "errors": self.pool.summary().get("errors", 0)}
