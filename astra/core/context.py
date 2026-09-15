"""Execution context handed to tools, planners and the orchestrator.

A ToolContext carries the shared subsystems (store, config, event bus, memory,
plugins, generic task engine) so a tool fn can do real work without reaching
for globals. An ExecutionContext tracks one in-flight execution: its steps,
artifacts and short-term scratch.
"""
from __future__ import annotations


class ToolContext:
    """Everything a tool may legitimately touch, by dependency injection."""

    def __init__(self, store=None, config=None, events=None, memory=None,
                 plugins=None, tasks=None):
        self.store = store
        self.config = config
        self.events = events
        self.memory = memory
        self.plugins = list(plugins or [])
        self.tasks = tasks

    def plugin(self, slug: str):
        for p in self.plugins:
            if p.slug == slug:
                return p
        return None

    def emit(self, kind: str, **data):
        if self.events:
            self.events.emit(kind, agent="tools", **data)


class ExecutionContext:
    """One agent execution in progress (short-term memory holder)."""

    def __init__(self, execution_id: str, goal: str, state: str = "IDLE"):
        self.execution_id = execution_id
        self.goal = goal
        self.state = state
        self.steps: list[dict] = []       # planned steps
        self.results: dict[str, dict] = {}  # step_id -> {ok, output}
        self.artifacts: list[dict] = []    # outputs kept for the report
        self.short_term: list[tuple] = []  # (role, text) conversation scratch
        self.errors: list[dict] = []

    def remember(self, text: str, role: str = "self") -> None:
        self.short_term.append((role, text))
        if len(self.short_term) > 20:
            self.short_term.pop(0)

    def transcript(self) -> str:
        return "\n".join(f"{r}: {t}" for r, t in self.short_term)