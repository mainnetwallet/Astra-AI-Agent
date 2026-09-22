"""Authoritative, human-facing runtime capability summary for capability
questions ("what can you do?", "Tomar ki ki tools available?").

Built fresh from the ONE `ToolRegistry` (see `astra.tools.registry`) on
every call — never hardcoded, never baked into the static Core System
Prompt. Feed the result to `astra.ai.system_prompt.build_system_prompt`'s
`runtime_context` parameter so it is composed once, per request, on top
of the Core + specialized layers (see that module's docstring for the
full architecture this preserves).

This is intentionally a DIFFERENT artifact from
`astra.ai.agent_tool_loop.build_tool_catalog`:

  - `build_tool_catalog` is the INTERNAL, exact-name, machine-facing tool
    list a model uses to actually invoke a tool through the JSON tool
    protocol (exact names, argument schemas). It must never be described
    as "what Astra can do" verbatim — that leaks implementation detail.
  - `build_capability_context` (this module) is the EXTERNAL, clean,
    human-facing description of what those registered tools let Astra do,
    grouped by category. It never lists raw tool names, argument schemas,
    or anything about the tool-call protocol, so it is safe to hand to
    ANY AI call — even a plain chat turn that will never touch the tool
    loop — as grounding for a capability question.

Only categories that are ACTUALLY registered on the registry right now
are ever mentioned. An empty/missing registry produces an explicit "no
external tools" statement rather than silence, so the model is never
left to guess (and never left free to invent access it doesn't have).
"""
from __future__ import annotations

# category (as used by astra.tools.builtins / astra.terminal / astra.browser
# / astra.web3) -> (clean user-facing label, short practical description).
# Unknown/future categories still work via `_label_for`'s fallback — this
# table only makes the common ones read naturally.
_CATEGORY_INFO: dict[str, tuple[str, str]] = {
    "terminal": ("terminal/shell access",
                "run shell commands, scripts and tests in a persistent "
                "session"),
    "browser": ("web browsing", "open and read live web pages"),
    "web3": ("web3/blockchain tools",
            "check wallets and prepare on-chain transactions"),
    "files": ("file access",
             "read, write and search files in the workspace"),
    "memory": ("memory", "save and recall notes across the conversation"),
    "tasks": ("task tracking", "create and list tasks"),
    "research": ("web research", "search the web and fetch page content"),
    "wallet": ("wallet info", "check wallet balances"),
    "system": ("system diagnostics", "report on Astra's own runtime health"),
}

NO_TOOLS_MESSAGE = (
    "Runtime capability catalog (authoritative — ground every capability "
    "claim in this, never invent one): no external tools are currently "
    "available in this runtime — no terminal, browser, file-system, "
    "web3, or other tool access. Answer capability questions honestly: "
    "only reasoning, knowledge, and text/code generation are available "
    "right now.")

_HEADER = (
    "Runtime capability catalog (authoritative — ground every capability "
    "claim in this list; never claim a category that is not listed here "
    "and never invent tool names):")


def _label_for(category: str) -> tuple[str, str]:
    key = (category or "").strip().lower()
    if key in _CATEGORY_INFO:
        return _CATEGORY_INFO[key]
    clean = key.replace("_", " ").replace("-", " ").strip() or "tool"
    return clean, ""


def build_capability_context(registry) -> str:
    """A short, deterministic, human-facing block naming the tool
    CATEGORIES actually registered on `registry` right now.

    Safe to drop straight into `runtime_context`: no tool names, no
    argument schemas, no JSON tool-call protocol — only a clean
    description of what Astra can actually do. Returns
    `NO_TOOLS_MESSAGE` when `registry` is None, unusable, or empty, so a
    capability question always gets an honest, grounded answer instead
    of silence the model could fill in with a guess.
    """
    if registry is None:
        return NO_TOOLS_MESSAGE
    try:
        tools = registry.list()
    except Exception:
        return NO_TOOLS_MESSAGE
    if not tools:
        return NO_TOOLS_MESSAGE

    counts: dict[str, int] = {}
    for t in tools:
        cat = (t.get("category") if isinstance(t, dict) else "") or "other"
        cat = cat.strip().lower() or "other"
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return NO_TOOLS_MESSAGE

    lines = [_HEADER]
    for cat in sorted(counts):
        label, desc = _label_for(cat)
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- {label}{suffix}")
    return "\n".join(lines)
