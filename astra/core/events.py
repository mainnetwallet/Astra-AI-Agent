"""Live event bus for Astra.

Every subsystem (tasks, tools, workflows, scheduler, chat pipeline)
publishes events here. Events are persisted in SQLite (audit trail + dashboard
history) and can be streamed to the frontend over SSE. A tiny in-process
subscription list lets local components react immediately.
"""
from __future__ import annotations

import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    agent      TEXT DEFAULT '',
    data       TEXT DEFAULT '{}',
    created_at TEXT DEFAULT ''
);
"""

# Kinds actually emitted by the current codebase, plus the EventBus contract
# kinds for upcoming modules (browser, web3, AI providers).
EVENT_KINDS = (
    # agent execution loop
    "agent.started", "agent.thinking", "agent.planning",
    "agent.completed", "agent.failed",
    "agent.step.started", "agent.step.completed", "agent.step.failed",
    # generic task engine (status map mirrors TaskEngine.mark(); task.started
    # is also emitted by the workflow engine)
    "task.created", "task.started", "task.pending", "task.ready", "task.running",
    "task.done", "task.completed", "task.failed", "task.cancelled", "task.skipped",
    # tool registry
    "tool.started", "tool.completed", "tool.failed",
    # AI providers
    "ai.started", "ai.token", "ai.completed", "ai.failed",
    # memory + experiences + workflows + scheduler
    "memory.saved", "memory.recalled", "experience.learned",
    "workflow.started", "workflow.completed", "workflow.failed",
    "scheduler.tick",
    # browser agent
    "browser.opened", "browser.navigation", "browser.action", "browser.error",
    # web3
    "web3.transaction.prepared", "web3.transaction.submitted",
    "web3.transaction.broadcast", "web3.transaction.rejected",
    "web3.transaction.confirmed", "web3.transaction.failed",
    # plugins / providers (contract for future modules)
    "plugin.loaded", "plugin.failed", "plugin.disabled",
    "provider.selected", "provider.failed", "provider.health_changed",
    # AstraRouter (internal routing brain) — routing decision visibility
    "router.request", "router.decision", "router.fallback", "router.retry",
    "credential.rotation",
    # Astra AI Gateway (separate system: own four AI connections with
    # automatic fallback) — kept distinct from the internal router's own
    # "router.*"/"ai.*" events so the dashboard Logs panel can filter for
    # the Gateway specifically.
    "astra_gateway.request", "astra_gateway.success", "astra_gateway.error",
    "astra_gateway.stream_interrupted", "astra_gateway.test",
    # Gateway-spec supervision (§9-12, §19): deterministic result
    # validation + bounded correction, layered on top of the existing
    # retry/verify pipeline — see astra.core.classification + correction.
    "supervision.correction_requested", "supervision.correction_succeeded",
    "supervision.correction_exhausted", "supervision.validation_failed",
    # Gateway execution recovery (§2-5, §7-12, §18 of the FINAL FIX PROMPT):
    # the Gateway acting as routing/control-plane for the EXISTING Provider
    # system's own provider/model catalog — never executed against, only
    # consulted for target selection + health/cooldown bookkeeping (see
    # astra/ai/gateway_contract.py, astra/ai/gateway_recovery.py).
    "gateway.execution_completed", "gateway.execution_recovered",
    "gateway.execution_failed", "gateway.target_cooldown",
    "gateway.recovery_target_selected",
    # Gateway-OWNED result supervision (§6-12): distinct from
    # "supervision.*" above (that's the tool-output validation path)
    # — these fire when the Gateway itself validates a Provider's raw
    # response and drives a correction back through ProviderExecutionPort
    # to the SAME target (see astra/ai/gateway_supervision.py).
    "gateway.supervision.correction_requested",
    "gateway.supervision.correction_succeeded",
    "gateway.supervision.correction_failed",
    "gateway.supervision.correction_exhausted",
    "router.gateway_supervision",
    # Gateway task-completion supervisor (astra/ai/gateway_task_completion.py):
    # verify -> correct -> re-verify against the Task Completion Contract.
    "router.gateway_task_completion",
    "gateway.task_completion.correction_requested",
    "gateway.task_completion.correction_succeeded",
    "gateway.task_completion.correction_failed",
    "gateway.task_completion.correction_exhausted",
    # Chat pipeline (astra/ai/chat_pipeline.py): Gateway understand+assign ->
    # Provider -> Gateway verify -> user.
    "chat.pipeline.started", "chat.pipeline.assigned",
    "chat.pipeline.understand_failed", "chat.pipeline.verified",
    "chat.pipeline.verify_error", "chat.pipeline.finished",
    "chat.pipeline.failed",
    # Startup reconciliation (EventBus.reconcile_stale_operations): closes an
    # operation that began in a previous run and can therefore never finish,
    # so its start row does not stay "… running" in the Activity Log forever.
    "operation.interrupted",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_op_id() -> str:
    """A short, unique id for one operation (a provider call, a tool call, a
    chat request...).

    Lifecycle events (e.g. `tool.started` / `tool.completed`) carry this on
    the event payload so consumers can pair a start with its terminal event
    exactly — no guessing from titles, and concurrent operations of the same
    kind never collide."""
    import uuid
    return uuid.uuid4().hex[:12]


class EventBus:
    def __init__(self, store, limit: int = 2000):
        self.store = store
        self._limit = limit
        self._lock = threading.RLock()
        self._subscribers: list = []
        if not store.table_exists("events"):
            store.install(SCHEMA)

    def subscribe(self, fn) -> None:
        self._subscribers.append(fn)

    def emit(self, kind: str, agent: str = "", **data) -> dict:
        """Persist + broadcast one event. Returns the stored record."""
        if kind not in EVENT_KINDS:
            # allow unknown kinds but log a warning — don't silently drop
            import warnings
            warnings.warn(f"unknown event kind: {kind!r}", UserWarning, stacklevel=2)
        from json import dumps
        # One timestamp for both the persisted row and the SSE broadcast, so
        # history and the live feed order an event identically (two _now()
        # calls could straddle a second boundary and disagree by 1s).
        created_at = _now()
        with self._lock:
            rid = self.store.insert(
                "events", kind=kind, agent=agent,
                data=dumps(data, ensure_ascii=False), created_at=created_at)
            self._prune()
        record = {"id": rid, "kind": kind, "agent": agent, "data": data,
                  "created_at": created_at}
        for fn in list(self._subscribers):
            try:
                fn(record)
            except Exception:
                pass
        return record

    # Terminal suffixes the Activity Log treats as "this operation ended"
    # (mirrors static/js/log_model.js). Kept in sync by
    # tests/test_lifecycle_terminal_events.py.
    _TERMINAL_SUFFIXES = ("completed", "succeeded", "failed", "error",
                          "timeout", "cancelled", "rejected", "confirmed",
                          "done", "finished", "exhausted", "interrupted")

    @classmethod
    def _is_terminal(cls, kind: str, data: dict) -> bool:
        if (data or {}).get("terminal") is True:
            return True
        return str(kind or "").rsplit(".", 1)[-1] in cls._TERMINAL_SUFFIXES

    def reconcile_stale_operations(self, scan: int = 500,
                                   limit: int = 50) -> int:
        """Close operations that can never finish because the process that
        started them is gone.

        Called once at startup (before any new event is emitted): any
        `op`-correlated operation that has a start event but no terminal
        event in the recent history belonged to the previous run, so it gets
        one synthetic `operation.interrupted` terminal. Without this, an
        interrupted request (server restart, crash, aborted stream) leaves a
        permanent "… running" row in the Activity Log with no matching
        terminal event ever arriving. Returns how many rows were closed.
        """
        try:
            rows = list(reversed(self.history(limit=scan)))
        except Exception:
            return 0
        starts: dict[str, dict] = {}
        terminals: set[str] = set()
        for r in rows:
            d = r.get("data") or {}
            op = d.get("op")
            if not op:
                continue
            if self._is_terminal(r.get("kind"), d):
                terminals.add(op)
            else:
                starts.setdefault(op, r)
        stale = [(op, r) for op, r in starts.items() if op not in terminals]

        # A child operation whose `trace`/`request` is the `request` of
        # another stale operation is resolved by that parent's own terminal
        # (the Activity Log closes a request's still-running children), so
        # emitting a second terminal for it would append a duplicate row.
        # Only the parent(s) get one.
        parents: dict[str, set] = {}
        for op, r in stale:
            req = (r.get("data") or {}).get("request")
            if req:
                parents.setdefault(req, set()).add(op)
        # Requests that already have a terminal in the window: their children
        # are resolved by that terminal too (now, or when a client replays
        # this history), so they must not be double-closed here.
        closed_requests = set()
        for r in rows:
            d = r.get("data") or {}
            if self._is_terminal(r.get("kind"), d) and d.get("request"):
                closed_requests.add(d["request"])

        def _covered(op, r):
            d = r.get("data") or {}
            for field in ("trace", "request"):
                val = d.get(field)
                if not val:
                    continue
                if val in closed_requests:
                    return True
                owners = parents.get(val)
                if owners and any(o != op for o in owners):
                    return True
            return False

        stale = [(op, r) for op, r in stale if not _covered(op, r)]
        closed = 0
        for op, r in stale[:limit]:
            d = r.get("data") or {}
            try:
                self.emit(
                    "operation.interrupted", agent=r.get("agent") or "",
                    op=op, trace=d.get("trace", ""),
                    request=d.get("request", ""),
                    original_kind=r.get("kind", ""),
                    reason="interrupted when the app stopped",
                    terminal=True)
                closed += 1
            except Exception:
                pass
        return closed

    def _prune(self) -> None:
        if not self._limit:
            return
        try:
            self.store.exec(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (self._limit,))
        except Exception:
            pass

    def history(self, limit: int = 50, after_id: int = 0) -> list[dict]:
        rows = self.store.fetch(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
            (after_id, limit))
        return [{**r, "data": _json(r.get("data") or "{}")} for r in rows]

    def since(self, after_id: int) -> list[dict]:
        """Chronological events emitted after an id (for SSE tailing)."""
        rows = self.store.fetch(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT 200",
            (after_id,))
        return [{**r, "data": _json(r.get("data") or "{}")} for r in rows]

    def last_id(self) -> int:
        r = self.store.fetchone("SELECT MAX(id) m FROM events")
        return (r["m"] or 0) if r else 0

    def count(self) -> int:
        r = self.store.fetchone("SELECT COUNT(*) c FROM events")
        return r["c"] if r else 0

    def clear(self) -> int:
        """Wipe the persisted event log (the Logs panel's 'Clear' button —
        without this, history() keeps handing the same rows back to any
        client that reloads, since 'Clear' only ever emptied the browser's
        in-memory feed). Returns how many rows were removed."""
        with self._lock:
            n = self.count()
            self.store.exec("DELETE FROM events")
            return n


def _json(raw) -> dict:
    from json import loads
    try:
        return loads(raw)
    except Exception:
        return {}
