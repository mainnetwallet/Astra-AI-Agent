"""Z.AI adapter (OpenAI-compatible v4 endpoint)."""
from __future__ import annotations

from .base import CompatibleAdapter


class ZAIAdapter(CompatibleAdapter):
    name = "zai"
    base_url = "https://api.z.ai/api/paas/v4"
    models_env = "ZAI_MODELS"
    base_url_env = "ZAI_BASE_URL"
    api_keys_env = "ZAI_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json", "vision"]