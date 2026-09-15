"""Cerebras adapter."""
from __future__ import annotations

from .base import CompatibleAdapter


class CerebrasAdapter(CompatibleAdapter):
    name = "cerebras"
    base_url = "https://api.cerebras.ai/v1"
    models_env = "CEREBRAS_MODELS"
    api_keys_env = "CEREBRAS_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json"]