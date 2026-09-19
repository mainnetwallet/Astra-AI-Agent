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

    def list(self) -> list[dict]:
        return [t.describe() for t in self._tools.values()]

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

    # -- rate limiting --------------------------------------------------------
    def _enforce_rate_limit(self, tool: Tool) -> None:
        if tool.rate_limit_per_min <= 0:
            return
        interval = 60.0 / tool.rate_limit_per_min
        last = self._rate_limit_last.get(tool.name)
        now = time.monotonic()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._rate_limit_last[tool.name] = time.monotonic()

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
                allow_confirmation: bool = True) -> dict:
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

        start = time.perf_counter()
        try:
            result = self._invoke_with_retry(tool, args, ctx)
        except Exception:
            ms = (time.perf_counter() - start) * 1000.0
            self._note(name, errored=True, ms=ms)
            raise
        ms = (time.perf_counter() - start) * 1000.0
        self._note(name, errored=False, ms=ms)
        return {"ok": True, "decision": "allow", "result": result,
                "duration_ms": ms}
