"""High-level policies: whether a tool/plugin may act autonomously.

The Policy in `astra.core.permissions` maps a permission level to a decision;
this module attaches a human reason and handles overrides (allowing a specific
tool once, or remembering confirmation for a session).
"""
from __future__ import annotations

from .permissions import Level, Policy


def evaluate(tool_name: str, level: int, requires_confirmation: bool,
             policy: Policy | None = None,
             overrides: dict | None = None) -> tuple[str, str]:
    """Return (decision, reason). decision in {'allow','ask','deny'}.

    overrides: {tool_name: bool} — a one-shot session allow ("allow this time"),
    used when a tool defers to WAITING_USER and the user says yes.
    """
    overrides = overrides or {}
    policy = policy or Policy()
    if overrides.get(tool_name) is True:
        return "allow", "user approved this invocation"
    decision = policy.decision(level, requires_confirmation, tool_name)
    if decision == "allow":
        return "allow", "within granted permissions"
    if decision == "deny":
        return "deny", f"requires permission {Level.NAMES.get(level, level)} " \
                       f"(not granted)"
    return decision, f"requires confirmation ({Level.NAMES.get(level, '?')})"