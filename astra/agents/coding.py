"""CodingAgent — inspect, edit, run tests, fix, rerun. Controlled subprocesses."""
from __future__ import annotations

from .base import SpecialistAgent


class CodingAgent(SpecialistAgent):
    name = "coding"
    icon = "💻"
    description = ("Inspect codebases, edit/create files, run tests, read "
                   "failures, fix and rerun — inside a policy-enforced "
                   "workspace with controlled subprocesses.")
    capabilities = ["coding", "repo_inspect", "code_edit", "test_run"]
    tools = ["read_file", "write_file", "list_files", "search_files",
             "run_command", "search_web"]
    preferred_task_types = ("coding", "reasoning")
    required_model_capabilities = ["chat", "tools", "json", "coding", "reasoning"]
    priority = 25

    def score(self, goal: str, task_type: str = "") -> int:
        import re
        low = goal.lower()
        if re.search(r"\b(code|fix|test|debug|refactor|github|repo|function|"
                     r"script|compile|error)\b", low):
            return 30
        return 0

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "coding",
                "required_capabilities": ["chat", "tools", "json", "coding"],
                "reasoning_level": "high"}