"""Lenient JSON extraction for raw LLM text output.

Chat-tuned models (Cohere's c4ai-aya-expanse-* family among them) very
often do not return bare JSON even when explicitly asked to: they wrap it
in a ```json ... ``` fence, add a one-line preamble ("Sure, here is the
plan:"), or add trailing prose after the object. A bare `json.loads(text)`
(or `text.strip().strip("`")`, which only trims backtick characters off
the ends and leaves a leading "json\\n" language tag behind) fails on all
of these, which was surfacing as repeated
"gateway.task_completion.correction_exhausted / response is not valid
JSON" events and a misleading "no AI Provider configured" fallback reply
even when a Provider was in fact configured and answering.

`loads_lenient` tries, in order:
    1. a straight `json.loads` on the stripped text (fast path, unchanged
       behavior for models that already return bare JSON)
    2. the text with a leading/trailing ``` / ```json / ```JSON fence
       stripped off
    3. the first balanced top-level `{...}` or `[...]` substring found
       anywhere in the text (handles preamble/trailing prose)

Raises `ValueError` (same exception type `json.loads` itself raises) if
none of these produce valid JSON, so every existing `except ValueError` /
`except Exception` call site keeps working unchanged.
"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _escape_raw_control_chars(text: str) -> str:
    """Escape literal control characters (real line breaks, tabs, ...) that
    appear INSIDE a JSON string literal; everything outside a string literal
    is left untouched.

    Chat models very often put an actual line break inside a string value
    (e.g. a multi-line code answer) instead of the JSON-required `\\n`
    escape. That is invalid per the JSON grammar (raw control characters
    are not allowed unescaped inside a string) and makes an otherwise
    well-formed `{"action": "final", "answer": "...multi-line code..."}`
    object fail to parse — the single most common way a model's JSON
    action leaks to the user as a literal string (see agent_tool_loop.py /
    response_boundary.py). Quote/escape tracking mirrors `_find_balanced`
    below.
    """
    out = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\":
                out.append(ch)
                escape = True
            elif ch == '"':
                in_string = False
                out.append(ch)
            elif ch in _CONTROL_ESCAPES:
                out.append(_CONTROL_ESCAPES[ch])
            elif ord(ch) < 0x20:
                out.append("\\u%04x" % ord(ch))
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
    return "".join(out)


def _find_balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def loads_lenient(text: str):
    """Parse `text` as JSON, tolerating markdown fences and surrounding
    prose. Raises ValueError if no valid JSON can be found."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty text")

    # 1) fast path: already-bare JSON.
    try:
        return json.loads(raw)
    except ValueError:
        pass

    # 1b) same, with raw control characters inside string literals escaped
    #     first (see _escape_raw_control_chars) — tried before the fence/
    #     balanced-brace steps because it can rescue an object that is
    #     otherwise already bare and well-formed.
    sanitized = _escape_raw_control_chars(raw)
    if sanitized != raw:
        try:
            return json.loads(sanitized)
        except ValueError:
            pass

    # 2) a ```json ... ``` (or bare ``` ... ```) fence around the whole
    #    response.
    m = _FENCE_RE.match(raw)
    if m:
        body = m.group(1).strip()
        for candidate in (body, _escape_raw_control_chars(body)):
            try:
                return json.loads(candidate)
            except ValueError:
                continue

    # 3) first balanced {...} or [...] anywhere in the text (preamble
    #    and/or trailing commentary around the JSON). `_find_balanced`
    #    tracks quote state character-by-character, so an embedded raw
    #    newline never confuses where the object actually ends.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        candidate = _find_balanced(raw, open_ch, close_ch)
        if candidate is not None:
            for c in (candidate, _escape_raw_control_chars(candidate)):
                try:
                    return json.loads(c)
                except ValueError:
                    continue

    raise ValueError("no valid JSON found in text")
