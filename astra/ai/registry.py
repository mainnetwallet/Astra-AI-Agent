"""Provider registry: builds every *configured* provider adapter.

A provider is only instantiated when it has credentials (a pool with at least
one key) **or** explicit user opt-in via `AI_PROVIDER`/`PROVIDERS`. Providers
without credentials are reported as "not configured" — never as healthy.
AstraRouter is deliberately absent here: it is the routing brain, not a
provider, and gets no adapter, no models list, no credentials.

Two provider families are registered here:

* the ten **modern adapters** in `astra/ai/adapters/` (gemini, groq, mistral,
  openrouter, cerebras, cloudflare, sambanova, cohere, zai, bedrock), built
  from `<PROVIDER>_API_KEYS`; and
* the two **backward-compatible** providers in `astra/ai/provider.py` —
  `ClaudeProvider` (name `anthropic`, via `ANTHROPIC_API_KEY`) and
  `OpenAICompatibleProvider` (via `AI_BASE_URL`/`AI_API_KEY`).

All of them are routable peers; the modern adapters are the recommended path.
"""
from __future__ import annotations

from astra.ai.adapters import ADAPTERS

# api-keys env var per provider (the spec's normalized naming).
# The Astra AI Gateway is deliberately absent — it is not a provider (see
# astra/ai/gateway.py); its GW_* config is read only by that module's
# build_astra_ai_gateway(), never by this registry.
KEYS_ENV = {
    "gemini": "GEMINI_API_KEYS", "groq": "GROQ_API_KEYS",
    "mistral": "MISTRAL_API_KEYS", "openrouter": "OPENROUTER_API_KEYS",
    "cerebras": "CEREBRAS_API_KEYS", "cloudflare": "CLOUDFLARE_API_KEYS",
    "sambanova": "SAMBA_API_KEYS", "cohere": "COHERE_API_KEYS",
    "zai": "ZAI_API_KEYS", "bedrock": "BEDROCK_CREDENTIALS",
}


def has_credentials(config, provider: str) -> bool:
    if provider == "bedrock":
        # Bedrock accepts either a bearer-token API key or classic
        # access_key:secret_key IAM pairs — either is sufficient.
        getlist = getattr(config, "getlist", lambda _k, d=[]: d)
        return bool(getlist("BEDROCK_API_KEYS", default=[])) or \
            bool(getlist("BEDROCK_CREDENTIALS", default=[]))
    env = KEYS_ENV.get(provider)
    if not env:
        return False
    vals = getattr(config, "getlist", lambda _k, d=[]: d)(env, default=[])
    return bool(vals)


class ProviderRegistry:
    """Holds instantiated AIProvider adapters, keyed by name."""

    def __init__(self, config=None, events=None, providers: list | None = None):
        self.config = config
        self.events = events
        self._by_name: dict[str, object] = {}
        for p in (providers or []):
            self._by_name[getattr(p, "name", "provider")] = p

    def add(self, provider) -> None:
        self._by_name[getattr(provider, "name", "provider")] = provider

    def get(self, name: str):
        return self._by_name.get(name)

    def all(self) -> list:
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return list(self._by_name)

    def configured(self) -> list:
        """Providers that are actually usable (have credentials)."""
        out = []
        for p in self.all():
            pool = getattr(p, "pool", None)
            if pool is not None:
                if bool(pool):
                    out.append(p)
                continue
            if getattr(p, "health_check", lambda: False)():
                out.append(p)
        return out

    def __iter__(self):
        return iter(self.all())

    def __len__(self):
        return len(self._by_name)


def build_providers(config=None, events=None) -> ProviderRegistry:
    """Create adapters for every provider that has credentials configured.

    Also honours `AI_PROVIDER` as an order preference and as an opt-in list
    (`AI_PROVIDER=gemini,groq` forces those even if legacy keys coexist).
    """
    from astra.core.config import Config
    config = config or Config()
    reg = ProviderRegistry(config=config, events=events)
    # legacy Anthropic + generic OpenAI-compatible still participate when
    # configured (backward compatibility).
    from .provider import ClaudeProvider, OpenAICompatibleProvider
    forced = config.getlist("AI_PROVIDER", default=[])
    for name, cls in ADAPTERS.items():
        if not has_credentials(config, name) and name not in forced:
            continue
        try:
            reg.add(cls(config=config, events=events))
        except Exception:
            continue
    anthropic_key = config.get("ANTHROPIC_API_KEY", "") if config else ""
    if anthropic_key:
        reg.add(ClaudeProvider(config=config, api_key=anthropic_key, events=events))
    if (config and config.get("AI_BASE_URL")) or (config and config.get("AI_API_KEY")):
        reg.add(OpenAICompatibleProvider(config=config, events=events))
    return reg
