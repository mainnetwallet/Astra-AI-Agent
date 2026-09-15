"""FileAgent — read/write/edit/search files within a safe workspace."""
from __future__ import annotations

from .base import SpecialistAgent


class FileAgent(SpecialistAgent):
    name = "files"
    icon = "📄"
    description = ("Read, write, edit and search files (text, JSON, CSV, "
                   "markdown, code) inside the configured workspace.")
    capabilities = ["files", "read", "write", "search", "metadata"]
    tools = ["read_file", "write_file", "list_files", "search_files"]
    preferred_task_types = ("tool_selection", "summarization")
    required_model_capabilities = ["chat", "tools"]
    priority = 40

    def score(self, goal: str, task_type: str = "") -> int:
        import re
        low = goal.lower()
        if re.search(r"\b(read|analyze|analyse|write|edit|summarize|open|save)"
                     r"\s+(the\s+)?(file|pdf|json|csv|md|txt|document)\b", low) or \
           re.search(r"\bfile\b", low):
            return 15
        return 0

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "summarization",
                "required_capabilities": ["chat", "tools"]}