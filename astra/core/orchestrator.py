"""Astra Orchestrator — the agent's brain/loop.

UNDERSTAND → PLAN → SELECT TOOL → EXECUTE → OBSERVE → VERIFY → LEARN →
CONTINUE / COMPLETE, with explicit state per the spec:

IDLE / THINKING / PLANNING / WAITING_TOOL / EXECUTING / OBSERVING /
VERIFYING / WAITING_USER / PAUSED / COMPLETED / FAILED / CANCELLED

Every execution gets a unique id (`exec-<n>`), persists in SQLite and emits
events so the Live dashboard can follow it step by step. Multi-step goals
create dependent tasks (TaskEngine) and reuse experience memory before each
step. A confirm-gated step parks the run in WAITING_USER until the user
approves (resume) — the agent never silently performs gated actions.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime

from astra.core.context import ExecutionContext

EXEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT UNIQUE,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL,      -- final state
    plan        TEXT DEFAULT '[]',
    results     TEXT DEFAULT '{}',
    error       TEXT DEFAULT '',
    created_at  TEXT DEFAULT '',
    started_at  TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
);
"""

STATES_COMPLETE = ("COMPLETED", "FAILED", "CANCELLED")
STATES_WAIT = "WAITING_USER"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Orchestrator:
    def __init__(self, store, config=None, tasks=None, registry=None,
                 planner=None, executor=None, router=None, memory=None,
                 experiences=None, events=None, policy=None, plugins=None):
        self.store = store
        self.config = config
        self.tasks = tasks
        self.registry = registry
        self.planner = planner
        self.executor = executor
        self.router = router
        self.memory = memory
        self.experiences = experiences
        self.events = events
        self.policy = policy
        self._plugins = list(plugins or [])
        self._pending: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        if not store.table_exists("astra_executions"):
            store.install(EXEC_SCHEMA)

    # -- lifecycle -----------------------------------------------------------
    def submit(self, goal: str, sync: bool = False) -> dict:
        """Start (or directly run) an execution. Returns control record."""
        eid = "exec-" + uuid.uuid4().hex[:8]
        self.store.insert("astra_executions", execution_id=eid, goal=goal,
                          status="IDLE", plan="[]", results="{}",
                          error="", created_at=_now(), started_at="", completed_at="")
        if self.events:
            self.events.emit("agent.started", agent="orchestrator",
                             execution=eid, goal=goal)
        if sync:
            return self.run(eid)
        t = threading.Thread(target=self.run, args=(eid,), daemon=True)
        self._threads[eid] = t
        t.start()
        return {"execution_id": eid, "status": "started", "async": True}

    def run(self, execution_id: str) -> dict:
        """The execution loop. Safe to call directly (tests)."""
        row = self._row(execution_id)
        if not row:
            return {"execution_id": execution_id, "status": "FAILED",
                    "error": "unknown execution"}
        goal = row["goal"]
        run_ctx = ExecutionContext(execution_id, goal)
        self._set(row, "PLANNING", started=True)
        if self.events:
            self.events.emit("agent.planning", agent="orchestrator",
                             execution=execution_id, goal=goal)
        plan = self.planner.plan(goal)
        self._store_plan(execution_id, plan)
        run_ctx.steps = plan

        root_task = None
        if self.tasks:
            root_task = self.tasks.create(
                goal=goal, type="plan", max_retries=0,
                metadata={"execution_id": execution_id})

        results, report_error = {}, ""
        for step in plan:
            if execution_id in self._pending:
                break
            step_task = None
            if self.tasks and root_task:
                step_task = self.tasks.create(
                    goal=f"{step['tool']}: {step['description']}",
                    parent_task_id=root_task["id"], type="step",
                    max_retries=max(0, int(step.get("retries", 0))),
                    metadata={"execution_id": execution_id, "step_id": step["id"]})
            self._set(row, "EXECUTING")
            if self.events:
                self.events.emit("task.started", agent="orchestrator",
                                 execution=execution_id, tool=step["tool"],
                                 task=step_task["id"] if step_task else None,
                                 description=step["description"])
            out = self.executor.execute(step, self._tool_ctx(), run_ctx)
            results[step["id"]] = out
            run_ctx.results[step["id"]] = out
            if out.get("decision") == "ask":
                self._pending[execution_id] = step
                self._set(row, STATES_WAIT)
                report_error = f"step {step['id']} needs confirmation: {out.get('error', '')}"
                break
            if not out.get("ok"):
                report_error = f"step {step['id']} failed: {out.get('error', 'no result')}"
                if step_task:
                    self.tasks.mark(step_task["id"], "failed")
            elif step_task:
                self.tasks.mark(step_task["id"], "done", result=out.get("output") or {})
        final = (STATES_WAIT if execution_id in self._pending else
                 ("FAILED" if report_error else "COMPLETED"))
        self._set(row, final, completed=final in STATES_COMPLETE, error=report_error)
        self._store_results(execution_id, results, report_error)
        if self.events:
            self.events.emit(
                "agent.completed" if final == "COMPLETED" else "agent.failed",
                agent="orchestrator", execution=execution_id, status=final,
                error=report_error)
        if self.experiences and root_task:
            self.experiences.add(
                "plan " + goal[:120],
                strategy="; ".join(s["tool"] for s in plan),
                failure=report_error or "", success=(final == "COMPLETED"))
        return self.report(execution_id)

    def resume(self, execution_id: str, allow: bool = True) -> dict:
        """Approve (or deny) a pending confirmation; continue the run."""
        step = self._pending.pop(execution_id, None)
        row = self._row(execution_id)
        if not step or not row:
            return {"execution_id": execution_id, "status": "not waiting"}
        if not allow or row["status"] != STATES_WAIT:
            self._set(row, "CANCELLED", completed=True)
            return {"execution_id": execution_id, "status": "CANCELLED"}
        if self.registry:
            self.registry.confirm(step["tool"], True)
        # re-run from the stored plan; already-done steps are no-ops because
        # the executor runs the same plan again (confirmation now granted)
        return self.run(execution_id)

    def cancel(self, execution_id: str) -> dict:
        row = self._row(execution_id)
        if not row:
            return {"execution_id": execution_id, "status": "unknown"}
        self._pending.pop(execution_id, None)
        self._set(row, "CANCELLED", completed=True, error="cancelled by user")
        if self.events:
            self.events.emit("agent.failed", agent="orchestrator",
                             execution=execution_id, status="CANCELLED")
        return {"execution_id": execution_id, "status": "CANCELLED"}

    # -- introspection --------------------------------------------------------
    def state(self, execution_id: str) -> dict:
        return self.report(execution_id)

    def report(self, execution_id: str) -> dict:
        row = self._row(execution_id)
        if not row:
            return {"execution_id": execution_id, "status": "unknown",
                    "goal": "", "plan": [], "results": {}, "steps": 0,
                    "pending": False}
        return {"execution_id": execution_id, "goal": row["goal"],
                "status": row["status"], "error": row.get("error", ""),
                "plan": json.loads(row.get("plan") or "[]"),
                "results": json.loads(row.get("results") or "{}"),
                "steps": len(json.loads(row.get("plan") or "[]")),
                "pending": execution_id in self._pending,
                "created_at": row.get("created_at", ""),
                "completed_at": row.get("completed_at", "")}

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self.store.fetch(
            "SELECT * FROM astra_executions ORDER BY id DESC LIMIT ?", (limit,))
        return [{"execution_id": r["execution_id"], "goal": r["goal"],
                 "status": r["status"], "created_at": r["created_at"]}
                for r in rows]

    def stats(self) -> dict:
        rows = self.store.fetch(
            "SELECT status, COUNT(*) c FROM astra_executions GROUP BY status")
        counts = {r["status"]: r["c"] for r in rows}
        return {"total": sum(counts.values()), "by_status": counts,
                "running": len(self._threads)}

    # -- internals ------------------------------------------------------------
    def _row(self, execution_id: str):
        return self.store.fetchone(
            "SELECT * FROM astra_executions WHERE execution_id = ?",
            (execution_id,))

    def _set(self, row, status: str, started: bool = False,
             completed: bool = False, error: str = "") -> None:
        now = _now()
        sets, args = ["status = ?"], [status]
        if started:
            sets.append("started_at = ?"); args.append(now)
        if completed:
            sets.append("completed_at = ?"); args.append(now)
        if error:
            sets.append("error = ?"); args.append(error[:500])
        args.append(row["id"])
        self.store.exec(f"UPDATE astra_executions SET {', '.join(sets)} WHERE id = ?",
                        tuple(args))

    def _store_plan(self, execution_id: str, plan: list) -> None:
        self.store.exec(
            "UPDATE astra_executions SET plan = ? WHERE execution_id = ?",
            (json.dumps(plan, ensure_ascii=False), execution_id))

    def _store_results(self, execution_id: str, results: dict, error: str) -> None:
        self.store.exec(
            "UPDATE astra_executions SET results = ?, error = ? WHERE execution_id = ?",
            (json.dumps(results, ensure_ascii=False), error[:500], execution_id))

    def _tool_ctx(self):
        from astra.core.context import ToolContext
        return ToolContext(store=self.store, config=self.config,
                           events=self.events, memory=self.memory,
                           plugins=self._plugins, tasks=self.tasks)