"""Gemini adapter — uses Gemini's OpenAI-compatible endpoint, so the shared
compatible base handles chat/stream/discovery unchanged."""
from __future__ import annotations

from .base import CompatibleAdapter


class GeminiAdapter(CompatibleAdapter):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    models_env = "GEMINI_MODELS"
    base_url_env = "GEMINI_BASE_URL"
    api_keys_env = "GEMINI_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json", "vision"]