"""ResearchAgent — web search, URL fetch, compare, report."""
from __future__ import annotations

from .base import SpecialistAgent


class ResearchAgent(SpecialistAgent):
    name = "research"
    icon = "🔎"
    description = ("Research projects and websites: search the web, fetch "
                   "pages, compare sources and write reports.")
    capabilities = ["research", "web_search", "url_fetch", "summary",
                    "compare", "report"]
    tools = ["search_web", "fetch_url", "read_file", "write_file", "remember"]
    preferred_task_types = ("research", "summarization", "structured_output")
    required_model_capabilities = ["chat", "tools", "json"]
    priority = 30

    def score(self, goal: str, task_type: str = "") -> int:
        import re
        low = goal.lower()
        if re.search(r"research|compare|report|what is|about |analyse|analyze|"
                     r"review|investigate|project info|read this", low):
            return 20
        return 0

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "research",
                "required_capabilities": ["chat", "tools", "json"],
                "reasoning_level": "high"}

    def decorate_plan(self, plan: list[dict], goal: str) -> list[dict]:
        """Ensure a fetch step exists for a URL-bearing research goal."""
        import re
        if not plan:
            return plan
        url = re.search(r"https?://\S+", goal)
        if url and not any(s.get("tool") in ("fetch_url", "search_web")
                           for s in plan):
            plan.insert(0, {"id": "r0", "tool": "fetch_url",
                            "params": {"url": url.group(0).rstrip(".,;)")},
                            "description": "Fetch target page",
                            "verify": [], "retries": 1})
        return plan