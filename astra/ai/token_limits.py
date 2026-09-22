"""Provider-aware output-token resolution.

Astra must never impose an arbitrary small completion cap on a model. The
provider API is the authority on how many tokens a model may emit, so this
module centralises ONE decision every Gateway/Provider request goes through:

* **Optional output limits are omitted.** OpenAI-compatible
  ``chat/completions`` and Bedrock's Converse API treat the output token
  field as optional — leaving it out lets the model use its own maximum.
  When no explicit budget is configured we therefore pass ``None`` and the
  adapter omits the field entirely, instead of inventing a number.
* **Required output limits are derived.** Anthropic's Messages API
  *requires* ``max_tokens``. When it is required and nothing was configured,
  we derive the value from the selected model's own capability metadata
  (``Model.max_output_tokens``), then the model's context window, then a
  documented per-provider ceiling — never a universal small number.
* **An explicit budget always wins.** ``CHAT_MAX_TOKENS`` / a caller-passed
  value is a real operator override, not a ceiling that silently overrides
  the model. It is honoured verbatim.

This module contains no model names, no endpoint URLs and no credentials; it
is pure decision logic, unit-testable without network access.
"""
from __future__ import annotations

# Providers whose chat API REQUIRES an output-token field. Everything else we
# integrate (OpenAI-compatible HTTP, Bedrock Converse, Gemini, Groq,
# Cloudflare, ...) treats the field as optional, so omitting it is the honest
# "let the model decide" behaviour.
_REQUIRES_MAX_TOKENS = frozenset({"anthropic", "claude"})

# Documented output ceiling used ONLY when the provider requires the field and
# no model-specific capability is known. This is a provider fact (the
# Messages API has no server-side default), not an Astra-imposed cap.
_REQUIRED_PROVIDER_CEILING = {
    "anthropic": 8192,
    "claude": 8192,
}


def _norm(provider) -> str:
    return str(provider or "").strip().lower().replace("_", "-")


def requires_max_tokens(provider) -> bool:
    """True when `provider`'s API rejects a request without an output limit."""
    return _norm(provider) in _REQUIRES_MAX_TOKENS


def _model_output_cap(model_meta) -> int:
    """Best-effort output ceiling from live model metadata (a `Model`, a
    dict with ``max_output_tokens``, or an object attribute). Returns 0 when
    nothing usable is known."""
    if model_meta is None:
        return 0
    value = None
    if isinstance(model_meta, dict):
        value = model_meta.get("max_output_tokens")
    else:
        value = getattr(model_meta, "max_output_tokens", None)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _context_window(model_meta) -> int:
    if model_meta is None:
        return 0
    value = (model_meta.get("context_window") if isinstance(model_meta, dict)
             else getattr(model_meta, "context_window", None))
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def resolve_output_tokens(requested=None, *, provider="", model_meta=None):
    """Return the output-token value to send for one request, or ``None`` to
    omit the field (model/provider default behaviour).

    ``requested`` is an explicit operator/caller budget and, when positive,
    is always returned unchanged. Otherwise the decision is provider-aware:
    optional for providers that support omission, derived from model
    capability for providers that require the field.
    """
    if requested is not None:
        try:
            n = int(requested)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            return n
    if not requires_max_tokens(provider):
        return None
    cap = _model_output_cap(model_meta)
    if cap:
        return cap
    ctx = _context_window(model_meta)
    if ctx:
        # A model's total context includes its output; reserving a bounded
        # slice of the model's OWN window is provider-aware and scales with
        # the model, unlike a fixed universal number.
        return max(1, ctx // 4)
    return _REQUIRED_PROVIDER_CEILING.get(_norm(provider), 4096)
