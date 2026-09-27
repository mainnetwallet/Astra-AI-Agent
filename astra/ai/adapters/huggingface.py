"""Hugging Face adapter (Inference Providers router, OpenAI-compatible)."""
from __future__ import annotations

from .base import CompatibleAdapter


class HuggingFaceAdapter(CompatibleAdapter):
    """Hugging Face Inference Providers, via the unified OpenAI-compatible
    router at ``https://router.huggingface.co/v1``. The router forwards each
    call to whichever partner provider (fal, Together, Novita, ...) actually
    serves the requested model id.
    """
    name = "huggingface"
    base_url = "https://router.huggingface.co/v1"
    models_env = "HF_MODELS"
    base_url_env = "HF_BASE_URL"
    api_keys_env = "HF_API_KEYS"
    image_models_env = "HF_IMAGE_MODELS"
    capabilities = ["chat", "stream", "tools", "json", "vision"]
