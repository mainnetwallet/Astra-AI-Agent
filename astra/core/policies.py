"""High-level policies: whether a tool may act autonomously.

The Policy in `astra.core.permissions` maps a permission level to a decision;
this module attaches a human reason and handles overrides (allowing a specific
tool once, or remembering confirmation for a session).
"""
from __future__ import annotations

from .permissions import Level, Policy


def evaluate(tool_name: str, level: int, requires_confirmation: bool,
             policy: Policy | None = None,
             overrides: dict | None = None,
             confirmation_delegate: str = "") -> tuple[str, str]:
    """Return (decision, reason). decision in {'allow','ask','deny'}.

    overrides: {tool_name: bool} — a one-shot session allow ("allow this time"),
    used when a tool defers to WAITING_USER and the user says yes.

    confirmation_delegate: passed straight through to `Policy.decision` —
    when non-empty, this tool has its own deterministic confirm/allow/block
    gate (e.g. the Web3 transaction policy for `tx_prepare`) and the generic
    ask-prompt must not double-gate it on top of that. The generic
    permission-level check (is this risk level granted at all?) still runs
    first and can still deny the tool outright; only the boolean
    requires_confirmation ask-branch is deferred to the tool's own policy.
    """
    overrides = overrides or {}
    policy = policy or Policy()
    if overrides.get(tool_name) is True:
        return "allow", "user approved this invocation"
    decision = policy.decision(level, requires_confirmation, tool_name,
                               confirmation_delegate=confirmation_delegate)
    if decision == "allow":
        if confirmation_delegate and requires_confirmation:
            return "allow", f"confirmation delegated to {confirmation_delegate} policy"
        return "allow", "within granted permissions"
    if decision == "deny":
        return "deny", f"requires permission {Level.NAMES.get(level, level)} " \
                       f"(not granted)"
    return decision, f"requires confirmation ({Level.NAMES.get(level, '?')})"
