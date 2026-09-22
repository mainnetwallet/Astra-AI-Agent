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

    A seam: something that can execute a `ProviderExecutionTarget` without
    the caller (Gateway-side recovery/supervision code) needing to know
    about ProviderRegistry, adapters, or credentials. Implemented by
    `astra.ai.router._RouterExecutionPort` — the concrete bridge that lets
    Gateway-owned result supervision (gateway_supervision.py) send a
    correction back to the real Existing Provider adapter without Gateway
    code ever importing one.
    """

    def execute(self, target: ProviderExecutionTarget, messages: list,
                max_tokens: int = 500, **kwargs) -> str:
        raise NotImplementedError


@dataclass
class ProviderExecutionDecision:
    """The Gateway's structured execution decision for ONE user request.

    This is the machine-readable half of the Gateway -> Provider handoff
    (the human-readable half is the runtime capability block the Provider is
    also given). The Gateway's UNDERSTAND step decides, from the LIVE runtime
    capability catalog, whether this request needs real tool execution, and
    if so which capability category — and states it here instead of leaving
    the Provider to re-guess:

        {"required": true, "capability": "terminal",
         "intent": "clone the requested repository"}

    Rules enforced by `normalized()` (never trust an unverified model claim):

      - `capability` is kept ONLY if the live runtime actually has that
        category. A capability the runtime does not have is dropped, never
        invented — see `astra.ai.capability_context`.
      - If the runtime has no tool capability at all, `required` is forced
        False: the Gateway may not demand execution that cannot happen.

    Backward compatible by design: an older/simpler Gateway reply with no
    `execution` object yields the default (`required=False`), which is
    exactly today's behavior (the Provider decides for itself).
    """
    required: bool = False
    capability: str = ""
    intent: str = ""
    reason: str = ""

    def normalized(self, available_categories=()) -> "ProviderExecutionDecision":
        """Return a copy with `capability`/`required` constrained to what the
        live runtime can actually do. `available_categories` is an iterable
        of live category ids (e.g. `RuntimeCapabilities.categories`)."""
        live = {str(c).strip().lower() for c in (available_categories or ()) if str(c).strip()}
        cap = (self.capability or "").strip().lower()
        required = bool(self.required)
        reason = (self.reason or "").strip()
        if cap and cap not in live:
            # Never claim (or demand) a capability the runtime does not have.
            cap = ""
        if required and not live:
            required = False
            reason = (reason + " " if reason else "") + (
                "No tool capability exists in this runtime, so the Gateway "
                "must not demand tool execution.")
        return ProviderExecutionDecision(required=required, capability=cap,
                                        intent=(self.intent or "").strip(),
                                        reason=reason.strip())

    @classmethod
    def from_dict(cls, data) -> "ProviderExecutionDecision":
        """Parse the Gateway's `execution` object, tolerating any shape.

        A missing/None/non-dict value, or individual fields of the wrong
        type, degrade to the default decision (required=False) rather than
        raising — an old or malformed Gateway reply must never break a turn.
        """
        if not isinstance(data, dict):
            return cls()
        required = data.get("required", False)
        if isinstance(required, str):
            required = required.strip().lower() in ("true", "yes", "1")
        cap = data.get("capability", "")
        if isinstance(cap, (list, tuple)):
            cap = next((str(c) for c in cap if str(c).strip()), "")
        intent = data.get("intent", "")
        reason = data.get("reason", "")
        return cls(required=bool(required), capability=str(cap or ""),
                   intent=str(intent or ""), reason=str(reason or ""))

    def to_dict(self) -> dict:
        return {"required": self.required, "capability": self.capability,
                "intent": self.intent, "reason": self.reason}

    def context_block(self, available_categories=()) -> str:
        """The authoritative handoff text the Provider/AgentToolLoop is given
        for this request. Empty when no execution is required (nothing to
        say), so ordinary chat turns are unchanged."""
        if not self.required:
            return ""
        lines = [
            "Gateway execution decision (authoritative — this request "
            "REQUIRES real tool execution before you answer):",
        ]
        if self.capability:
            lines.append(
                f"- Required capability: {self.capability} (available in "
                "this runtime).")
        else:
            lines.append(
                "- Required capability: use the most appropriate tool "
                "available in this runtime.")
        if self.intent:
            lines.append(f"- What the user asked for: {self.intent}")
        lines.append(
            "- Actually perform the action with the tool(s) available to "
            "you, then report the real result. Do NOT reply with "
            "instructions for the user to do it themselves, and do NOT "
            "promise to do it later — the tool must actually run first.")
        return "\n".join(lines)


@dataclass
class ProviderExecutionResult:
    """Sanitized outcome of one `ProviderExecutionPort.execute()` call (§6).

    Carries only what Gateway-owned result supervision
    (astra.ai.gateway_supervision) needs to validate a response: whether it
    succeeded, the raw text, and an error message on failure. Never an
    adapter object, credential, or provider-native response shape — the
    Existing Provider system's own `RoutingResult` (astra/ai/router.py) is
    reduced to this before it ever reaches Gateway code.
    """
    ok: bool
    text: str = ""
    error: str = ""
