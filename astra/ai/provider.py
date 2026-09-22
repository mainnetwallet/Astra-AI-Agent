"""AIProvider abstraction + the two backward-compatible single-provider paths.

Every provider exposes `chat(messages)`, optional streaming, a model list and
a cheap health check, so the AstraRouter can select between them under one
interface. Credentials come from config/env — never hardcoded, never logged.

Provider families
-----------------
* **Modern adapters (the default, recommended path)** — the ten modules under
  `astra/ai/adapters/`: gemini, groq, mistral, openrouter, cerebras,
  cloudflare, sambanova, cohere and zai (all built on `CompatibleAdapter`),
  plus bedrock (AWS SigV4 / Converse). They are credential-pool based, seeded
  into the model registry from `<PROVIDER>_API_KEYS` / `<PROVIDER>_MODELS`,
  and are what `astra/ai/registry.py` builds by default.
* **Backward-compatible providers (this module)** — `ClaudeProvider` (one
  Anthropic key via `ANTHROPIC_API_KEY`) and `OpenAICompatibleProvider` (one
  generic OpenAI-style endpoint via `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY`).
  Both still join routing when configured — they are the legacy single-provider
  path, not part of the modern adapter set.
"""
from __future__ import annotations

import json
import urllib.request

from astra.ai.system_prompt import build_system_prompt
from astra.ai.token_limits import resolve_output_tokens
from astra.core.exceptions import ProviderError


def close_http_error(exc) -> None:
    """Close an `urllib.error.HTTPError` response body.

    `urlopen()` raises before the `with` block is entered, so the HTTPError
    (which owns the socket/response body) is never closed by the caller. Left
    to the garbage collector it keeps the connection open until GC and emits a
    ResourceWarning; closing it here releases it deterministically. Safe to
    call with any object (no-op when it has no `close`)."""
    try:
        close = getattr(exc, "close", None)
        if close is not None:
            close()
    except Exception:
        pass


class AIProvider:
    name: str = "base"
    models: list[str] = []
    base_url: str = ""
    capabilities: list[str] = ["chat"]

    def __init__(self, config=None):
        self.config = config

    def chat(self, messages: list[dict], model: str | None = None,
             max_tokens: int | None = None) -> str:
        raise NotImplementedError

    def stream(self, messages: list[dict], model: str | None = None):
        """Yield text chunks. Default: one yield with the full reply."""
        yield self.chat(messages, model)

    def generate_image(self, prompt: str, model: str | None = None, **kw) -> str:
        raise ProviderError(f"{self.name} does not support image generation")

    def text_to_speech(self, text: str, model: str | None = None, **kw) -> str:
        raise ProviderError(f"{self.name} does not support text-to-speech")

    def health_check(self) -> bool:
        return True


def _flatten(messages: list[dict]) -> str:
    """Turn a message list into one prompt for stateless providers."""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        content = "".join(b.get("text", "") for b in content) if isinstance(content, list) else content
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _read_sse(resp) -> list[dict]:
    """Parse Server-Sent Events from an HTTP response into JSON dicts.

    Handles both Anthropic format (event: .../data: ...) and OpenAI format
    (data: {...} / data: [DONE]). Lines without 'data:' prefix are ignored.
    Empty data lines and [DONE] sentinel terminate the stream gracefully.
    """
    results = []
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return results


