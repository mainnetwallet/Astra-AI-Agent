"""Execution context handed to tools and workflow steps.

A ToolContext carries the shared subsystems (store, config, event bus,
memory, generic task engine) so a tool fn can do real work without
reaching for globals. An ExecutionContext tracks one in-flight execution:
its steps, artifacts and short-term scratch.

NOTE: the plugin system has been removed; ToolContext no longer carries
a `plugins` list or `plugin(slug)` lookup.
"""
from __future__ import annotations


class ToolContext:
    """Everything a tool may legitimately touch, by dependency injection."""

    def __init__(self, store=None, config=None, events=None, memory=None,
                 tasks=None, web3_manager=None, registry=None):
        self.store = store
        self.config = config
        self.events = events
        self.memory = memory
        self.tasks = tasks
        self.registry = registry       # ToolRegistry, for diagnostics tools
        self.web3_manager = web3_manager
        self.tx_manager = web3_manager   # alias used by web3 tools

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
