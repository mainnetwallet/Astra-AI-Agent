"""SambaNova adapter."""
from __future__ import annotations

from .base import CompatibleAdapter


class SambaNovaAdapter(CompatibleAdapter):
    name = "sambanova"
    base_url = "https://api.sambanova.ai/v1"
    models_env = "SAMBA_MODELS"
    api_keys_env = "SAMBA_API_KEYS"
    capabilities = ["chat", "stream", "tools", "json"]