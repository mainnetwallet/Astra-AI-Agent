"""The final response boundary — the last line of defense before any text
reaches `/api/chat` / the frontend.

Root cause of the internal-tool-call leak (see `agent_tool_loop._parse_action`
for the primary fix): the strict `raw.startswith("{") and raw.endswith("}")`
gate meant `loads_lenient`'s embedded-JSON extraction was never reached for a
model reply that prefaces or follows its tool-call JSON with any prose (the
overwhelmingly common case). That parser bug is fixed. This module is the
second, independent layer: even a genuinely malformed/truncated reply (e.g.
cut off by a max_tokens limit mid-JSON) must never let internal tool
protocol — `{"action": "tool", "tool": ..., "args": ..., "session_id": ...,
"thought": ...}` or its "final" counterpart — or a raw credential reach the
user. This is a real code-level guard, not a system-prompt instruction, and
it is applied at the ONE true choke point every chat reply passes through
before leaving the process: `ChatPipeline._reply` (see chat_pipeline.py) and
the `Agent.handle` exception fallback (see agent.py).
"""
from __future__ import annotations

import re

from astra.security import redact_text

# Keys that, in combination, identify Astra's internal tool-call/agent
# protocol (see agent_tool_loop.TOOL_PROTOCOL). Any one of these alone can
# appear in ordinary conversation (a user might paste JSON containing
# "action"); requiring at least two is a low-false-positive signal that a
# JSON blob is internal protocol, not legitimate content.
_PROTOCOL_KEYS = ("action", "tool", "args", "session_id", "thought")

# A generated image/audio response comes back from the adapter as a raw
# `data:image/png;base64,<...>` (or audio/*) string — often hundreds of KB.
# Images are produced by the Gateway's ImageRouter
# (astra/ai/image_router.py) and audio by astra.ai.router._attempt's
# text_to_speech() dispatch; either way astra.ai.artifact_extraction.
# extract_artifacts() already turns that same string into a real,
# downloadable artifact (see ChatPipeline._artifacts and the Chat UI's
# renderArtifact()), so it must never ALSO be dumped into the visible chat
# bubble as a wall of base64 text — that used to be exactly what happened,
# since nothing stripped it before it reached `reply`.
_DATA_URI_MEDIA_RE = re.compile(
    r"data:(?:image|audio)/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+")


def _looks_like_protocol_json(fragment: str) -> bool:
    hits = sum(1 for k in _PROTOCOL_KEYS if f'"{k}"' in fragment)
    return hits >= 2


def _scan_top_level_objects(text: str):
    """Yield (start, end, closed) for every top-level `{...}` span in
    `text`, string- and nesting-aware (so `{"args": {"command": "ls"}}`
    yields ONE outer span, not the inner `{"command": "ls"}` — Astra's own
    protocol always nests `args` one level deep, which a naive
    non-nesting regex misses entirely). `closed=False` means depth never
    returned to 0 before the string ended — a reply truncated mid-JSON
    (e.g. cut off by a max_tokens limit), which is exactly the case a
    strict/complete-JSON-only check would let through untouched."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        start = i
        depth = 0
        in_string = False
        escape = False
        j = i
        closed = False
        while j < n:
            ch = text[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        closed = True
                        break
            j += 1
        yield start, j, closed
        i = j if j > start else start + 1


def _strip_protocol_fragments(text: str) -> tuple[str, bool]:
    """Remove every top-level JSON object (balanced, or run-on to the end
    of the string if truncated) that looks like internal tool protocol.
    Returns (cleaned_text, removed_any)."""
    removed = False
    out = []
    last = 0
    for start, end, _closed in _scan_top_level_objects(text):
        frag = text[start:end]
        if _looks_like_protocol_json(frag):
            out.append(text[last:start])
            removed = True
            last = end
    out.append(text[last:])
    return "".join(out), removed

FALLBACK_TEXT = (
    "Sorry — I ran into an internal formatting issue producing that reply. "
    "Please try again.")

# Shown when stripping an embedded media data URI leaves nothing else in the
# reply (the common case for a plain "generate an image of X" turn, where the
# adapter's entire output IS the data URI) — the artifact card is the real
# answer here, this is just the accompanying chat line.
MEDIA_FALLBACK_TEXT = "Ready — dekhe nin niche."


def _strip_embedded_media(text: str) -> tuple[str, bool]:
    """Remove any embedded base64 image/audio data URI. Returns
    (cleaned_text, removed_any) — mirrors _strip_protocol_fragments so the
    caller can tell an "everything was media" reply from ordinary text."""
    cleaned, n = _DATA_URI_MEDIA_RE.subn("", text)
    return cleaned, n > 0


def sanitize_final_response(text: str) -> str:
    """The response-boundary guard. Call this on every piece of text that is
    about to be handed back as the `reply` field of a chat turn, regardless
    of which path produced it (tool loop, single-call provider path, error
    fallback). Two independent protections, always both applied:

    1. Strip any embedded internal tool-call/agent-protocol JSON
       (action/tool/args/session_id/thought) that slipped through — a
       normalization guard against accidental protocol leakage, not a
       prompt instruction.
    2. Redact anything that looks like a credential/token/secret (reuses
       `astra.security.redact_text`, which already recognizes GitHub PATs,
       OpenAI-shaped keys, bearer tokens, etc.) so a token echoed back in a
       tool result, error message or terminal transcript never reaches the
       API response.
    """
    if not text:
        return text
    cleaned, removed_protocol = _strip_protocol_fragments(text)
    cleaned, removed_media = _strip_embedded_media(cleaned)
    cleaned = redact_text(cleaned)
    cleaned = cleaned.strip()
    if removed_protocol and not cleaned:
        return FALLBACK_TEXT
    if removed_media and not cleaned:
        return MEDIA_FALLBACK_TEXT
    return cleaned if cleaned else text
