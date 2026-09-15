"""Mistral adapter."""
from __future__ import annotations

from .base import CompatibleAdapter


class MistralAdapter(CompatibleAdapter):
    name = "mistral"
    base_url = "https://api.mistral.ai/v1"
    models_env = "MISTRAL_MODELS"
    api_keys_env = "MISTRAL_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json", "coding"]