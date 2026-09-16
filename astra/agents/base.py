"""Base class for Astra specialist agents.

A specialist declares:
  name, description, capabilities, preferred task types, tools it relies on,
  and required model capabilities for AI reasoning.

The `route_hint()` method returns routing guidance for the AstraRouter
(task_type, extra required capabilities) so the selected specialist steers
*which model class* is chosen without itself being a provider.
"""
from __future__ import annotations


class SpecialistAgent:
    name: str = "general"
    description: str = "General assistant"
    icon: str = "🤖"
    capabilities: list[str] = ["chat", "delegation", "memory"]
    tools: list[str] = []
    preferred_task_types: tuple[str, ...] = ("simple_chat", "planning")
    required_model_capabilities: list[str] = ["chat"]
    priority: int = 100          # lower selects first on a tie

    # -- matching -----------------------------------------------------------
    def score(self, goal: str, task_type: str = "") -> int:
        """How well this specialist fits a goal. Higher = better fit."""
        return 0

    def route_hint(self, goal: str) -> dict:
        """AstraRouter routing guidance for this specialist."""
        return {"task_type": "simple_chat",
                "required_capabilities": list(self.required_model_capabilities)}

    def decorate_plan(self, plan: list[dict], goal: str) -> list[dict]:
        """Optional: adjust planner steps before execution (else unchanged)."""
        return plan

    def describe(self) -> dict:
        return {"name": self.name, "icon": self.icon,
                "description": self.description,
                "capabilities": list(self.capabilities),
                "tools": list(self.tools),
                "task_types": list(self.preferred_task_types),
                "model_capabilities": list(self.required_model_capabilities)}

    # -- behaviour hooks ----------------------------------------------------
    def preconditions(self, goal: str, ctx=None) -> list[str]:
        """Optional list of warnings/preconditions before running."""
        return []

    def report_tail(self, goal: str, report: dict) -> str:
        """Optional human summary suffix appended to an execution report."""
        return ""