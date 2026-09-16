"""Error classification + retry policy (Phase E).

One canonical classification for any failure the orchestrator/executor sees,
so recovery is chosen by *kind*, never by scraping message text:

    validation, authentication, authorization, rate_limit, timeout, network,
    provider, model_unavailable, tool, browser, web3, database, internal,
    user_cancelled,
    transaction_policy, transaction_rejected, transaction_failed

The retry policy is deliberately conservative:
 - never retry: validation, authentication, authorization, user_cancelled,
   and the three transaction verdicts — a signed/submitted tx is resolved
   against the chain, never blindly re-submitted.
 - retry with exponential backoff + full jitter (capped): rate_limit,
   timeout, network, provider, model_unavailable, tool, browser, web3,
   database, internal.
"""
from __future__ import annotations

import random
import time

# Canonical error codes (used by /api structured errors too).
CODES = frozenset({
    "validation", "authentication", "authorization", "rate_limit",
    "timeout", "network", "provider", "model_unavailable", "tool",
    "browser", "web3", "database", "internal", "user_cancelled",
    "transaction_policy", "transaction_rejected", "transaction_failed",
})

# Codes that must never be retried.
NON_RETRYABLE = frozenset({
    "validation", "authentication", "authorization", "user_cancelled",
    "transaction_policy", "transaction_rejected", "transaction_failed",
})

# AstraError.category (astra.core.exceptions) -> canonical code.
_CATEGORY = {
    "NetworkError": "network",
    "TimeoutError": "timeout",
    "ProviderError": "provider",
    "BrowserError": "browser",
    "ValidationError": "validation",
    "PermissionError": "authorization",
    "TransactionError": "web3",
    "PluginError": "tool",
    "ToolError": "tool",
    "WorkflowError": "database",
}


def code_for(exc=None, category: str = "", message: str = "") -> str:
    """Map an exception (or category/message) to one canonical error code.

    Priority: explicit canonical `.code` on the exception (e.g. the web3
    policy exceptions) → AstraError.category → message fingerprint.
    """
    if exc is not None:
        own = getattr(exc, "code", "")
        if isinstance(own, str) and own in CODES:
            return own
        cat = getattr(exc, "category", "") or type(exc).__name__
        if cat in _CATEGORY:
            return _CATEGORY[cat]
        message = message or str(exc)
    if category in _CATEGORY:
        return _CATEGORY[category]
    return _from_text(message.strip())


def _from_text(text: str) -> str:
    t = text.lower()
    if not t:
        return "internal"
    for token, code in (
        ("transaction_failed", "transaction_failed"),
        ("transaction_rejected", "transaction_rejected"),
        ("transaction_policy", "transaction_policy"),
        ("rate_limit", "rate_limit"), ("rate limited", "rate_limit"),
        ("quota", "rate_limit"), ("throttl", "rate_limit"), ("429", "rate_limit"),
        ("timed out", "timeout"), ("timeout", "timeout"),
        ("network_unavailable", "network"), ("connection", "network"),
        ("connect", "network"), ("refused", "network"), ("unreachable", "network"),
        ("model_unavailable", "model_unavailable"),
        ("authentication", "authentication"), ("unauthorized", "authentication"),
        ("authorization", "authorization"), ("denied", "authorization"),
        ("permission", "authorization"),
        ("not found", "tool"), ("unknown tool", "tool"), ("tool", "tool"),
        ("browser", "browser"), ("playwright", "browser"),
        ("database", "database"), ("sqlite", "database"), ("disk i/o", "database"),
        ("user_cancelled", "user_cancelled"), ("cancelled", "user_cancelled"),
        ("cancel", "user_cancelled"),
        ("transaction", "web3"), ("web3", "web3"), ("rpc", "web3"),
    ):
        if token in t:
            return code
    return "internal"


class RetryPolicy:
    """Deterministic retry policy: exp backoff + full jitter, capped.

    `max_attempts(code)` returns 0 for codes that must never be retried (so
    unused callers naturally don't retry), and `can_retry` is the plain
    predicate. `backoff_ms(attempt)` returns the sleep *before* the given
    0-based attempt: base * 2^attempt, jittered about [base, cap].
    """

    NEGATIVE = frozenset()

    def __init__(self, base_delay_s: float = 0.5, max_delay_s: float = 8.0,
                 jitter: float = 1.0, max_attempts: int = 2):
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter
        self.max_attempts = max(0, int(max_attempts))

    def can_retry(self, code: str) -> bool:
        return code not in NON_RETRYABLE

    def attempts(self, code: str) -> int:
        return 0 if not self.can_retry(code) else self.max_attempts

    def backoff_ms(self, attempt: int) -> float:
        """Sleep in ms before attempt `attempt`, exponential + full jitter.
        Always deterministic upper bound; never exceeds max_delay_s."""
        exp = self.base_delay_s * (2 ** max(0, attempt))
        cap = min(exp, self.max_delay_s)
        if self.jitter <= 0:
            return cap * 1000.0
        return random.uniform(cap * 0.5, cap) * 1000.0 if self.jitter >= 1 else \
            (cap + random.uniform(0, cap * self.jitter)) * 1000.0

    def sleep_before(self, attempt: int) -> float:
        ms = self.backoff_ms(attempt)
        time.sleep(ms / 1000.0)
        return ms