"""Astra AI Gateway — a separate multi-service AI gateway with automatic fallback.

Replaces the old third-party AgentRouter.org gateway with a set of four
independent AI connections:

    Astra AI Gateway
    ├── Gemini      ← GW_GEMINI_* config
    ├── Groq        ← GW_GROQ_* config
    ├── Cloudflare  ← GW_CLOUDFLARE_* config
    └── Bedrock     ← GW_BEDROCK_* config

Each connection has completely independent credentials, models and
endpoints/base URLs — separate from the existing Provider system:
GW_GEMINI_API_KEYS ≠ GEMINI_API_KEYS, etc. The Gateway is NOT a provider:
it is never added to ProviderRegistry and is reported separately in
health/dashboard output, never inside the provider table.

Architecture note: this is a *gateway*, not a provider-adapters shim. Each
Gateway connection is its own adapter class with its own configuration env
vars. The existing provider adapter classes below (GeminiAdapter, GroqAdapter,
CloudflareAdapter, BedrockAdapter) are subclassed purely to reuse their
request/response/auth plumbing — an implementation detail, not a statement
that the Gateway redistributes the providers' configuration. All environment
variables the Gateway reads are GW_-prefixed and independent.

Fallback: if Gemini fails → Groq → Cloudflare → Bedrock. The same task,
messages, context and system instructions carry over unchanged between
services (the full request is re-sent with the next connection until one
succeeds) — no restart, no task duplication.
"""
from __future__ import annotations

from astra.ai.adapters.bedrock import BedrockAdapter
from astra.ai.adapters.cloudflare import CloudflareAdapter
from astra.ai.adapters.gemini import GeminiAdapter
from astra.ai.adapters.groq import GroqAdapter
from astra.core.exceptions import ProviderError, TimeoutError


# ── Gateway connections ─────────────────────────────────────────────────────
# Each service has its own credentials / model list / endpoint. These classes
# only override env-var names so the Gateway never reads the Provider config.

class AstraGatewayGemini(GeminiAdapter):
    """Astra AI Gateway / Gemini connection."""
    name = "astra-gw-gemini"
    models_env = "GW_GEMINI_MODELS"
    api_keys_env = "GW_GEMINI_API_KEYS"
    base_url_env = "GW_GEMINI_BASE_URL"


class AstraGatewayGroq(GroqAdapter):
    """Astra AI Gateway / Groq connection."""
    name = "astra-gw-groq"
    models_env = "GW_GROQ_MODELS"
    api_keys_env = "GW_GROQ_API_KEYS"
    base_url_env = "GW_GROQ_BASE_URL"


class AstraGatewayCloudflare(CloudflareAdapter):
    """Astra AI Gateway / Cloudflare connection.

    Reuses CloudflareAdapter's per-account round-robin; account ids are read
    from the independent GW_CLOUDFLARE_ACCOUNT_IDS list.
    """
    name = "astra-gw-cloudflare"
    models_env = "GW_CLOUDFLARE_MODELS"
    api_keys_env = "GW_CLOUDFLARE_API_KEYS"
    base_url_env = "GW_CLOUDFLARE_BASE_URL"
    account_ids_env = "GW_CLOUDFLARE_ACCOUNT_IDS"


class AstraGatewayBedrock(BedrockAdapter):
    """Astra AI Gateway / Bedrock connection.

    Reuses BedrockAdapter's two auth modes (bearer-token API key, or classic
    SigV4 IAM pairs) with the gateway's own env vars and region override.
    """
    name = "astra-gw-bedrock"
    models_env = "GW_BEDROCK_MODELS"
    base_url_env = "GW_BEDROCK_BASE_URL"
    api_keys_env = "GW_BEDROCK_API_KEYS"
    credentials_env = "GW_BEDROCK_CREDENTIALS"
    region_env = "GW_BEDROCK_REGION"
    default_region = "us-east-1"

    def __init__(self, config=None, events=None, pool=None):
        # independent gateway region (defaults to AWS_REGION when unset)
        _region = None
        if config is not None:
            try:
                _region = config.get(getattr(self, "region_env", "AWS_REGION"), None) \
                    or config.get("AWS_REGION", None)
            except Exception:
                _region = None
        if _region:
            self.default_region = _region
        super().__init__(config, events, pool)


# Canonical fallback order: Gemini → Groq → Cloudflare → Bedrock
GATEWAY_CONNECTIONS = (
    AstraGatewayGemini,
    AstraGatewayGroq,
    AstraGatewayCloudflare,
    AstraGatewayBedrock,
)


