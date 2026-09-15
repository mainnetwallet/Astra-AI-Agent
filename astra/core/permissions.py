"""Permission levels for Astra tools and actions.

Each tool declares a minimum permission level; a policy decides whether that
level is auto-allowed, asks the user, or is denied — so consequential actions
(final-signing, shell) are gated while everyday reads stay frictionless.
"""
from __future__ import annotations


class Level:
    READ = 10
    LOW_RISK_WRITE = 20
    BROWSER_ACTION = 30
    FINANCIAL_ACTION = 40
    SYSTEM_ACTION = 50
    ADMIN = 60

    NAMES = {
        10: "read", 20: "low_risk_write", 30: "browser_action",
        40: "financial_action", 50: "system_action", 60: "admin",
    }


class Policy:
    """Decides what an automated executor may do without asking."""

    def __init__(self, granted: list[str] | None = None, auto_confirm: list[str] | None = None):
        # granted: permission names auto-allowed for automated runs
        # auto_confirm: tool names whose confirmation prompt is skipped
        self.granted = set(granted or ["read", "low_risk_write"])
        self.auto_confirm = set(auto_confirm or [])

    def allows(self, level: int) -> bool:
        return Level.NAMES.get(level, "read") in self.granted

    def decision(self, level: int, requires_confirmation: bool, tool_name: str = "") -> str:
        """Return 'allow' | 'ask' | 'deny' for a tool invocation."""
        name = Level.NAMES.get(level, "read")
        if name == "admin":
            return "deny" if "admin" not in self.granted else "allow"
        if not self.allows(level):
            return "deny"
        if requires_confirmation and tool_name not in self.auto_confirm:
            return "ask"
        return "allow"

    def describe(self) -> dict:
        return {"granted": sorted(self.granted),
                "auto_confirm": sorted(self.auto_confirm)}