class ClaudeProvider(AIProvider):
    """Backward-compatible single-key Anthropic (Claude) provider.

    Instantiated only when `ANTHROPIC_API_KEY` is set (model via
    `ANTHROPIC_MODEL`). It is not one of the ten modern adapters in
    `astra/ai/adapters/` and is not seeded from the model-registry env vars.
    """
    name = "anthropic"
    base_url = "https://api.anthropic.com/v1/messages"

    def __init__(self, config=None, api_key: str | None = None,
                 model: str | None = None, events=None, base_url: str | None = None):
        super().__init__(config)
        self.api_key = api_key or (config.get("ANTHROPIC_API_KEY") if config else None)
        self.models = [model or (config.get("ANTHROPIC_MODEL") if config else None)
                       or "claude-haiku-4-5-20251001"]
        self.base_url = base_url or self.base_url  # allow proxies / tests / gateway
        self.capabilities = ["chat", "stream"]
        self.events = events

    def _build_body(self, messages, model, max_tokens) -> dict:
        # Anthropic's Messages API REQUIRES max_tokens. When the caller has
        # no explicit budget, derive one from the selected model's own
        # capability (astra.ai.token_limits) instead of a fixed small
        # number; when the caller DOES pass one, honour it verbatim.
        max_tokens = resolve_output_tokens(
            max_tokens, provider=self.name,
            model_meta={"model": model or self.models[0]})
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user = _flatten([m for m in messages if m.get("role") != "system"])
        # No caller-supplied system message: fall back to the same
        # centralized Astra Core System Prompt every other call path uses,
        # instead of a second, divergent hardcoded identity string. This
        # only fires when a caller sends zero system-role messages — every
        # current call path always supplies one via
        # `astra.ai.system_prompt.build_system_prompt`.
        return {
            "model": model or self.models[0],
            "max_tokens": max_tokens,
            "system": system or build_system_prompt(),
            "messages": [{"role": "user", "content": user}],
        }

    def _request_headers(self) -> dict:
        return {
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def chat(self, messages: list[dict], model: str | None = None,
             max_tokens: int | None = None) -> str:
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY not set")
        body = json.dumps(self._build_body(messages, model, max_tokens)).encode()
        req = urllib.request.Request(self.base_url, data=body,
                                    headers=self._request_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            close_http_error(e)
            raise ProviderError(f"anthropic unavailable: {type(e).__name__}") from e
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip() or "(no reply)"

    def stream(self, messages: list[dict], model: str | None = None,
               max_tokens: int | None = None):
        """Real Anthropic streaming via SSE — yields token-sized text chunks."""
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY not set")
        if self.events:
            self.events.emit("ai.started", agent="provider",
                             provider=self.name, model=model or self.models[0])
        body = json.dumps({**self._build_body(messages, model, max_tokens),
                           "stream": True}).encode()
        req = urllib.request.Request(self.base_url, data=body,
                                    headers=self._request_headers())
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                full = ""
                for chunk in _read_sse(resp):
                    text = chunk.get("delta", {}).get("text", "")
                    if text:
                        full += text
                        yield text
            if self.events:
                self.events.emit("ai.completed", agent="provider",
                                 provider=self.name,
                                 model=model or self.models[0],
                                 length=len(full))
        except Exception as e:
            if self.events:
                self.events.emit("ai.failed", agent="provider",
                                 provider=self.name,
                                 model=model or self.models[0], error=str(e))
            close_http_error(e)
            raise ProviderError(f"anthropic unavailable: {type(e).__name__}") from e

    def health_check(self) -> bool:
        return bool(self.api_key)


class OpenAICompatibleProvider(AIProvider):
    """Backward-compatible generic OpenAI-compatible provider.

    Works with OpenAI, Ollama, LM Studio, DeepSeek, or any OpenAI-style
    endpoint. Uses AI_BASE_URL / AI_MODEL / AI_API_KEY (label via
    AI_PROVIDER_LABEL). It is the legacy single-endpoint path, separate from
    the ten modern adapters in `astra/ai/adapters/`.

    Zero external dependencies — pure stdlib urllib with SSE parsing.
    """
    name = "openai"
    base_url = "https://api.openai.com/v1"

    def __init__(self, config=None, events=None):
        super().__init__(config)
        self.base_url = (config.get("AI_BASE_URL") if config else None) or self.base_url
        self.api_key = (config.get("AI_API_KEY") if config else None) or ""
        self.models = [(config.get("AI_MODEL") if config else None) or "gpt-4o-mini"]
        self.name = (config.get("AI_PROVIDER_LABEL") if config else None) or "openai"
        self.capabilities = ["chat", "stream"]
        self.events = events

    def _headers(self) -> dict:
        h = {"content-type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(self, messages: list[dict], model: str | None = None,
             max_tokens: int | None = None) -> str:
        payload = {"model": model or self.models[0], "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions",
                                    data=body, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            close_http_error(e)
            raise ProviderError(f"{self.name} unavailable: {type(e).__name__}") from e
        return data.get("choices", [{}])[0].get("message", {}).get("content", "") or "(no reply)"

    def stream(self, messages: list[dict], model: str | None = None,
               max_tokens: int | None = None):
        """Real streaming via OpenAI-compatible SSE."""
        if self.events:
            self.events.emit("ai.started", agent="provider",
                             provider=self.name, model=model or self.models[0])
        payload = {"model": model or self.models[0], "messages": messages,
                   "stream": True}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions",
                                    data=body, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                full = ""
                for chunk in _read_sse(resp):
                    choices = chunk.get("choices", [])
                    delta = choices[0].get("delta", {}) if choices else {}
                    text = delta.get("content", "")
                    if text:
                        full += text
                        yield text
            if self.events:
                self.events.emit("ai.completed", agent="provider",
                                 provider=self.name,
                                 model=model or self.models[0],
                                 length=len(full))
        except Exception as e:
            if self.events:
                self.events.emit("ai.failed", agent="provider",
                                 provider=self.name,
                                 model=model or self.models[0], error=str(e))
            close_http_error(e)
            raise ProviderError(f"{self.name} unavailable: {type(e).__name__}") from e

    def health_check(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[str]:
        """Best-effort model list (GET /models); graceful fallback."""
        req = urllib.request.Request(f"{self.base_url}/models",
                                    headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return list(self.models)
