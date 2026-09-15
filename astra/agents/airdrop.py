"""AirdropAgent — campaign/task planning for the Airdrop plugin.

Plans airdrop work (eligibility checks, campaigns, tasks, browser runs) and
delegates: BrowserAgent handles browser operations, Web3Agent/Transaction
Manager handles any on-chain step. No airdrop logic is hard-wired into core.
"""
from __future__ import annotations

from .base import SpecialistAgent


class AirdropAgent(SpecialistAgent):
    name = "airdrop"
    icon = "🪂"
    description = ("Manage airdrop campaigns, eligibility tasks, deadlines and "
                   "browser-based automation — delegating on-chain steps to "
                   "the Web3 transaction pipeline.")
    capabilities = ["airdrop", "campaigns", "tasks", "deadlines", "eligibility"]
    tools = ["list_tasks", "create_task", "remember", "search_web",
             "browser_open", "browser_observe"]
    preferred_task_types = ("tool_selection", "web3", "browser")
    required_model_capabilities = ["chat", "tools", "json"]
    priority = 10

    def score(self, goal: str, task_type: str = "") -> int:
        import re
        low = goal.lower()
        if re.search(r"airdrop|eligibility|\bclaim\b|hamster|notcoin|task\b",
                     low):
            return 30
        return 0

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "web3",
                "required_capabilities": ["chat", "tools", "json"]}