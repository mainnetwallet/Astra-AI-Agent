"""Live event bus for Astra.

Every subsystem (orchestrator, tasks, tools, workflows, scheduler, plugins)
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

EVENT_KINDS = ("agent.started", "agent.completed", "agent.failed",
               "task.created", "task.started", "task.completed", "task.failed",
               "tool.started", "tool.completed", "tool.failed",
               "browser.opened", "transaction.prepared", "transaction.submitted",
               "transaction.confirmed",
               "plugin.loaded", "plugin.failed", "plugin.disabled",
               "memory.saved", "workflow.started", "workflow.completed",
               "provider.selected", "provider.failed", "scheduler.tick")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        from json import dumps
        with self._lock:
            rid = self.store.insert(
                "events", kind=kind, agent=agent,
                data=dumps(data, ensure_ascii=False), created_at=_now())
            self._prune()
        record = {"id": rid, "kind": kind, "agent": agent, "data": data,
                  "created_at": _now()}
        for fn in list(self._subscribers):
            try:
                fn(record)
            except Exception:
                pass
        return record

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
        from json import loads
        rows = self.store.fetch(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
            (after_id, limit))
        return [{**r, "data": _json(r.get("data") or "{}")} for r in rows]

    def since(self, after_id: int) -> list[dict]:
        """Chronological events emitted after an id (for SSE tailing)."""
        from json import loads
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


def _json(raw) -> dict:
    from json import loads
    try:
        return loads(raw)
    except Exception:
        return {}