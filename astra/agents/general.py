"""GeneralAgent — conversation, planning, explanation, delegation."""
from __future__ import annotations

from .base import SpecialistAgent


class GeneralAgent(SpecialistAgent):
    name = "general"
    icon = "🤖"
    description = ("Conversation, planning, explanation and delegation — the "
                   "default specialist that hands specialised work off.")
    capabilities = ["chat", "planning", "delegation", "memory", "explanation"]
    tools = ["answer", "remember", "recall", "list_tasks", "create_task",
             "get_health", "search_memory"]
    preferred_task_types = ("simple_chat", "planning", "tool_selection",
                            "translation", "summarization")
    required_model_capabilities = ["chat"]
    priority = 200

    def score(self, goal: str, task_type: str = "") -> int:
        # low, deliberate default so specialists win on their own turf
        return 1

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "simple_chat",
                "required_capabilities": ["chat"]}