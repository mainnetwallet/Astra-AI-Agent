"""Provider ↔ Gateway execution-handoff contract (§3, §7).

This module is the ONLY thing the Astra AI Gateway (astra/ai/gateway.py,
astra/ai/gateway_recovery.py) and the existing Provider system
(astra/ai/router.py) share for task-level recovery coordination. It is
deliberately tiny and dependency-free:

  - `ProviderExecutionTarget` — sanitized (provider_id, model_id,
    capabilities, metadata) describing ONE candidate the existing Provider
    system could execute against. It NEVER carries API keys, secrets,
    credential objects, ProviderRegistry/adapter instances or HTTP clients
    — only plain strings/lists/dicts safe to hand to the Gateway.

  - `ProviderExecutionPort` — a tiny dependency-inverted interface a caller
    *may* implement so Gateway-driven recovery code can ask "please execute
    this target" without importing anything from astra.ai.router or
    astra.ai.adapters. Nothing in this repo currently requires an object
    that implements this port (AstraRouter keeps owning its own execution
    loop, per §2/§15) — it exists so a future integration has a clean seam
    instead of Gateway reaching into ProviderRegistry directly.

  - `classify_execution_failure` — the *single* source of truth for turning
    an exception/message into one of the recovery categories from §7. It is
    a thin remap on top of `astra.core.classification.code_for` (the
    project's existing canonical error classifier) — never a second,
    competing classification brain. Two callers (the Executor's retry
    policy and Gateway-driven recovery) can therefore never disagree about
    what kind of failure just happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from astra.core.classification import code_for

# ── §7 failure taxonomy ──────────────────────────────────────────────────────
RATE_LIMIT = "RATE_LIMIT"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
TIMEOUT = "TIMEOUT"
NETWORK = "NETWORK"
SERVER_ERROR = "SERVER_ERROR"
AUTH_FAILURE = "AUTH_FAILURE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
INVALID_REQUEST = "INVALID_REQUEST"
CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
TOOL_UNSUPPORTED = "TOOL_UNSUPPORTED"
UNKNOWN = "UNKNOWN"

EXECUTION_FAILURE_CATEGORIES = frozenset({
    RATE_LIMIT, QUOTA_EXCEEDED, TIMEOUT, NETWORK, SERVER_ERROR, AUTH_FAILURE,
    MODEL_UNAVAILABLE, INVALID_REQUEST, CONTEXT_TOO_LARGE, TOOL_UNSUPPORTED,
    UNKNOWN,
})

# canonical astra.core.classification code -> §7 category (default mapping;
# a few canonical codes are ambiguous and get refined by text-sniffing below)
_CANONICAL_TO_CATEGORY = {
    "rate_limit": RATE_LIMIT,
    "timeout": TIMEOUT,
    "network": NETWORK,
    "provider": SERVER_ERROR,
    "model_unavailable": MODEL_UNAVAILABLE,
    "authentication": AUTH_FAILURE,
    "authorization": AUTH_FAILURE,
    "validation": INVALID_REQUEST,
    "schema_failure": INVALID_REQUEST,
    "tool": TOOL_UNSUPPORTED,
}

# Non-retryable §7 categories: never blindly backoff-retried (mirrors
# classification.NON_RETRYABLE's intent for the recovery-target vocabulary).
NON_RETRYABLE_CATEGORIES = frozenset({AUTH_FAILURE, INVALID_REQUEST})

# Categories where the *current* target should cool down and a different
# target should be tried, rather than the same one retried in place.
COOLDOWN_CATEGORIES = frozenset({
    RATE_LIMIT, QUOTA_EXCEEDED, MODEL_UNAVAILABLE, SERVER_ERROR, AUTH_FAILURE,
})


def classify_execution_failure(exc: Exception | None = None, *,
                                message: str = "") -> str:
    """Map an exception/message to one §7 recovery category.

    Text-based signals are checked FIRST, before delegating to
    `astra.core.classification.code_for` — because `code_for` prioritizes an
    exception's `.category` (e.g. every HTTP-level failure in this codebase
    raises a generic `ProviderError`, whose `.category` is always
    `"ProviderError"` → canonical code `"provider"`) over the message text
    that actually says *why* it failed ("rate limit reached", "quota
    exceeded", ...). Relying on `code_for`'s exception-first priority here
    would collapse every one of those into a single bucket and defeat the
    whole point of this recovery-specific taxonomy. This is still a single
    source of truth for *fingerprinting* (it reuses `code_for`'s own
    text classifier, `astra.core.classification._from_text`, for anything
    not covered by the extra signals below) — never a second, independently
    maintained set of substring rules.
    """
    text = (message or (str(exc) if exc is not None else "")).lower()

    if "context" in text and ("too large" in text or "exceed" in text or
                              "context_length" in text or "too long" in text):
        return CONTEXT_TOO_LARGE
    if "quota" in text:
        return QUOTA_EXCEEDED
    if "rate limit" in text or "rate_limit" in text or "rate limited" in text \
            or "429" in text or "throttl" in text:
        return RATE_LIMIT
    if "tool" in text and "unsupported" in text:
        return TOOL_UNSUPPORTED

    if text:
        code = code_for(message=text)
    elif exc is not None:
        code = code_for(exc=exc)
    else:
        code = "internal"
    return _CANONICAL_TO_CATEGORY.get(code, UNKNOWN)


@dataclass(frozen=True)
class ProviderExecutionTarget:
    """Sanitized candidate the existing Provider system could execute.

    Only ever carries plain identifiers/metadata — no secrets, clients, or
    ProviderRegistry/adapter objects. Hashable + comparable by
    (provider_id, model_id) so it can be used as a dict/set key when
    tracking "already tried this run".
    """
    provider_id: str
    model_id: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict = field(default_factory=dict, compare=False, hash=False)

    def key(self) -> tuple[str, str]:
        return (self.provider_id, self.model_id)

    def to_dict(self) -> dict:
        return {"provider_id": self.provider_id, "model_id": self.model_id,
                "capabilities": list(self.capabilities),
                "metadata": dict(self.metadata)}


class ProviderExecutionPort:
    """Dependency-inverted execution interface (§3).

    An optional seam: something that can execute a `ProviderExecutionTarget`
    without the caller (Gateway-side recovery code) needing to know about
    ProviderRegistry, adapters, or credentials. Not currently required by
    anything in this repo — AstraRouter keeps owning execution — but kept
    here so Gateway-driven recovery never has to reach into the Provider
    system's internals to get work done.
    """

    def execute(self, target: ProviderExecutionTarget, messages: list,
                max_tokens: int = 500, **kwargs) -> str:
        raise NotImplementedError
