"""Provider adapters: one class per real AI backend.

Each adapter implements the common interface (chat/stream/health_check/
list_models/supports) and keeps provider-specific HTTP inside its own file.
The AgentRouter never touches provider HTTP — it scores the adapters here.
"""
from __future__ import annotations

from .base import CompatibleAdapter
from .agentrouter_gateway import AgentRouterGatewayAdapter
from .bedrock import BedrockAdapter, BedrockCredentialPool, sign_v4
from .cloudflare import CloudflareAdapter
from .cerebras import CerebrasAdapter
from .cohere import CohereAdapter
from .gemini import GeminiAdapter
from .groq import GroqAdapter
from .mistral import MistralAdapter
from .openrouter import OpenRouterAdapter
from .sambanova import SambaNovaAdapter
from .zai import ZAIAdapter

__all__ = [
    "CompatibleAdapter", "AgentRouterGatewayAdapter",
    "BedrockAdapter", "BedrockCredentialPool", "sign_v4",
    "CloudflareAdapter", "CerebrasAdapter", "CohereAdapter", "GeminiAdapter",
    "GroqAdapter", "MistralAdapter", "OpenRouterAdapter", "SambaNovaAdapter",
    "ZAIAdapter",
]

# provider name -> (adapter class, models env, api-keys env, base env)
ADAPTERS: dict[str, type] = {
    "agentrouter_gateway": AgentRouterGatewayAdapter,
    "gemini": GeminiAdapter,
    "groq": GroqAdapter,
    "mistral": MistralAdapter,
    "openrouter": OpenRouterAdapter,
    "cerebras": CerebrasAdapter,
    "cloudflare": CloudflareAdapter,
    "sambanova": SambaNovaAdapter,
    "cohere": CohereAdapter,
    "zai": ZAIAdapter,
    "bedrock": BedrockAdapter,
}