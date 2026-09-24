"""Authoritative, human-facing runtime capability summary for capability
questions ("what can you do?", "Tomar ki ki tools available?").

Built fresh from the ONE `ToolRegistry` (see `astra.tools.registry`) on
every call — never hardcoded, never baked into the static Core System
Prompt. Feed the result to `astra.ai.system_prompt.build_system_prompt`'s
`runtime_context` parameter so it is composed once, per request, on top
of the Core + specialized layers (see that module's docstring for the
full architecture this preserves).

`collect_runtime_capabilities(registry)` returns the ONE authoritative
`RuntimeCapabilities` value for a turn: the live category set, the
human-facing block, and the machine-facing capability-ID list the Gateway
uses for its structured execution decision. `build_capability_context()` is
the thin backward-compatible wrapper for the human-facing block alone.

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

from dataclasses import dataclass, field

# category (as used by astra.tools.builtins / astra.terminal / astra.browser
# / astra.web3) -> (clean user-facing label, short practical description).
# Unknown/future categories still work via `_label_for`'s fallback — this
# table only makes the common ones read naturally.
_CATEGORY_INFO: dict[str, tuple[str, str]] = {
    "terminal": ("terminal/shell access",
                "run shell commands, scripts and tests in a persistent "
                "session"),
    # The isolated Agent Runtime (astra/runtime/). Kept distinct from
    # "terminal" so a Gateway execution decision can name the isolated
    # environment explicitly instead of the legacy host session.
    "runtime": ("isolated Agent Runtime",
                "run commands, install packages (npm/pip/apt/apk/git) and "
                "manage files inside Astra's own isolated Linux environment, "
                "plus the Astra Agent Terminal attached to it"),
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
    "ai": ("AI generation",
           "generate text/answers with an AI model"),
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


@dataclass(frozen=True)
class RuntimeCapabilities:
    """ONE authoritative runtime capability representation, derived live from
    the actual `ToolRegistry` (see `astra.tools.registry`).

    This is the single object the Gateway (request understanding/planning)
    and the Provider (execution) both read, so the two can never disagree
    about what the runtime can actually do:

      - `categories`      live tool categories (``terminal``, ``browser``,
                          ``files``, ``web3``, ``memory``, ``tasks``,
                          ``research``, ``wallet``, ``system``, ...), exactly
                          the ones with at least one registered tool.
      - `human_context`   the clean, human-facing capability block
                          (`build_capability_context`) — safe to hand to any
                          model, never names a tool or the JSON protocol.
      - `catalog_text()`  the machine-facing *capability ID* list the
                          Gateway uses to fill `execution.capability` — the
                          exact category tokens, so a structured execution
                          decision can never name a capability that does not
                          exist. It is deliberately NOT the exact tool
                          catalog (`astra.ai.agent_tool_loop.build_tool_catalog`
                          owns that machine-facing tool surface).

    Everything is derived on construction; nothing is hardcoded and nothing
    is cached — a registry change is visible on the very next request.
    """
    categories: tuple[str, ...] = ()
    _human: str = field(default="", repr=False)

    @property
    def available(self) -> bool:
        return bool(self.categories)

    def has(self, category: str) -> bool:
        return (category or "").strip().lower() in self.categories

    @property
    def human_context(self) -> str:
        return self._human or NO_TOOLS_MESSAGE

    def catalog_text(self) -> str:
        """Machine-facing capability-ID block for the Gateway's UNDERSTAND
        step. Only categories that actually exist right now appear."""
        if not self.categories:
            return _NO_CAPABILITY_IDS
        return _CAPABILITY_IDS_HEADER + ", ".join(self.categories)

    def to_dict(self) -> dict:
        return {"categories": list(self.categories)}


_NO_CAPABILITY_IDS = (
    "Runtime capability IDs available right now: (none — no tool capability "
    "exists in this runtime)")

_CAPABILITY_IDS_HEADER = (
    "Runtime capability IDs available right now (the ONLY valid values for "
    "execution.capability): ")


def collect_runtime_capabilities(registry) -> RuntimeCapabilities:
    """Derive the live `RuntimeCapabilities` from `registry` right now.

    Never hardcoded, never cached: a category appears only when the live
    `ToolRegistry` actually has a tool in it. Returns an empty (unavailable)
    representation for a missing/raising/empty registry so callers fail
    safe — they then honestly state that no tool capability exists.
    """
    if registry is None:
        return RuntimeCapabilities((), NO_TOOLS_MESSAGE)
    try:
        tools = registry.list()
    except Exception:
        return RuntimeCapabilities((), NO_TOOLS_MESSAGE)
    if not tools:
        return RuntimeCapabilities((), NO_TOOLS_MESSAGE)

    counts: dict[str, int] = {}
    for t in tools:
        cat = (t.get("category") if isinstance(t, dict) else "") or "other"
        cat = str(cat).strip().lower() or "other"
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return RuntimeCapabilities((), NO_TOOLS_MESSAGE)

    lines = [_HEADER]
    for cat in sorted(counts):
        label, desc = _label_for(cat)
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- {label}{suffix}")
    return RuntimeCapabilities(tuple(sorted(counts)), "\n".join(lines))


# ── execution policy (§10-§13) ──────────────────────────────────────────────
# The permanent terminal-execution priority, stated as a live block (never
# baked into the static Core prompt with dynamic data). Both the Gateway's
# UNDERSTAND call and every Provider call receive it, so neither can be
# unaware of the policy or of the current state of the fallback.
EXECUTION_POLICY_HEADER = "Terminal execution policy (authoritative):"


def execution_policy_block(*, runtime_available: bool = True,
                           runtime_status: str = "",
                           runtime_id: str = "",
                           runtime_backend: str = "",
                           runtime_platform: str = "",
                           runtime_distro: str = "",
                           session_id: str = "",
                           cwd: str = "",
                           host_fallback_available: bool = False,
                           host_fallback_reason: str = "",
                           pending_approvals: str = "") -> str:
    """The PRIMARY / FALLBACK / HOST-FALLBACK policy + its LIVE state.

    `runtime_backend`/`runtime_platform`/`runtime_distro` describe WHICH
    isolated runtime is providing execution on this host (`proot` on
    Android/Termux, `wsl2` + Ubuntu on Windows). They are part of the block
    so the Gateway and every Provider know the execution environment is
    `agent_runtime` with that backend - and never a Windows CMD/PowerShell
    or host shell.

    Pure and stateless: the caller passes what is actually true right now
    (never a cached guess), so the block can never claim the Agent Runtime
    is unavailable while it is available.
    """
    lines = [EXECUTION_POLICY_HEADER,
             "1. PRIMARY — Astra Agent Runtime (isolated environment). Use "
             "it first for every request it can perform. It requires NO user "
             "permission."]
    if runtime_available:
        state = runtime_status or "available"
        detail = f" (runtime={runtime_id})" if runtime_id else ""
        lines.append(f"   - Live: Agent Runtime is AVAILABLE, state={state}"
                     f"{detail}. Prefer it; never describe it as unavailable.")
    else:
        reason = f": {host_fallback_reason}" if host_fallback_reason else ""
        lines.append("   - Live: the Agent Runtime is NOT available in this "
                     f"runtime right now{reason}. Tell the user plainly; do "
                     "not pretend it worked and do not silently move to the "
                     "host.")
    if session_id or cwd:
        lines.append(f"   - This conversation's session={session_id} "
                     f"cwd={cwd or '(default)'}")
    if runtime_backend:
        identity = (f"environment=agent_runtime backend={runtime_backend}")
        if runtime_distro:
            identity += f" distro={runtime_distro}"
        if runtime_platform:
            identity += f" platform={runtime_platform}"
        lines.append(f"   - Runtime identity: {identity}. Commands run "
                     "inside that isolated Linux runtime and nowhere else; "
                     "the host shell (cmd.exe/PowerShell) is never the "
                     "runtime.")
    # 2. FALLBACK + 3. the approval rule.
    if host_fallback_available:
        lines.append("2. FALLBACK — HOST terminal. Available, but ONLY after "
                     "the user explicitly allows that exact command in the "
                     "Assistant Chat. A runtime failure never authorises it.")
    else:
        lines.append("2. FALLBACK — HOST terminal: not available in this "
                     "runtime. Do not offer or attempt host execution.")
    lines.append("3. Approvals: never assume approval, never execute a host "
                 "command before the user selects Allow, and never execute "
                 "one they denied. If the Runtime genuinely cannot perform "
                 "the operation, ask for approval with "
                 "`host_terminal_request` (exact command + cwd + why), then "
                 "stop and tell the user the approval is waiting in the chat.")
    if pending_approvals:
        lines.append("4. Pending approvals in this conversation:")
        for line in str(pending_approvals).splitlines():
            lines.append("   " + line)
    return "\n".join(lines)


def build_capability_context(registry) -> str:
    """A short, deterministic, human-facing block naming the tool
    CATEGORIES actually registered on `registry` right now.

    Safe to drop straight into `runtime_context`: no tool names, no
    argument schemas, no JSON tool-call protocol — only a clean
    description of what Astra can actually do. Returns
    `NO_TOOLS_MESSAGE` when `registry` is None, unusable, or empty, so a
    capability question always gets an honest, grounded answer instead
    of silence the model could fill in with a guess.

    Thin wrapper over `collect_runtime_capabilities` so the human block and
    the machine-facing capability-ID list are ALWAYS derived from the same
    live registry read (one source of truth, never two).
    """
    return collect_runtime_capabilities(registry).human_context
