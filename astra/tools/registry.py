"""Universal tool registry for Astra.

Tools are the verbs of the agent: the orchestrator plans steps that each name
a tool, and execution goes through `ToolRegistry.execute`, which validates
input schema, checks the permission policy, measures duration, and emits
events — so every action is audited and recoverable.

Plugins may register their own tools (Plugin.tools()); built-ins live in
`astra.tools.builtins`.
"""
from __future__ import annotations

import time

from astra.core.exceptions import AstraError, PermissionError
from astra.core.policies import evaluate
from astra.core.timeutil import duration_ms
from .schemas import Tool


class ToolRegistry:
    def __init__(self, policy=None, events=None, config=None):
        self._tools: dict[str, Tool] = {}
        self.policy = policy
        self.events = events
        self.config = config
        self._stats: dict[str, dict] = {}    # name -> {calls, errors, total_ms}
        self._overrides: dict[str, bool] = {}  # tool_name -> allowed (session)

    # -- registration -------------------------------------------------------
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def register_function(self, name: str, fn, **kw):
        kw.setdefault("description", fn.__doc__ or "")
        self.register(Tool(name, fn, **kw))

    def register_plugin_tools(self, plugins) -> int:
        """Register every tool exposed by loaded plugins. Returns count."""
        n = 0
        for p in plugins:
            if not getattr(p, "enabled", True):
                continue
            for spec in p.tools() or []:
                spec = dict(spec)
                spec.setdefault("plugin", getattr(p, "slug", ""))
                spec.setdefault("risk", 1)
                self.register(Tool(**spec))
                n += 1
        return n

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def find(self, category: str | None = None) -> list[Tool]:
        tools = sorted(self._tools.values(), key=lambda t: (t.category, t.name))
        return [t for t in tools if not category or t.category == category]

    def list(self, category: str | None = None) -> list[dict]:
        return [t.describe() for t in self.find(category)]

    # -- execution ----------------------------------------------------------
    def execute(self, name: str, args: dict | None = None, ctx=None,
                allow_confirmation: bool = True) -> dict:
        """Validate, gate, run and audit one tool call.

        Returns {"ok": bool, "result": ..., "decision": ..., "reason": ...,
                 "tool": name, "duration_ms": int} or raises PermissionError
        / AstraError on hard failure. Decision 'ask' produces
        {"ok": False, "decision": "ask", "reason": ...} so the orchestrator
        can hold for WAITING_USER instead of silently proceeding.
        """
        args = dict(args or {})
        t = self._tools.get(name)
        if not t:
            raise AstraError(f"unknown tool: {name}")
        # 1. schema
        t.satisfies(args)
        # 2. policy
        if self.policy:
            decision, reason = evaluate(
                name, t.risk, t.requires_confirmation, self.policy,
                self._overrides)
            if decision == "deny":
                raise PermissionError(f"tool '{name}' denied: {reason}")
            if decision == "ask" and not allow_confirmation:
                self._note(name, errored=True)
                return {"ok": False, "decision": "ask", "reason": reason,
                        "tool": name, "result": None, "duration_ms": 0}
            if decision == "ask" and allow_confirmation:
                # orchestrator-level confirmation: return ask, don't run
                self._note(name, errored=True)
                return {"ok": False, "decision": "ask", "reason": reason,
                        "tool": name, "result": None, "duration_ms": 0}
        # 3. run
        if self.events:
            self.events.emit("tool.started", agent="tools", tool=name,
                             args={k: v for k, v in args.items()
                                   if not _secret(k)})
        t0 = time.perf_counter()
        ok, error, result = False, "", None
        try:
            result = t.fn(args, ctx)
            ok = True
            return {"ok": True, "tool": name, "result": result,
                    "decision": "allow", "duration_ms": duration_ms(t0)}
        except AstraError as e:
            error, ok = e.message or e.category, False
            self._note(name, errored=True)
            if self.events:
                self.events.emit("tool.failed", agent="tools", tool=name,
                                 error=error)
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self._note(name, errored=True)
            if self.events:
                self.events.emit("tool.failed", agent="tools", tool=name,
                                 error=error)
            raise AstraError(error) from e
        finally:
            if self.events:
                self.events.emit("tool.completed", agent="tools", tool=name,
                                 ok=ok, error=error or "",
                                 duration_ms=duration_ms(t0))

    # -- session confirm overrides ------------------------------------------
    def confirm(self, name: str, allowed: bool = True) -> None:
        """Record a user decision for a previously-asked tool."""
        self._overrides[name] = allowed

    # -- stats ---------------------------------------------------------------
    def stats(self, name: str | None = None) -> dict:
        if name:
            return self._stats.get(name, {})
        return dict(self._stats)

    def _note(self, name: str, errored: bool = False, ms: float = 0) -> None:
        st = self._stats.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0})
        st["calls"] += 1
        st["total_ms"] += ms
        st["errors"] += 1 if errored else 0


def _secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("key", "token", "secret", "pass", "seed",
                                "mnemonic", "private"))