def _build_connection(cls, config=None):
    """Build one gateway connection iff its own credentials are configured.

    Returns None when the connection's GW_*_API_KEYS / GW_*_CREDENTIALS are
    unset/blank, so an unconfigured connection simply drops out of the
    fallback chain — same convention as every provider adapter.
    """
    if config is None:
        return None
    # gateway connections are independent of provider config; only GW_* counts
    keys = config.getlist(cls.api_keys_env, default=[])
    creds = getattr(cls, "credentials_env", None)
    if not keys and creds:
        keys = config.getlist(creds, default=[])
    if not keys:
        return None
    try:
        return cls(config=config)
    except Exception:
        return None


class AstraAIGateway:
    """Automatic-fallback gateway over the four connections.

    A request enters the gateway once and is attempted on Gemini, then Groq,
    then Cloudflare, then Bedrock — the first successful reply wins; the same
    messages/context continue with every subsequent attempt (no restart).
    """

    def __init__(self, connections: list | None = None, config=None):
        self.name = "astra_ai_gateway"
        self.config = config
        self.connections = []
        self.last_connection = ""     # name of the most recent successful call
        self.last_model = ""
        self.last_attempts = 0
        if connections is not None:
            self.connections = list(connections)
        elif config is not None:
            for cls in GATEWAY_CONNECTIONS:
                conn = _build_connection(cls, config)
                if conn is not None:
                    self.connections.append(conn)

    # -- capability -----------------------------------------------------------
    @property
    def models(self) -> list[str]:
        out = []
        for c in self.connections:
            out.extend(c.models or [])
        return out

    def is_usable(self) -> bool:
        """At least one connection has healthy credentials."""
        for c in self.connections:
            pool = getattr(c, "pool", None)
            if pool is not None and bool(pool):
                return True
            try:
                if c.health_check():
                    return True
            except Exception:
                continue
        return bool(self.connections)

    # -- the four-provider fallback ------------------------------------------
    def _try_connections(self, fn, *, model, messages, max_tokens):
        """Run `fn` over the connections in order; first success wins.

        Tracks which connection/model served the request (`last_connection`,
        `last_model`) and how many connections were attempted before success
        (`last_attempts`) so callers can report the real serving path.
        """
        last_error = ""
        attempts = 0
        for conn in self.connections:
            attempts += 1
            self.last_attempts = attempts
            try:
                call_model = model
                if call_model and (conn.models or []) and \
                        call_model not in conn.models:
                    # this connection doesn't serve the requested model —
                    # skip it rather than fail it (fallback intent is a
                    # different model on the next service).
                    continue
                result = fn(conn, call_model)
                self.last_connection = conn.name
                self.last_model = call_model or (conn.models[0] if conn.models else "")
                return result
            except (ProviderError, TimeoutError) as e:
                last_error = getattr(e, "message", None) or str(e)
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue
        self.last_connection = ""
        self.last_model = ""
        raise ProviderError(
            f"Astra AI Gateway: all four services failed —— {last_error}")

    def chat(self, messages, model=None, max_tokens=500) -> str:
        return self._try_connections(
            lambda c, m: c.chat(messages, model=m, max_tokens=max_tokens),
            model=model, messages=messages, max_tokens=max_tokens)

    def stream(self, messages, model=None, max_tokens=500):
        # Generator fallback: first connection's stream that starts wins.
        for conn in self.connections:
            try:
                call_model = model
                if call_model and (conn.models or []) and \
                        call_model not in conn.models:
                    continue
                for chunk in conn.stream(messages, model=call_model,
                                         max_tokens=max_tokens):
                    yield chunk
                return
            except (ProviderError, TimeoutError):
                continue
            except Exception:
                continue

    # -- health (reported separately, never as a provider) --------------------
    def health(self) -> dict:
        out = {}
        for c in self.connections:
            pool = getattr(c, "pool", None)
            if pool is not None:
                state = ("healthy" if bool(pool)
                         else ("not_configured" if pool.count == 0 else "degraded"))
            else:
                state = "not_configured"
            out[c.name] = {
                "state": state,
                "models": list(c.models or []),
                "base_url": getattr(c, "base_url", ""),
            }
        return out


def build_astra_ai_gateway(config=None) -> AstraAIGateway | None:
    """Build the Astra AI Gateway iff at least one connection is configured.

    Returns None when no GW_* connection credentials are set, mirroring the
    old "unconfigured means absent" convention. The returned object is the
    router's `gateway`; it is never added to ProviderRegistry.
    """
    from astra.core.config import Config
    config = config or Config()
    gw = AstraAIGateway(config=config)
    if not gw.connections:
        return None
    return gw