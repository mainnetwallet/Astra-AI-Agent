"""ConversationContextBuilder — the single source of prior-turn history for
one chat turn.

Bug this fixes
--------------
`ChatPipeline` always accepted a `context` string and faithfully forwarded it
to both the Gateway (`_understand`) and the Provider call, so the *plumbing*
for multi-turn chat existed. What was missing was anyone actually filling
that string in from the persisted transcript: `/api/chat` read `context`
straight off the incoming request body, and the browser never sent one (see
`static/js/astra.js`) — so every turn ran with empty history, "why?" and
"continue" had nothing to resolve against, and the Gateway/Provider only
ever agreed on an empty context.

This module is the fix: given a `ChatLog` and a `conversation_id`, it loads
that conversation's own messages (never another conversation's — isolation
is structural, everything is filtered by `conversation_id`), turns them into
a canonical, provider-independent list of `{"role", "content"}` turns, and
applies one deterministic trim so the result fits a safe size budget while
always keeping the newest turns. `astra/web.py` builds this once per turn
and hands the *same* `ConversationContext` to both the Gateway call and the
Provider call (`ChatPipeline.run(..., history=...)`), which is what
"the same context reaches both" actually requires.

`ChatLog` (server-side persistence) is untouched; this module only reads it.
`MemorySystem` (long-term / cross-conversation memory) is a different
subsystem and is never consulted here — thread history and long-term memory
stay separate, on purpose (see astra/memory/memory.py).
"""
from __future__ import annotations

import dataclasses

# No Astra-imposed limit by default: the full useful conversation is carried,
# and provider-aware fitting against the selected model's REAL context window
# happens where the model is known (astra.ai.context_budget, applied in the
# router/gateway). `None` means "no limit"; callers that genuinely need a
# smaller budget can still pass a number.
DEFAULT_MAX_CHARS = None
DEFAULT_MAX_TURNS = None


@dataclasses.dataclass
class ConversationContext:
    """Canonical, provider-independent prior-turn history for one chat turn.

    `messages` is chronological (oldest first), already trimmed, and never
    contains the current (not-yet-answered) user message — see
    `ConversationContextBuilder.build`'s `exclude_message_id`.
    """
    messages: list[dict]
    conversation_id: int | None = None

    def __bool__(self) -> bool:
        return bool(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def as_text(self) -> str:
        """Flattened "User: ...\\nAssistant: ..." block, for prompts that
        only take a single text blob (the Gateway's UNDERSTAND prompt)."""
        lines = []
        for m in self.messages:
            speaker = "User" if m["role"] == "user" else "Assistant"
            lines.append(f"{speaker}: {m['content']}")
        return "\n".join(lines)

    def as_provider_messages(self) -> list[dict]:
        """Plain role/content turns, ready to prepend to a Provider
        `messages[]` list, before the current user turn."""
        return [{"role": m["role"], "content": m["content"]}
                for m in self.messages]


class ConversationContextBuilder:
    """Loads + trims one conversation's history from `ChatLog`.

    This is the ONE place that turns persisted `ChatLog` rows into the
    canonical history both the Gateway and the Provider are shown. Nothing
    else in the codebase should hand-roll a "prior conversation" string —
    route handlers and the pipeline both go through here so they can never
    silently drift apart.
    """

    def __init__(self, chat_log, *, max_chars: int | None = DEFAULT_MAX_CHARS,
                 max_turns: int | None = DEFAULT_MAX_TURNS):
        self.chat_log = chat_log
        self.max_chars = None if max_chars is None else int(max_chars)
        self.max_turns = None if max_turns is None else int(max_turns)

    def build(self, conversation_id: int | None, *,
              exclude_message_id: int | None = None,
              max_chars: int | None = None,
              max_turns: int | None = None) -> ConversationContext:
        """Chronological, trimmed history for `conversation_id` only.

        `exclude_message_id`: pass the row id of the current user message
        if it has already been persisted (e.g. via `ChatLog.add_user`)
        before this is called, so it is never echoed back into its own
        history (requirement: no current-message duplication). Preferred
        usage is to call `build()` BEFORE persisting the current message at
        all, so there is nothing to exclude.
        """
        if self.chat_log is None or conversation_id is None:
            return ConversationContext(messages=[], conversation_id=conversation_id)

        effective_turns = max_turns if max_turns is not None else self.max_turns
        effective_chars = max_chars if max_chars is not None else self.max_chars
        # 0 == "no limit" (see `_trim`).
        turns_limit = 0 if effective_turns is None else max(0, int(effective_turns))
        chars_limit = 0 if effective_chars is None else max(0, int(effective_chars))

        # Over-fetch a bit: some rows may be dropped below (the excluded
        # current message, empty text, failed assistant replies), and we
        # still want `turns_limit` genuine turns to trim from afterwards.
        # With no turn limit we ask for the WHOLE conversation (limit=0),
        # never an artificial ceiling.
        fetch_limit = (max(turns_limit * 3 + 10, 50) if turns_limit else 0)
        raw = self.chat_log.history(
            limit=fetch_limit, conversation_id=conversation_id)
        rows = raw.get("messages") or []

        turns = []
        for r in rows:
            if exclude_message_id is not None and r.get("id") == exclude_message_id:
                continue
            role = "user" if r.get("role") == "user" else "assistant"
            text = (r.get("text") or "").strip()
            if not text:
                continue
            if role == "assistant" and not r.get("ok", True):
                # A failed/errored reply is noise, not useful conversational
                # context, and would only teach the model to repeat a failure.
                continue
            turns.append({"role": role, "content": text})

        trimmed = self._trim(turns, chars_limit, turns_limit)
        return ConversationContext(messages=trimmed, conversation_id=conversation_id)

    @staticmethod
    def _trim(turns: list[dict], max_chars: int, max_turns: int) -> list[dict]:
        """Deterministic trim. `0` for either limit means NO LIMIT — the full
        history is preserved (provider-aware fitting to the selected model's
        real context window happens later, in astra.ai.context_budget). When
        a budget IS supplied, keep the newest turns first and drop older ones
        once it is spent; never return empty just because the newest turn
        alone exceeds the budget — at least the latest turn is always kept,
        since that is what a "why?"/"continue" follow-up resolves against."""
        candidates = turns[-max_turns:] if max_turns else list(turns)
        kept = []
        total = 0
        for t in reversed(candidates):
            n = len(t["content"])
            if kept and max_chars and total + n > max_chars:
                break
            kept.append(t)
            total += n
        kept.reverse()
        return kept
