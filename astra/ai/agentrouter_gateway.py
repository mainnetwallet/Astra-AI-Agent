"""AgentRouter.org gateway client — a core/service module, NOT a provider.

NOT to be confused with Astra's own internal `AgentRouter` (astra/ai/router.py),
which is the routing brain that scores across every adapter in
astra/ai/adapters/ — it has no base URL, no API key and no model list of its
own. This file is the client for the *external* third-party service at
agentrouter.org: a non-profit, OpenAI-compatible gateway that aggregates
30+ upstream providers (Claude, GPT, Gemini, DeepSeek, GLM, ...) behind one
API key. https://docs.agentrouter.org

By architecture decision, this client lives outside `astra/ai/adapters/` and
is never added to `ADAPTERS`/`ProviderRegistry`: it is not one more entry in
the provider list next to Gemini/Groq/etc. Instead it is wired directly into
the internal AgentRouter core (astra/ai/router.py) as its own configured
service, reachable only through `AGENTROUTER_API_KEYS`/`AGENTROUTER_MODELS`/
`AGENTROUTER_BASE_URL`, and reported separately in health/dashboard output as
"AgentRouter Core", never inside the provider table.

The HTTP shape reused here (`{base_url}/chat/completions`, plain
`Authorization: Bearer <key>`) is identical to CompatibleAdapter's, so this
class subclasses it purely for the request/response/retry/credential-pool
plumbing — that inheritance is an implementation detail, not a statement
that this is a registered provider.

agentrouter.org separately documents an "Anthropic-compatible" route meant
for plugging directly into the official Claude Code CLI
(`ANTHROPIC_BASE_URL=https://agentrouter.org/`), which from public reports
appears to validate that the calling client identifies itself as Claude
Code. Astra deliberately does not use that route or send any User-Agent
that claims to be Claude Code (or any other client this isn't) — Astra is
a different application and identifies itself honestly. If agentrouter.org
requires a specific client identifier for a given route, that's a reason
to use a different route or provider, not to misrepresent what's calling.
"""
from __future__ import annotations

from astra.ai.adapters.base import CompatibleAdapter


class AgentRouterGatewayClient(CompatibleAdapter):
    """Client for the agentrouter.org third-party gateway.

    Deliberately NOT named/registered as a provider adapter (see module
    docstring). `name` is kept distinct from both "agentrouter" (which would
    collide with the internal router's identity) and the provider namespace.
    """
    name = "agentrouter_gateway"
    base_url = "https://agentrouter.org/v1"
    models_env = "AGENTROUTER_MODELS"
    api_keys_env = "AGENTROUTER_API_KEYS"
    base_url_env = "AGENTROUTER_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json"]


def build_agentrouter_gateway(config=None):
    """Build the AgentRouter.org gateway client iff it has credentials.

    Returns None when `AGENTROUTER_API_KEYS` is unset/blank, so the router
    core simply has no gateway to fall back to — same "unconfigured means
    absent" convention as every provider adapter, but this object never goes
    into ProviderRegistry.
    """
    from astra.core.config import Config
    config = config or Config()
    keys = config.getlist("AGENTROUTER_API_KEYS", default=[])
    if not keys:
        return None
    try:
        return AgentRouterGatewayClient(config=config)
    except Exception:
        return None
