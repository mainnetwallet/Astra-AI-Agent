"""Provider-aware conversation/context fitting.

Astra previously trimmed conversation history to a fixed 6000 characters /
20 turns and capped prompts at 12000 characters — small universal numbers
that silently discarded context even when the selected model could hold all
of it. This module replaces those caps with ONE provider-aware rule:

    keep the full useful conversation when it fits the selected model's
    actual context window; only when it genuinely does not fit, drop the
    lowest-value material first.

There is no tokenizer dependency in this project, so the size of a prompt is
reported as an **estimate** (``CHARS_PER_TOKEN_ESTIMATE`` characters per
token), never as an exact token count. The context window it is compared
against comes from real model metadata (``astra.ai.models.Model
.context_window``), so the budget moves with whichever model actually serves
the request.

Reduction priority (highest value first — never dropped):

  1. Core system instructions
  2. Gateway execution decision
  3. Current user request
  4. Active tool protocol / schema
  5. Active terminal / execution state
  6. Required recent tool results
  7. Recent conversation
  8. Older low-value history

Because (1), (2), (4) and (5) live in the system prompt and the tool-loop
context blocks — and (3) is always the final message — the deterministic
``fit_messages`` below only ever drops items 7/8: the middle of the message
list (older conversation turns and older tool exchanges), oldest first. The
leading system message(s) and the final user request are structurally
protected and can never be removed.
"""
from __future__ import annotations

# Conservative estimate only. This project has no tokenizer/model-specific
# token counting, so sizes are always presented as estimates — never as exact
# token counts.
CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens_from_chars(chars: int) -> int:
    """Estimated tokens for a character count (documented as an estimate)."""
    try:
        n = int(chars)
    except (TypeError, ValueError):
        n = 0
    return max(0, n) // CHARS_PER_TOKEN_ESTIMATE


def _content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return len(str(content or ""))


def estimate_message_tokens(messages) -> int:
    """Estimated tokens for a whole message list (documented estimate)."""
    return estimate_tokens_from_chars(
        sum(_content_chars(m.get("content")) for m in messages or []
            if isinstance(m, dict)))


def default_reserve_tokens(context_window: int) -> int:
    """Tokens to leave free for the model's own output.

    Derived from the model's window (a bounded slice), never a fixed number,
    so it scales with whatever model is selected. Used only when the caller
    has no concrete resolved output limit to reserve.
    """
    try:
        window = int(context_window)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return 0
    return max(256, window // 8)


def fit_messages(messages, *, context_window: int = 0,
                 reserve_tokens: int = 0):
    """Return `messages` unchanged when it fits `context_window` (minus the
    output reserve); otherwise drop the oldest middle turns until it does.

    Only middle messages are eligible for removal. The leading system
    message(s) — Core prompt, Gateway execution decision, tool protocol,
    live terminal/execution state — and the final user request are never
    dropped, so an active execution can never lose its instructions or its
    current turn.
    """
    if not messages:
        return list(messages or [])
    try:
        window = int(context_window)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return list(messages)
    try:
        reserve = max(0, int(reserve_tokens))
    except (TypeError, ValueError):
        reserve = 0
    budget = max(1, window - reserve)
    if estimate_message_tokens(messages) <= budget:
        return list(messages)

    protected = set()
    i = 0
    while (i < len(messages) and isinstance(messages[i], dict)
           and messages[i].get("role") == "system"):
        protected.add(i)
        i += 1
    protected.add(len(messages) - 1)  # current request / latest turn

    droppable = [j for j in range(len(messages)) if j not in protected]
    dropped = set()
    while droppable:
        remaining = [m for k, m in enumerate(messages) if k not in dropped]
        if estimate_message_tokens(remaining) <= budget:
            break
        dropped.add(droppable.pop(0))  # oldest middle turn first
    return [m for k, m in enumerate(messages) if k not in dropped]
