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
import re
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
    pending_step TEXT DEFAULT '',   -- JSON step dict when WAITING_USER
    selected_agent TEXT DEFAULT '',
    selected_provider TEXT DEFAULT '',
    selected_model TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    created_at  TEXT DEFAULT '',
    started_at  TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS execution_steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    step_id      TEXT DEFAULT '',
    tool         TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    status       TEXT DEFAULT 'pending',
    result       TEXT DEFAULT '{}',
    error        TEXT DEFAULT '',
    started_at   TEXT DEFAULT '',
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
                 experiences=None, events=None, policy=None, plugins=None,
                 agents=None):
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
        self.agents = agents                 # AgentManager (specialist selection)
        self.web3_manager = None             # set by bootstrap (transaction mgr)
        self._plugins = list(plugins or [])
        self._pending: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._tool_times: dict[str, list] = {}   # execution_id -> [started_at]
        self._install_schema()

    _EXEC_COLUMN_HEALS = (
        ("pending_step", "TEXT DEFAULT ''"),
        ("selected_agent", "TEXT DEFAULT ''"),
        ("selected_provider", "TEXT DEFAULT ''"),
        ("selected_model", "TEXT DEFAULT ''"),
    )

    def _install_schema(self) -> None:
        """Self-installing table + column self-heal for older databases."""
        if not self.store.table_exists("astra_executions"):
            self.store.install(EXEC_SCHEMA)
            return
        cols = {r["name"] for r in self.store.fetch("PRAGMA table_info(astra_executions)")}
        for name, ddl in self._EXEC_COLUMN_HEALS:
            if name not in cols:
                try:
                    self.store.exec(f"ALTER TABLE astra_executions ADD COLUMN {name} {ddl}")
                except Exception:
                    pass
        if not self.store.table_exists("execution_steps"):
            self.store.install(EXEC_SCHEMA)

    # -- lifecycle -----------------------------------------------------------
    def submit(self, goal: str, sync: bool = False) -> dict:
        """Start (or directly run) an execution. Returns control record."""
        eid = "exec-" + uuid.uuid4().hex[:8]
        self.store.insert("astra_executions", execution_id=eid, goal=goal,
                          status="IDLE", plan="[]", results="{}",
                          pending_step="", error="",
                          created_at=_now(), started_at="", completed_at="")
        if self.events:
            self.events.emit("agent.started", agent="orchestrator",
                             execution=eid, goal=goal)
        if sync:
            return self.run(eid)
        t = threading.Thread(target=self.run, args=(eid,), daemon=True)
        self._threads[eid] = t
        t.start()
        return {"execution_id": eid, "status": "started", "async": True}

    MAX_REPLANS = 1      # dynamic replanning attempts per execution

    def run(self, execution_id: str, replan: int = 0,
            replan_for: str = "") -> dict:
        """The execution loop. Safe to call directly (tests).

        Idempotent by design: results already persisted are skipped, so
        resume()/replan() continue rather than redo finished steps. When a
        step fails and an alternative plan is possible, the loop replans the
        remaining goal (bounded by MAX_REPLANS) instead of giving up.
        """
        row = self._row(execution_id)
        if not row:
            return {"execution_id": execution_id, "status": "FAILED",
                    "error": "unknown execution"}
        goal = row["goal"]
        run_ctx = ExecutionContext(execution_id, goal)
        results = json.loads(row.get("results") or "{}")   # keep succeeded steps

        # specialist selection + routing hints (Agent manager)
        task_type = "simple_chat"
        selected_agent = ""
        if self.agents:
            from astra.ai.router import classify
            task_type = classify(goal)
            agent = self.agents.select(goal, task_type)
            selected_agent = agent.name

        if replan:
            # dynamic replanning: feed the failure back and drop the dead step
            if self.events:
                self.events.emit("agent.planning", agent="orchestrator",
                                 execution=execution_id, goal=goal,
                                 replan=True, error=replan_for)
            plan = self.planner.plan(goal, ctx={"replan_for": replan_for})
            plan = [s for s in plan
                    if not (s["id"] in results and results[s["id"]].get("ok"))]
            if not plan:
                plan = [self.planner._answer(goal)]
        else:
            if self.events:
                self.events.emit("agent.planning", agent="orchestrator",
                                 execution=execution_id, goal=goal,
                                 specialist=selected_agent or None,
                                 task_type=task_type)
            plan = self.planner.plan(goal)
            if self.agents:
                plan = self.agents.decorate(goal, plan, task_type)
            # plan in dependency order so `depends_on` steps run first
            plan = self._topo(plan)
        self._set(row, "PLANNING", started=not row.get("started_at"),
                  pending_step="")
        if selected_agent and (row.get("selected_agent") or "") != selected_agent:
            self.store.exec(
                "UPDATE astra_executions SET selected_agent = ? WHERE id = ?",
                (selected_agent, row["id"]))
        self._store_plan(execution_id, plan)
        run_ctx.steps = plan

        root_task = self._find_root_task(execution_id)
        if self.tasks and not root_task:
            root_task = self.tasks.create(
                goal=goal, type="plan", max_retries=0,
                metadata={"execution_id": execution_id})

        report_error = ""
        for step in plan:
            if execution_id in self._pending:
                break
            # idempotency: never redo a step that already succeeded
            prev = results.get(step["id"])
            if prev and prev.get("ok"):
                run_ctx.results[step["id"]] = prev
                continue
            step_task = self._find_step_task(execution_id, step["id"])
            if self.tasks and not step_task and root_task:
                step_task = self.tasks.create(
                    goal=f"{step['tool']}: {step['description']}",
                    parent_task_id=root_task["id"], type="step",
                    max_retries=max(0, int(step.get("retries", 0))),
                    metadata={"execution_id": execution_id, "step_id": step["id"]})
            self._set(row, "EXECUTING")
            self._record_step(execution_id, step, "running")
            sep = self._tool_times.get(execution_id)
            if sep:
                sep[0] = _now()
            if self.events:
                self.events.emit("task.started", agent="orchestrator",
                                 execution=execution_id, tool=step["tool"],
                                 task=step_task["id"] if step_task else None,
                                 description=step["description"])
            # dependency data-flow: substitute {{<step_id>.<field>}} from
            # earlier results before executing this step.
            frozen = self._resolve(step, results, {})
            out = self.executor.execute(frozen, self._tool_ctx(), run_ctx)
            if out.get("error_code"):
                step = dict(step, error_code=out["error_code"])
            results[step["id"]] = out
            run_ctx.results[step["id"]] = out
            self._record_step(execution_id, step,
                              "ok" if out.get("ok") else
                              ("wait" if out.get("decision") == "ask" else "failed"),
                              result=out)
            if self.router and out.get("response") and not row.get("selected_provider"):
                self.store.exec(
                    "UPDATE astra_executions SET selected_provider = ?, "
                    "selected_model = ? WHERE execution_id = ?",
                    (self.router.last_route()["provider"],
                     self.router.last_route()["model"], execution_id))
            if out.get("decision") == "ask":
                self._pending[execution_id] = step
                self._set(row, STATES_WAIT, pending_step=json.dumps(step))
                report_error = f"step {step['id']} needs confirmation: {out.get('error', '')}"
                break
            if not out.get("ok"):
                report_error = f"step {step['id']} failed: {out.get('error', 'no result')}"
                if step_task:
                    self.tasks.mark(step_task["id"], "failed")
                # dynamic replanning: recover from the dead end (bounded)
                if replan < self.MAX_REPLANS:
                    return self.run(execution_id, replan + 1,
                                    replan_for=report_error)
            elif step_task:
                self.tasks.mark(step_task["id"], "done", result=out.get("output") or {})
        final = (STATES_WAIT if execution_id in self._pending else
                 ("FAILED" if report_error else "COMPLETED"))
        self._set(row, final, completed=final in STATES_COMPLETE,
                  error=report_error, pending_step="")
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
        row = self._row(execution_id)
        if not row or row["status"] != STATES_WAIT:
            return {"execution_id": execution_id, "status": "not waiting"}
        # recover the step from memory, else from the persisted pending_step
        step = self._pending.pop(execution_id, None) or \
            json.loads(row.get("pending_step") or "{}")
        if not step:
            return {"execution_id": execution_id, "status": "not waiting"}
        if not allow:
            self._set(row, "CANCELLED", completed=True, pending_step="")
            return {"execution_id": execution_id, "status": "CANCELLED"}
        if self.registry:
            self.registry.confirm(step.get("tool", ""), True)
        # continue: run() skips already-succeeded steps (idempotent)
        return self.run(execution_id)

    def recover_stale(self) -> list[str]:
        """Crash recovery with recovery semantics (never mark-all-failed).

        - WAITING_USER executions are *reconstituted*: their pending step is
          restored into `_pending` and status stays WAITING_USER so the user
          can still approve/deny after a restart (recovery, not failure).
        - IDLE / PLANNING (nothing executed yet) and EXECUTING (a tool may
          have run and its effect is unknown) are terminal-failed — resuming
          mid-flight could duplicate a non-idempotent side effect, so we fail
          them with a recovery note instead of silently re-running.
        Returns ids touched.
        """
        stale = self.store.fetch(
            "SELECT execution_id, status, pending_step FROM astra_executions "
            "WHERE status IN ('IDLE', 'PLANNING', 'EXECUTING', 'WAITING_USER')")
        ids = []
        for r in stale:
            eid = r["execution_id"]
            ids.append(eid)
            if r["status"] == "WAITING_USER" and r.get("pending_step"):
                try:
                    self._pending[eid] = json.loads(r["pending_step"])
                except Exception:
                    self._pending[eid] = {}
                continue   # stays WAITING_USER; user decides
            # a mid-flight tool's effect is unknown → fail, never auto-resume
            self.store.exec(
                "UPDATE astra_executions SET status = 'FAILED', "
                "error = 'interrupted by restart (recovery); the run may "
                "have partially executed', completed_at = ? "
                "WHERE execution_id = ?",
                (_now(), eid))
            if self.events:
                self.events.emit("agent.failed", agent="orchestrator",
                                 execution=eid, status="FAILED",
                                 error="interrupted by restart (recovery)")
        # drop pending markers for anything now terminal, keep the ones
        # freshly reconstituted (status still WAITING_USER).
        self._pending = {k: v for k, v in self._pending.items()
                         if (self._row(k) or {}).get("status") == "WAITING_USER"}
        return ids

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
                    "pending": False, "steps_detail": []}
        steps_detail = self.store.fetch(
            "SELECT * FROM execution_steps WHERE execution_id = ? "
            "ORDER BY id", (execution_id,))
        return {"execution_id": execution_id, "goal": row["goal"],
                "status": row["status"], "error": row.get("error", ""),
                "plan": json.loads(row.get("plan") or "[]"),
                "results": json.loads(row.get("results") or "{}"),
                "selected_agent": row.get("selected_agent", ""),
                "selected_provider": row.get("selected_provider", ""),
                "selected_model": row.get("selected_model", ""),
                "steps": len(json.loads(row.get("plan") or "[]")),
                "pending": execution_id in self._pending,
                "pending_step": (json.loads(row.get("pending_step")) if row.get("pending_step") else None),
                "created_at": row.get("created_at", ""),
                "completed_at": row.get("completed_at", ""),
                "steps_detail": steps_detail,}

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
             completed: bool = False, error: str = "",
             pending_step: str | None = None) -> None:
        now = _now()
        sets, args = ["status = ?"], [status]
        if started:
            sets.append("started_at = ?"); args.append(now)
        if completed:
            sets.append("completed_at = ?"); args.append(now)
        if error:
            sets.append("error = ?"); args.append(error[:500])
        if pending_step is not None:
            sets.append("pending_step = ?"); args.append(pending_step)
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

    def _record_step(self, execution_id: str, step: dict, status: str,
                     result: dict | None = None) -> None:
        """Persist one row into execution_steps (per-step execution trail)."""
        try:
            started = _now() if status == "running" else ""
            if result is None:
                # running marker: no result yet
                self.store.insert(
                    "execution_steps", execution_id=execution_id,
                    step_id=step.get("id", ""), tool=step.get("tool", ""),
                    description=step.get("description", ""), status=status,
                    result="{}", error="", started_at=started, completed_at="")
                self._tool_times[execution_id] = [started]
                return
            status_s = "completed" if status == "ok" else status
            error = result.get("error") or ""
            error_code = result.get("error_code") or step.get("error_code") or ""
            completed = _now()
            started = (self._tool_times.get(execution_id) or [""])[0]
            out = {}
            if result.get("output") is not None:
                out["output"] = result.get("output")
            if result.get("summary"):
                out["summary"] = result["summary"]
            if result.get("decision") == "ask":
                out["pending"] = True
            self.store.ensure_column("execution_steps", "error_code",
                                     "TEXT DEFAULT ''")
            self.store.insert(
                "execution_steps", execution_id=execution_id,
                step_id=step.get("id", ""), tool=step.get("tool", ""),
                description=step.get("description", ""), status=status_s,
                result=json.dumps(out, ensure_ascii=False)[:4000], error=error,
                error_code=error_code,
                started_at=started, completed_at=completed)
        except Exception:
            pass   # step trail is advisory; never break the run on it

    def _find_root_task(self, execution_id: str) -> dict | None:
        """Reuse the plan task for this execution instead of duplicating it on
        resume()/replan(). TaskEngine.list() can't filter on metadata JSON, so
        scan the small working set in Python."""
        if not self.tasks:
            return None
        for t in self.tasks.list(type="plan", limit=200):
            meta = t.get("metadata") or "{}"
            try:
                m = json.loads(meta) if isinstance(meta, str) else meta
            except Exception:
                continue
            if m.get("execution_id") == execution_id:
                return t
        return None

    def _find_step_task(self, execution_id: str, step_id: str) -> dict | None:
        if not self.tasks:
            return None
        for t in self.tasks.list(type="step", limit=200):
            meta = t.get("metadata") or "{}"
            try:
                m = json.loads(meta) if isinstance(meta, str) else meta
            except Exception:
                continue
            if m.get("execution_id") == execution_id and m.get("step_id") == step_id:
                return t
        return None

    def _tool_ctx(self):
        from astra.core.context import ToolContext
        return ToolContext(store=self.store, config=self.config,
                           events=self.events, memory=self.memory,
                           plugins=self._plugins, tasks=self.tasks,
                           web3_manager=getattr(self, "web3_manager", None))

    # -- dependency support -----------------------------------------------------
    @staticmethod
    def _topo(steps: list) -> list[dict]:
        """Stable dependency order: any step listed in another's `depends_on`
        runs first. Unknown/missing deps are ignored, cycles never deadlock
        (unresolved steps fall to the end)."""
        by_id = {s["id"]: s for s in steps}
        ordered, done = [], set()
        pending = True
        while pending and len(ordered) < len(steps):
            pending = False
            for s in steps:
                if s["id"] in done:
                    continue
                deps = [d for d in (s.get("depends_on") or []) if d in by_id]
                if all(d in done for d in deps):
                    ordered.append(s)
                    done.add(s["id"])
                    pending = True
        return ordered

    @staticmethod
    def _resolve(step: dict, results: dict, params: dict) -> dict:
        """Fill {{<step_id>.<path>}} / {{path}} placeholders in step params
        from earlier step results (dependency data-flow)."""
        out = {}
        for k, v in (step.get("params") or {}).items():
            if isinstance(v, str) and "{{" in v:
                v = re.sub(r"\{\{\s*([\w.]+)\s*\}\}",
                           lambda m: str(Orchestrator._lookup(
                               m.group(1), results, params)), v)
            out[k] = v
        return dict(step, params=out)

    @staticmethod
    def _lookup(path: str, results: dict, params: dict):
        """Resolve a dotted path from an earlier step result. `{{w1.output.url}}`
        walks results['w1']['output']['url']; a path that skips the executor's
        'output' wrapper (`{{w1.url}}`) is auto-prefixed so both spellings work."""
        if path in params:
            return params[path]
        parts = path.split(".")
        if parts[0] not in results:
            return ""
        base = results[parts[0]]
        if not isinstance(base, dict):
            return ""
        candidates = (base, base.get("output"))
        for bundle in candidates:
            if not isinstance(bundle, dict):
                continue
            cur, ok = bundle, True
            for p in parts[1:]:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False
                    break
            if ok:
                return cur if cur is not None else ""
        return ""