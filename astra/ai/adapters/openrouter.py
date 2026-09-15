"""OpenRouter adapter."""
from __future__ import annotations

from .base import CompatibleAdapter


class OpenRouterAdapter(CompatibleAdapter):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    models_env = "OPENROUTER_MODELS"
    api_keys_env = "OPENROUTER_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json", "vision"]
    extra_headers = {
        "HTTP-Referer": "https://github.com/mainnetwallet/Astra-AI-Agent",
        "X-Title": "Astra AI Agent",
    }