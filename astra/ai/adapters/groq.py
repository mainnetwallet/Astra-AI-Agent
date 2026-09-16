"""Groq adapter."""
from __future__ import annotations

from .base import CompatibleAdapter


class GroqAdapter(CompatibleAdapter):
    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    models_env = "GROQ_MODELS"
    base_url_env = "GROQ_BASE_URL"
    api_keys_env = "GROQ_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json"]