"""AIProvider abstraction.

A provider exposes a `chat(messages)`, optional streaming, a model list and a
cheap health check. The router selects between them under one interface, so
swapping Anthropic for OpenAI/Ollama/anything later is a config change, not a
code change. Credentials come from config/env — never hardcoded, never logged.
"""
from __future__ import annotations

import json
import urllib.request

from astra.core.exceptions import ProviderError
from astra.core.timeutil import duration_ms, ms_now


class AIProvider:
    name: str = "base"
    models: list[str] = []
    base_url: str = ""
    capabilities: list[str] = ["chat"]

    def __init__(self, config=None):
        self.config = config

    def chat(self, messages: list[dict], model: str | None = None,
             max_tokens: int = 500) -> str:
        raise NotImplementedError

    def stream(self, messages: list[dict], model: str | None = None):
        """Yield text chunks. Default: one yield with the full reply."""
        yield self.chat(messages, model)

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


class ClaudeProvider(AIProvider):
    name = "anthropic"
    base_url = "https://api.anthropic.com/v1/messages"

    def __init__(self, config=None, api_key: str | None = None, model: str | None = None):
        super().__init__(config)
        self.api_key = api_key or (config.get("ANTHROPIC_API_KEY") if config else None)
        self.models = [model or (config.get("ANTHROPIC_MODEL") if config else None)
                       or "claude-haiku-4-5-20251001"]
        self.capabilities = ["chat", "stream"]

    def chat(self, messages: list[dict], model: str | None = None,
             max_tokens: int = 500) -> str:
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY not set")
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user = _flatten([m for m in messages if m.get("role") != "system"])
        body = json.dumps({
            "model": model or self.models[0],
            "max_tokens": max_tokens,
            "system": system or "You are Astra AI Agent, keep answers short, "
                               "use Banglish when the user writes Banglish.",
            "messages": [{"role": "user", "content": user[:12000]}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url, data=body, headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise ProviderError(f"anthropic unavailable: {type(e).__name__}") from e
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip() or "(no reply)"

    def health_check(self) -> bool:
        return bool(self.api_key)


class OfflineProvider(AIProvider):
    """Always-unavailable provider used as the explicit last fallback so the
    router never spins forever; orchestrator then serves locally."""
    name = "offline"

    def chat(self, messages, model=None, max_tokens=500):
        raise ProviderError("offline provider — no AI configured")

    def health_check(self) -> bool:
        return False