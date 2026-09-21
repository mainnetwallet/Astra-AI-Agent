"""Universal tool registry: turns registered `Tool` objects into safe,
policy-gated, audited calls.

`ToolRegistry.execute()` is the single path every tool call goes
through — builtins (astra/tools/builtins.py), browser tools
(astra/browser/__init__.py), and web3 tools (astra/web3/tools.py) are
all registered here the same way. It enforces, in order:

  1. permission-level policy (astra.core.permissions.Policy.decision) —
     "deny" raises, "ask" returns a non-executing result, "allow" proceeds
  2. input-schema validation (Tool.satisfies) — raises immediately,
     never retried
  3. rate limiting (Tool.rate_limit_per_min) — sleeps to enforce a
     minimum interval between calls to the same tool
  4. execution with an optional timeout (Tool.timeout) and retry-with-
     backoff (Tool.retries / Tool.retry_backoff_s) on failure
  5. per-tool call/error/duration stats, retrievable via stats()
"""
from __future__ import annotations

import threading
import time

from astra.core.exceptions import PermissionError as ToolPermissionError
from astra.core.exceptions import TimeoutError as ToolTimeoutError
from astra.core.events import new_op_id
from astra.core.permissions import Policy
from astra.tools.schemas import Tool


class ToolRegistry:
    def __init__(self, policy: Policy | None = None, events=None, config=None):
        self.policy = policy or Policy()
        self.events = events
        self.config = config
        self._tools: dict[str, Tool] = {}
        self._stats: dict[str, dict] = {}
        self._rate_limit_last: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- registration --------------------------------------------------------
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self, category: str | None = None) -> list[dict]:
        """Registered tools as plain dicts, optionally filtered to one
        category ("browser", "builtin", "web3", ...). The filter was lost
        when this module was restored, so `list("browser")` raised
        TypeError instead of returning the browser tools."""
        return [t.describe() for t in self._tools.values()
                if not category or t.category == category]

    # -- stats -----------------------------------------------------------
    def _note(self, name: str, errored: bool, ms: float) -> None:
        with self._lock:
            st = self._stats.setdefault(
                name, {"calls": 0, "errors": 0, "total_ms": 0.0,
                       "average_duration": 0.0, "last_called": ""})
            st["calls"] += 1
            if errored:
                st["errors"] += 1
            st["total_ms"] += ms
            st["average_duration"] = st["total_ms"] / st["calls"]
            st["last_called"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def stats(self, name: str | None = None):
        if name is not None:
            return dict(self._stats.get(
                name, {"calls": 0, "errors": 0, "total_ms": 0.0,
                       "average_duration": 0.0, "last_called": ""}))
        return {n: dict(s) for n, s in self._stats.items()}

    # -- activity events -----------------------------------------------------
    @staticmethod
    def _brief(obj, limit: int = 240) -> str:
        """A tiny, redacted, JSON-safe summary of a tool's args/result for the
        Activity Log. Capped so a large payload never bloats the event table,
        and run through security.redact so no secret leaves the process."""
        try:
            from astra.security import redact as _redact
            safe = _redact(obj)
            if isinstance(safe, str):
                text = safe
            else:
                import json
                text = json.dumps(safe, ensure_ascii=False, default=str)
        except Exception:
            return ""
        return text[:limit] + ("…" if len(text) > limit else "")

    def _emit_tool(self, kind: str, tool: Tool, trace: str = "", **data) -> None:
        """Publish one tool-lifecycle event for the Activity Log.

        Best-effort: a subscriber/event-bus failure must never break the tool
        call it is describing, so this swallows everything."""
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="tools", tool=tool.name,
                             category=tool.category, trace=trace, **data)
        except Exception:
            pass

    # -- rate limiting --------------------------------------------------------
    def _enforce_rate_limit(self, tool: Tool) -> None:
        if tool.rate_limit_per_min <= 0:
            return
        interval = 60.0 / tool.rate_limit_per_min
        now = time.monotonic()
        # Reserve this call's slot atomically, then sleep OUTSIDE the lock.
        # The old read-then-sleep-then-write let two concurrent callers both
        # read the same `last` and both proceed, admitting more than the limit;
        # sleeping under the lock would instead serialize every tool call.
        with self._lock:
            last = self._rate_limit_last.get(tool.name)
            start_at = max(now, last + interval) if last is not None else now
            self._rate_limit_last[tool.name] = start_at
        wait = start_at - now
        if wait > 0:
            time.sleep(wait)

    # -- invocation (timeout) -------------------------------------------------
    def _invoke(self, tool: Tool, args: dict, ctx):
        if tool.timeout and tool.timeout > 0:
            box: dict = {}

            def runner():
                try:
                    box["result"] = tool.fn(args, ctx)
                except BaseException as e:  # noqa: BLE001 — re-raised on join
                    box["error"] = e

            th = threading.Thread(target=runner, daemon=True)
            th.start()
            th.join(tool.timeout)
            if th.is_alive():
                raise ToolTimeoutError(
                    f"tool '{tool.name}' timed out after {tool.timeout}s")
            if "error" in box:
                raise box["error"]
            return box.get("result")
        return tool.fn(args, ctx)

    def _invoke_with_retry(self, tool: Tool, args: dict, ctx):
        attempts = tool.retries + 1
        last_exc = None
        for attempt in range(attempts):
            try:
                return self._invoke(tool, args, ctx)
            except Exception as e:
                last_exc = e
                if attempt < attempts - 1:
                    if tool.retry_backoff_s:
                        time.sleep(tool.retry_backoff_s)
                    continue
                raise
        raise last_exc  # pragma: no cover — loop always returns or raises

    # -- the one true execution path ------------------------------------------
    def execute(self, name: str, args: dict | None = None, ctx=None,
                allow_confirmation: bool = True, trace: str = "") -> dict:
        args = dict(args or {})
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")

        delegate = tool.confirmation_delegate if allow_confirmation else ""
        decision = self.policy.decision(
            tool.risk, tool.requires_confirmation, tool_name=name,
            confirmation_delegate=delegate)

        if decision == "deny":
            raise ToolPermissionError(
                f"tool '{name}' denied by policy (risk={tool.risk})")
        if decision == "ask":
            return {"ok": False, "decision": "ask", "tool": name,
                    "reason": f"tool '{name}' requires confirmation"}

        # decision == "allow"
        tool.satisfies(args)   # ValidationError raises here, never retried
        self._enforce_rate_limit(tool)

        # Diagnostics tools (e.g. get_health) read `ctx.registry`; attach
        # ourselves if the caller handed in a context without one.
        if ctx is not None and getattr(ctx, "registry", None) is None:
            try:
                ctx.registry = self
            except Exception:
                pass

        start = time.perf_counter()
        op = new_op_id()
        self._emit_tool("tool.started", tool, trace=trace, op=op,
                        input=self._brief(args))
        try:
            result = self._invoke_with_retry(tool, args, ctx)
        except Exception as e:
            ms = (time.perf_counter() - start) * 1000.0
            self._note(name, errored=True, ms=ms)
            self._emit_tool("tool.failed", tool, trace=trace, op=op,
                            terminal=True, duration_ms=ms,
                            error=str(e)[:200], input=self._brief(args))
            raise
        ms = (time.perf_counter() - start) * 1000.0
        self._note(name, errored=False, ms=ms)
        self._emit_tool("tool.completed", tool, trace=trace, op=op,
                        terminal=True, duration_ms=ms, output=self._brief(result))
        return {"ok": True, "decision": "allow", "result": result,
                "duration_ms": ms}
