"""Universal tool registry for Astra.

Tools are the verbs of the agent: the orchestrator plans steps that each name
a tool, and execution goes through `ToolRegistry.execute`, which validates
input schema, checks the permission policy, enforces optional timeout/retry/
rate-limit policies, measures duration, and emits events — so every action is
audited and recoverable.

Registry 2.0 (2026-09): tools may declare `timeout`, `retries`, `idempotent`,
`supports_async`, `rate_limit_per_min` and strict unknown-arg rejection.
Stats track last_called / average_duration alongside calls/errors/total_ms.

Plugins may register their own tools (Plugin.tools()); built-ins live in
`astra.tools.builtins`.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from astra.core.exceptions import AstraError, PermissionError, ValidationError
from astra.core.policies import evaluate
from astra.core.timeutil import duration_ms
from .schemas import Tool


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _CallResult:
    """Box for one tool invocation (works with the timeout thread)."""
    def __init__(self):
        self.result = None
        self.exc = None


class ToolRegistry:
    def __init__(self, policy=None, events=None, config=None):
        self._tools: dict[str, Tool] = {}
        self.policy = policy
        self.events = events
        self.config = config
        self._stats: dict[str, dict] = {}   # name -> metrics (see _note)
        self._overrides: dict[str, bool] = {}  # tool_name -> allowed (session)
        self._rate_marks: dict[str, list] = {}  # name -> recent call times
        self._rate_lock = threading.Lock()

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
                allow_confirmation: bool = True, retries: int | None = None,
                ) -> dict:
        """Validate, gate, run and audit one tool call.

        `retries` overrides the tool's own retry count (0 = run exactly once,
        which the executor uses so *it* owns retry policy; None = tool's).
        Pipeline:
        1. input schema + strict unknown-arg check
        2. permission policy gate
        3. rate-limit wait (if rate_limit_per_min set)
        4. invoke (with optional timeout + retry loop)
        5. optional output validation
        6. always: stats + tool.completed event (via finally block)
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
                self._overrides,
                confirmation_delegate=getattr(t, "confirmation_delegate", ""))
            if decision == "deny":
                raise PermissionError(f"tool '{name}' denied: {reason}")
            if decision == "ask":
                # orchestrator always handles confirmation via override
                # or WAITING_USER — never silently proceed.
                return {"ok": False, "decision": "ask", "reason": reason,
                        "tool": name, "result": None, "duration_ms": 0}
        # 3. rate-limit
        if t.rate_limit_per_min:
            self._rate_limit_wait(name, t.rate_limit_per_min)
        # 4. event: tool.started
        if self.events:
            self.events.emit("tool.started", agent="tools", tool=name,
                             args={k: v for k, v in args.items()
                                   if not _secret(k)})
        t0 = time.perf_counter()
        ok, error, result = False, "", None
        last_exc: Exception | None = None
        max_attempts = 1 + (t.retries if retries is None else retries)
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    result = self._invoke(t, args, ctx)
                    ok = True
                    last_exc = None
                    break
                except ValidationError:
                    # never retry validation errors (deterministic)
                    raise
                except AstraError as e:
                    error, last_exc = e.message or e.category, e
                    if attempt < max_attempts:
                        time.sleep(min(t.retry_backoff_s * (attempt - 1), 8))
                    else:
                        if self.events:
                            self.events.emit("tool.failed", agent="tools",
                                             tool=name, error=error)
                        raise
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    last_exc = e
                    if attempt < max_attempts:
                        time.sleep(min(t.retry_backoff_s * (attempt - 1), 8))
                    else:
                        if self.events:
                            self.events.emit("tool.failed", agent="tools",
                                             tool=name, error=error)
                        raise AstraError(error) from e
            # 5. output validation (optional)
            if ok and result is not None and t.output:
                try:
                    t.validates_output(result)
                except ValidationError:
                    pass  # log, don't block
        finally:
            # 6. always: stats + tool.completed — runs on BOTH the success
            #    path (break exits the loop) and every raise path (validation
            #    error, AstraError exhaustion, generic-exc exhaustion).
            ms = duration_ms(t0)
            if not ok:
                error = error or (f"{type(last_exc).__name__}: {last_exc}"
                                  if last_exc else "tool failed")
            self._note(name, errored=not ok, ms=ms)
            if self.events:
                self.events.emit("tool.completed", agent="tools", tool=name,
                                 ok=ok, error=error or "",
                                 duration_ms=ms, attempt=attempt)
        return {"ok": True, "tool": name, "result": result,
                "decision": "allow", "duration_ms": ms}

    def _invoke(self, t: Tool, args: dict, ctx=None):
        """Run a tool function, enforcing timeout via a daemon thread."""
        if t.timeout <= 0:
            return t.fn(args, ctx)
        res = _CallResult()
        def _call():
            try:
                res.result = t.fn(args, ctx)
            except Exception as e:
                res.exc = e
        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(timeout=t.timeout)
        if th.is_alive():
            raise AstraError(f"tool '{t.name}' timed out after {t.timeout}s")
        if res.exc is not None:
            raise res.exc
        return res.result

    def _rate_limit_wait(self, name: str, rpm: int) -> None:
        """Block until the per-tool min interval between calls elapses.
        Bounded to 5s max to avoid pathologically long sleeps."""
        if rpm <= 0:
            return
        interval = 60.0 / rpm
        with self._rate_lock:
            marks = self._rate_marks.setdefault(name, [])
            now = time.perf_counter()
            if marks:
                elapsed = now - marks[-1]
                wait = interval - elapsed
                if wait > 0:
                    time.sleep(min(wait, 5.0))
                    now = time.perf_counter()
            marks.append(now)
            # keep at most the last 20 marks for memory safety
            if len(marks) > 20:
                self._rate_marks[name] = marks[-20:]

    # -- session confirm overrides ------------------------------------------
    def confirm(self, name: str, allowed: bool = True) -> None:
        """Record a user decision for a previously-asked tool."""
        self._overrides[name] = allowed

    # -- stats ---------------------------------------------------------------
    def stats(self, name: str | None = None) -> dict:
        if name:
            return dict(self._stats.get(name, {}))
        return {k: dict(v) for k, v in self._stats.items()}

    def _note(self, name: str, errored: bool = False, ms: float = 0) -> None:
        st = self._stats.setdefault(name, {
            "calls": 0, "errors": 0, "total_ms": 0,
            "last_called": "", "last_duration_ms": 0,
            "average_duration": 0.0,
        })
        st["calls"] += 1
        st["total_ms"] += ms
        st["errors"] += 1 if errored else 0
        st["last_called"] = _now_iso()
        st["last_duration_ms"] = round(ms, 2)
        st["average_duration"] = round(st["total_ms"] / st["calls"], 2) if st["calls"] else 0.0


def _secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("key", "token", "secret", "pass", "seed",
                                "mnemonic", "private"))