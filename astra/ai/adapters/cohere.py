"""Cohere adapter (OpenAI-compatibility endpoint)."""
from __future__ import annotations

from .base import CompatibleAdapter


class CohereAdapter(CompatibleAdapter):
    name = "cohere"
    base_url = "https://api.cohere.ai/compatibility/v1"
    models_env = "COHERE_MODELS"
    base_url_env = "COHERE_BASE_URL"
    api_keys_env = "COHERE_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json", "vision", "translation"]
    extra_headers = {}
    # Cohere's compatibility endpoint also accepts x-api-key; provide it when
    # the caller already carries a bearer header it's redundant, so we keep
    # plain Bearer (both dialects are accepted by the endpoint).