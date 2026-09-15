"""Generic task engine for Astra.

A task is a unit of work with optional dependencies (a DAG). The orchestrator
and workflows both dispatch through here so every unit of work has one home
for persistence, retries, priorities and state. Supports the spec's example:

    Research project
          ↓        Check eligibility
          ↓        Create tasks
          ↓        Execute task
          ↓        Verify

A failed child task marks only itself failed; the workflow decides whether to
abort (dependency failure) or continue (independent siblings).
"""
from __future__ import annotations

import json
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_task_id INTEGER REFERENCES astra_tasks(id) ON DELETE CASCADE,
    goal          TEXT NOT NULL,
    description   TEXT DEFAULT '',
    type          TEXT DEFAULT 'generic',   -- airdrop|research|tool|workflow_step…
    status        TEXT DEFAULT 'pending',   -- pending|ready|running|done|failed|skipped|cancelled
    priority      INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT '',
    started_at    TEXT DEFAULT '',
    completed_at  TEXT DEFAULT '',
    retry_count   INTEGER DEFAULT 0,
    max_retries   INTEGER DEFAULT 2,
    dependencies  TEXT DEFAULT '[]',        -- JSON list of task ids
    result        TEXT DEFAULT '{}',        -- JSON dict
    error         TEXT DEFAULT '',
    metadata      TEXT DEFAULT '{}'         -- JSON dict (agent, tool, etc.)
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TaskEngine:
    def __init__(self, store, events=None, max_retries: int = 2, backoff_s: float = 1.0):
        self.store = store
        self.events = events
        self.default_max_retries = max_retries
        self.backoff_s = backoff_s
        if not store.table_exists("astra_tasks"):
            store.install(SCHEMA)

    # -- lifecycle ----------------------------------------------------------
    def create(self, goal: str, *, parent_task_id: int | None = None,
               description: str = "", type: str = "generic",
               priority: int = 0, dependencies: list[int] | None = None,
               max_retries: int | None = None, **metadata) -> dict:
        from json import dumps
        deps = dependencies or []
        tid = self.store.insert(
            "astra_tasks", parent_task_id=parent_task_id, goal=goal,
            description=description, type=type, status="pending",
            priority=priority, created_at=_now(), started_at="",
            completed_at="", retry_count=0,
            max_retries=max_retries if max_retries is not None else self.default_max_retries,
            dependencies=dumps(deps), result="{}", error="",
            metadata=dumps(metadata, ensure_ascii=False))
        task = self.get(tid)
        self._emit("task.created", task)
        return task

    def get(self, task_id: int) -> dict | None:
        return self.store.fetchone("SELECT * FROM astra_tasks WHERE id = ?", (task_id,))

    def list(self, status: str | None = None, type: str | None = None,
             limit: int = 200) -> list[dict]:
        cond, args = [], []
        if status:
            cond.append("status = ?"); args.append(status)
        if type:
            cond.append("type = ?"); args.append(type)
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        return self.store.fetch(
            f"SELECT * FROM astra_tasks{where} ORDER BY id DESC LIMIT {int(limit)}",
            tuple(args))

    def update(self, task_id: int, **fields) -> dict | None:
        allowed = {"goal", "description", "type", "status", "priority",
                   "result", "error", "metadata"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                sets.append(f"{k} = ?"); args.append(v)
        if not sets:
            return self.get(task_id)
        args.append(task_id)
        self.store.exec(f"UPDATE astra_tasks SET {', '.join(sets)} WHERE id = ?",
                        tuple(args))
        return self.get(task_id)

    def mark(self, task_id: int, status: str, error: str = "", result: dict | None = None) -> dict | None:
        now = _now()
        fields = {"status": status,
                  "started_at": self.get(task_id).get("started_at") or now if status == "running" else self.get(task_id).get("started_at"),
                  "completed_at": now if status in ("done", "failed", "cancelled", "skipped") else ""}
        t = self.update(task_id, **fields, error=error, result=result or {})
        if t:
            self._emit(f"task.{status}", t)
        return t

    # -- DAG / dependencies -------------------------------------------------
    @staticmethod
    def _deps(task: dict) -> list[int]:
        try:
            return json.loads(task.get("dependencies") or "[]")
        except Exception:
            return []

    def ready(self, task: dict) -> bool:
        """A task is ready when every dependency is 'done'."""
        return all(
            (d := self.get(i)) is not None and d["status"] == "done"
            for i in self._deps(task))

    def blocking_deps(self, task: dict) -> list[str]:
        """Human-readable blockers (dependency id + its status)."""
        out = []
        for i in self._deps(task):
            d = self.get(i)
            out.append(f"#{i}:{d['status'] if d else 'missing'}")
        return out

    def claim_ready(self) -> list[dict]:
        """All 'pending' tasks whose dependencies are satisfied, in priority
        order. Claiming marks them 'ready' for the executor."""
        claimed = []
        for t in self.list(status="pending"):
            if self.ready(t):
                self.mark(t["id"], "ready")
                claimed.append(t)
        claimed.sort(key=lambda t: -t["priority"])
        return claimed

    def execute(self, task_id: int, fn, retriable: bool = True) -> tuple[bool, dict]:
        """Run a callable on one task with retry/backoff. Returns (ok, task).

        fn(task) -> result dict (or raises). Retries respect max_retries and
        the task's own status transitions (running → done/failed)."""
        import time
        task = self.get(task_id)
        if not task:
            return False, {"error": "task not found"}
        self.mark(task_id, "running")
        attempt = task.get("retry_count") or 0
        err = ""
        while True:
            try:
                result = fn(self.get(task_id)) or {}
                return True, self.mark(task_id, "done", result=result)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                attempt += 1
                self.store.exec(
                    "UPDATE astra_tasks SET retry_count = ?, error = ? WHERE id = ?",
                    (attempt, err[:500], task_id))
                if attempt > (task.get("max_retries") or 0) or not retriable:
                    break
                time.sleep(min(self.backoff_s * attempt, 10))
        return False, self.mark(task_id, "failed", error=err,
                                result={"attempts": attempt})

    # -- subtree / workflow helpers -----------------------------------------
    def subtree(self, root_id: int) -> list[dict]:
        """All descendants of a task (BFS) — used to cancel a whole plan."""
        out, frontier = [], [root_id]
        seen = set()
        while frontier:
            cid = frontier.pop()
            seen.add(cid)
            for t in self.store.fetch(
                    "SELECT * FROM astra_tasks WHERE parent_task_id = ?", (cid,)):
                out.append(t)
                if t["id"] not in seen:
                    frontier.append(t["id"])
        return out

    def cancel_subtree(self, root_id: int) -> int:
        n = 0
        for t in self.subtree(root_id) + [self.get(root_id) or {}]:
            if t and t.get("status") in ("pending", "ready", "running"):
                self.mark(t["id"], "cancelled")
                n += 1
        return n

    def attempts(self, task_id: int) -> dict:
        """Summary of the retry history for an execution report."""
        t = self.get(task_id) or {}
        return {"attempts": t.get("retry_count", 0),
                "max_retries": t.get("max_retries", 0),
                "error": t.get("error", "")}

    # -- stats ---------------------------------------------------------------
    def stats(self) -> dict:
        rows = self.store.fetch(
            "SELECT status, COUNT(*) c FROM astra_tasks GROUP BY status")
        counts = {r["status"]: r["c"] for r in rows}
        return {"total": sum(counts.values()), "by_status": counts}

    def _emit(self, kind: str, task: dict) -> None:
        if self.events and task:
            self.events.emit(kind, agent="tasks", task_id=task["id"],
                             goal=task["goal"], status=task["status"])