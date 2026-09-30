"""Global emergency shutdown for Astra's agent execution.

One backend-authoritative latch. While it is engaged Astra refuses to START
any agent work, and engaging it stops what is currently running:

  * chat / agent turns      -> `Agent.handle` answers "shutdown active"
  * agent tool calls        -> `ToolRegistry.execute` blocks them (and blocks
                               every browser/financial/system/admin tool
                               for non-agent callers too)
  * workflows + schedules   -> `WorkflowEngine.run` refuses; running runs are
                               cancelled (the engine stops them before their
                               next step — a step already executing finishes,
                               there is no thread to kill)
  * Agent Runtime terminals -> every runtime session is closed
  * host terminal sessions  -> every terminal session is closed
  * browser automation      -> every browser session is closed
  * Web3 pipeline           -> `TransactionManager.emergency_stop()` (cancels
                               PREPARED/AUTHORIZED/SIGNED transactions)

The Astra web server itself is NOT stopped — the operator must still be able
to open the Security Center and release the latch. The latch is persisted, so
a restart does not silently re-enable agents.

`engage()` reports what each subsystem actually did (or that it was not
present / failed). It never claims a stop it did not perform.
"""
from __future__ import annotations

import json
import threading
import time

from astra.core.exceptions import AstraError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_emergency_state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# Workflow run statuses that mean "finished" — anything else is still live.
_RUN_DONE = {"completed", "failed", "cancelled"}


class EmergencyShutdownActive(AstraError):
    """Raised when work is refused because emergency shutdown is active."""

    category = "EmergencyShutdown"
    retryable = False

    def __init__(self, what: str = "this operation"):
        super().__init__(
            f"emergency shutdown is active — {what} is disabled "
            "(release it from the Security Center)")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class EmergencyShutdown:
    def __init__(self, store, stack=None, events=None):
        self.store = store
        self.stack = stack if stack is not None else {}
        self.events = events
        self._lock = threading.RLock()
        self._active = False
        self._state: dict = {}
        if store is not None:
            store.install(_SCHEMA)
            self._load()

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            row = self.store.fetchone(
                "SELECT v FROM astra_emergency_state WHERE k = 'state'")
            self._state = json.loads(row["v"]) if row and row.get("v") else {}
        except Exception:
            self._state = {}
        self._active = bool(self._state.get("active"))

    def _save(self) -> None:
        if self.store is None:
            return
        self.store.exec(
            "INSERT INTO astra_emergency_state(k, v) VALUES('state', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (json.dumps(self._state),))

    # -- state -----------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> dict:
        with self._lock:
            return {"active": self._active,
                    "engaged_at": self._state.get("engaged_at") or None,
                    "released_at": self._state.get("released_at") or None,
                    "last_result": self._state.get("last_result") or None}

    def guard(self, what: str = "agent execution") -> None:
        """Raise `EmergencyShutdownActive` when the latch is engaged."""
        if self._active:
            raise EmergencyShutdownActive(what)

    # -- engage ----------------------------------------------------------
    def engage(self, actor: str = "operator") -> dict:
        """Latch the shutdown, then stop every running subsystem.

        The latch is set FIRST so no new work can start while the stops are
        still in progress."""
        with self._lock:
            already = self._active
            self._active = True
            self._state.update({"active": True, "engaged_at": _now(),
                                "engaged_by": actor})
            self._save()
        results = [self._stop_workflows(), self._stop_runtime(),
                   self._stop_terminal(), self._stop_browser(),
                   self._stop_web3(), self._note_scheduler()]
        summary = {"engaged_at": self._state["engaged_at"],
                   "already_active": already, "subsystems": results,
                   "failed": [r["subsystem"] for r in results
                              if r["status"] == "failed"]}
        with self._lock:
            self._state["last_result"] = summary
            self._save()
        self._emit("security.emergency_shutdown", results=summary["subsystems"])
        return {"active": True, **summary}

    def release(self, actor: str = "operator") -> dict:
        """Clear the latch so agents may run again. Does not restart
        anything that was stopped — sessions/runs are simply allowed anew."""
        detail = {}
        with self._lock:
            self._active = False
            self._state.update({"active": False, "released_at": _now(),
                                "released_by": actor})
            self._save()
        tx = self.stack.get("tx_manager")
        if tx is not None and hasattr(tx, "emergency_resume"):
            try:
                tx.emergency_resume()
                detail["web3_pipeline"] = "resumed"
            except Exception as exc:                   # noqa: BLE001
                detail["web3_pipeline"] = f"resume failed: {type(exc).__name__}"
        self._emit("security.emergency_released")
        return {"active": False, "released_at": self._state["released_at"],
                "detail": detail}

    # -- per-subsystem stops (each reports honestly) -----------------------
    @staticmethod
    def _row(name: str, status: str, detail: str) -> dict:
        return {"subsystem": name, "status": status, "detail": detail}

    def _guarded(self, name: str, key: str, fn) -> dict:
        target = self.stack.get(key)
        if target is None:
            return self._row(name, "absent", "not present in this build")
        try:
            return self._row(name, "stopped", fn(target))
        except Exception as exc:                       # noqa: BLE001
            return self._row(name, "failed", type(exc).__name__)

    def _stop_workflows(self) -> dict:
        def run(wf):
            cancelled = 0
            for r in wf.list_runs(limit=200):
                if r.get("status") not in _RUN_DONE:
                    wf.cancel_run(r["id"])
                    cancelled += 1
            return f"{cancelled} running workflow run(s) cancelled"
        return self._guarded("workflows", "workflows", run)

    def _stop_runtime(self) -> dict:
        return self._guarded(
            "agent_runtime", "runtime",
            lambda rt: f"{rt.close_all()} runtime session(s) closed")

    def _stop_terminal(self) -> dict:
        return self._guarded(
            "terminal", "terminal",
            lambda t: f"{t.close_all()} terminal session(s) closed")

    def _stop_browser(self) -> dict:
        def run(b):
            b.close_all()
            return "browser sessions closed"
        return self._guarded("browser", "browser_manager", run)

    def _stop_web3(self) -> dict:
        def run(tx):
            tx.emergency_stop()
            return "transaction pipeline stopped; pending transactions cancelled"
        return self._guarded("web3", "tx_manager", run)

    def _note_scheduler(self) -> dict:
        if self.stack.get("scheduler") is None:
            return self._row("scheduler", "absent", "not present in this build")
        return self._row(
            "scheduler", "blocked",
            "schedules keep ticking but every workflow they start is refused")

    # -- events ------------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="security", **data)
        except Exception:
            pass
