"""AgentManager — selects the right specialist for a goal.

Selection combines the AstraRouter's task-type classification with per-agent
keyword scoring. The winner steers routing hints and may decorate the plan;
multi-step work still runs through the Orchestrator → Planner → ToolRegistry
pipeline. AgentManager is not a provider and not a plugin.
"""
from __future__ import annotations

from .base import SpecialistAgent


class AgentManager:
    def __init__(self):
        self._agents: dict[str, SpecialistAgent] = {}
        self._fallback = None

    def register(self, agent: SpecialistAgent) -> None:
        if isinstance(agent, type):
            agent = agent()
        self._agents[agent.name] = agent
        if agent.name == "general":
            self._fallback = agent

    def register_many(self, agents: list) -> None:
        for a in agents:
            self.register(a)

    # -- selection -----------------------------------------------------------
    def select(self, goal: str, task_type: str = "") -> SpecialistAgent:
        """Pick the specialist with the best fit (deterministic)."""
        best, best_score = None, -1
        for agent in self._agents.values():
            s = agent.score(goal, task_type)
            if s > best_score:
                best, best_score = agent, s
        return best or self._fallback or General()

    def select_for_task_type(self, task_type: str) -> SpecialistAgent:
        for agent in self._agents.values():
            if task_type in agent.preferred_task_types:
                return agent
        return self._fallback or General()

    def route_hint(self, goal: str, task_type: str = "") -> dict:
        return self.select(goal, task_type).route_hint(goal)

    def decorate(self, goal: str, plan: list[dict], task_type: str = "") -> list[dict]:
        agent = self.select(goal, task_type)
        return agent.decorate_plan(plan, goal)

    def preconditions(self, goal: str, ctx=None) -> list[str]:
        return self.select(goal).preconditions(goal, ctx)

    # -- introspection -------------------------------------------------------
    def get(self, name: str) -> SpecialistAgent | None:
        return self._agents.get(name)

    def list(self) -> list[dict]:
        out = []
        for a in self._agents.values():
            out.append(a.describe())
        return out

    def __len__(self):
        return len(self._agents)


class General:
    name = "general"
    description = "General assistant"
    icon = "🤖"
    capabilities = ["chat"]
    preferred_task_types = ("simple_chat",)
    required_model_capabilities = ["chat"]
    priority = 999

    def score(self, goal, task_type=""):
        return 0

    def route_hint(self, goal):
        return {"task_type": "simple_chat", "required_capabilities": ["chat"]}

    def decorate_plan(self, plan, goal):
        return plan

    def preconditions(self, goal, ctx=None):
        return []

    def report_tail(self, goal, report):
        return ""