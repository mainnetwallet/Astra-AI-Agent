"""AgentRouter.org gateway adapter.

NOT to be confused with Astra's own internal `AgentRouter` (astra/ai/router.py),
which is the routing brain that scores across every adapter in this package —
it has no base URL, no API key and no model list of its own (see
.env.example). This file is the *external* third-party service at
agentrouter.org: a non-profit, OpenAI-compatible gateway that aggregates
30+ upstream providers (Claude, GPT, Gemini, DeepSeek, GLM, ...) behind one
API key. https://docs.agentrouter.org

Integration here uses agentrouter.org's documented OpenAI-compatible route
(`{base_url}/chat/completions`, plain `Authorization: Bearer <key>`) — the
same shape as every other CompatibleAdapter in this package, so no adapter
code was needed beyond this class.

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

from .base import CompatibleAdapter


class AgentRouterGatewayAdapter(CompatibleAdapter):
    name = "agentrouter_gateway"
    base_url = "https://agentrouter.org/v1"
    models_env = "AGENTROUTER_MODELS"
    api_keys_env = "AGENTROUTER_API_KEYS"
    base_url_env = "AGENTROUTER_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json"]
