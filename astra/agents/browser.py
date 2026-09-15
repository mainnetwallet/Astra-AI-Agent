"""BrowserAgent — real Playwright-driven browser automation (graceful offline)."""
from __future__ import annotations

from .base import SpecialistAgent


class BrowserAgent(SpecialistAgent):
    name = "browser"
    icon = "🌐"
    description = ("Open websites, observe pages, click/fill/scroll and "
                   "extract structured content — verification-first.")
    capabilities = ["browser", "navigation", "click", "fill", "extract",
                    "screenshot"]
    tools = ["browser_open", "browser_observe", "browser_action",
             "browser_extract", "browser_screenshot", "browser_close"]
    preferred_task_types = ("browser", "research")
    required_model_capabilities = ["chat", "tools"]
    priority = 20

    def score(self, goal: str, task_type: str = "") -> int:
        import re
        low = goal.lower()
        if re.search(r"\b(open|visit|navigate|click|scroll|fill)\b", low) and \
           re.search(r"https?://|\bwebsite\b|\bsite\b|\bpage\b", low):
            return 25
        return 0

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "browser", "required_capabilities": ["chat", "tools"]}