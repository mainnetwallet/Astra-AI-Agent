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
    conversation_context TEXT DEFAULT '',
    selected_agent TEXT DEFAULT '',
    selected_provider TEXT DEFAULT '',
    selected_model TEXT DEFAULT '',
    gateway_verification_status TEXT DEFAULT '',
    gateway_verification_reason TEXT DEFAULT '',
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
        ("conversation_context", "TEXT DEFAULT ''"),
        ("gateway_verification_status", "TEXT DEFAULT ''"),
        ("gateway_verification_reason", "TEXT DEFAULT ''"),
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
    def submit(self, goal: str, sync: bool = False, context: str = "",
               attachments: list | None = None) -> dict:
        """Start (or directly run) an execution. Returns control record.

        `context`, when the caller has it (e.g. the client's own recent
        chat history), is recent Assistant conversation relevant to `goal`.
        `attachments`, when present, is a list of processed attachment dicts
        from the multimodal upload layer — stored in memory per execution
        and forwarded to the Planner for capability-aware routing.
        """
        eid = "exec-" + uuid.uuid4().hex[:8]
        self.store.insert("astra_executions", execution_id=eid, goal=goal,
                          status="IDLE", plan="[]", results="{}",
                          pending_step="", error="",
                          conversation_context=context or "",
                          created_at=_now(), started_at="", completed_at="")
        if attachments:
            self._attachments = getattr(self, "_attachments", {})
            self._attachments[eid] = attachments
        if self.events:
            self.events.emit("agent.started", agent="orchestrator",
                             execution=eid, goal=goal,
                             attachments=len(attachments or []))
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
        convo_context = row.get("conversation_context") or ""
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

        attachments = getattr(self, "_attachments", {}).get(execution_id)

        if replan:
            if self.events:
                self.events.emit("agent.planning", agent="orchestrator",
                                 execution=execution_id, goal=goal,
                                 replan=True, error=replan_for)
            plan = self.planner.plan(goal, ctx={"replan_for": replan_for,
                                                "conversation_context": convo_context,
                                                "attachments": attachments})
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
            plan = self.planner.plan(goal, ctx={"conversation_context": convo_context,
                                                "attachments": attachments})
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
        gv_status, gv_reason = "", ""
        if final == "COMPLETED":
            # Gap 2: automatic Executor evidence -> Astra AI Gateway final
            # task-completion verification. Only meaningful for a plan
            # that actually executed a real (non-"answer") tool step —
            # see _gateway_final_task_verification's docstring.
            results, gv_status, gv_reason = self._gateway_final_task_verification(
                execution_id, row, goal, plan, results, convo_context)
            self._store_plan(execution_id, plan)   # corrective steps may have been appended
            if gv_status == "FAILED":
                # a genuine failure surfaced DURING final verification's
                # own correction attempts (not a step failure above) is a
                # real execution failure, not merely "unconfirmed" (§7).
                final = "FAILED"
                report_error = report_error or (
                    "gateway final verification failed: " + gv_reason)
        self._set(row, final, completed=final in STATES_COMPLETE,
                  error=report_error, pending_step="")
        self._store_results(execution_id, results, report_error)
        if gv_status:
            self.store.exec(
                "UPDATE astra_executions SET gateway_verification_status = ?, "
                "gateway_verification_reason = ? WHERE execution_id = ?",
                (gv_status, gv_reason[:500], execution_id))
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
                "gateway_verification_status": row.get("gateway_verification_status", ""),
                "gateway_verification_reason": row.get("gateway_verification_reason", ""),
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

    # -- Gap 2: automatic Executor evidence -> Gateway final task-
    # completion verification -------------------------------------------
    # Distinct from Planner's pre-execution contract (which only checks
    # the PLAN's JSON shape, before any tool has run): this runs AFTER
    # the Executor has actually executed every step, and verifies the
    # EXECUTED WORK — via evidence gathered from real step results, never
    # invented — against the original goal, using the same
    # GatewayTaskCompletionSupervisor contract machinery Planner already
    # uses. See astra/ai/gateway_task_completion.py.
    def _collect_execution_evidence(self, results: dict) -> dict:
        """Evidence built entirely from what the Executor actually did —
        never invented, never asked of the AI. Keys are only ever present
        when the corresponding thing genuinely happened."""
        ok_steps = sorted(sid for sid, r in results.items() if r.get("ok"))
        failed = {sid: (r.get("error") or "")[:200]
                 for sid, r in results.items() if not r.get("ok")}
        evidence: dict = {}
        if ok_steps:
            evidence["steps_completed"] = ",".join(ok_steps)
        if failed:
            evidence["step_failures"] = json.dumps(failed, ensure_ascii=False)[:1000]
        outputs = {sid: r.get("output") for sid, r in results.items() if r.get("ok")}
        if outputs:
            evidence["step_outputs"] = json.dumps(outputs, ensure_ascii=False,
                                                   default=str)[:2000]
        return evidence

    @staticmethod
    def _execution_summary_text(goal: str, plan: list, results: dict) -> str:
        """A plain-text description of what was executed — stands in for
        "the AI's claim of completion" that `verify_task_completion`
        expects as `result.text`. Built only from real plan/results data,
        never from an extra AI call (that would defeat the point of
        evidence-based verification)."""
        lines = [f"Goal: {goal}"]
        for step in plan:
            sid = step.get("id", "")
            r = results.get(sid) or {}
            state = "ok" if r.get("ok") else ("failed: " + str(r.get("error") or ""))
            lines.append(f"- [{step.get('tool','?')}] {step.get('description','')} -> {state}")
        return "\n".join(lines)

    def _gateway_final_task_verification(self, execution_id: str, row,
                                         goal: str, plan: list, results: dict,
                                         convo_context: str
                                         ) -> tuple[dict, str, str]:
        """Returns `(results, status, reason)`. `status` is one of the
        Gateway's COMPLETE/INCOMPLETE/FAILED/UNCERTAIN taxonomy, or
        "UNVERIFIED" (Gateway control layer missing — strict, never
        silently treated as COMPLETE), or "" (not applicable: the plan
        was pure conversation, nothing execution-side to verify).

        Only fires for a plan with at least one real (non-"answer") tool
        step — a plain answer has nothing beyond what Planner's own
        pre-execution contract already checked (see planner.py). Never
        replays a step that already succeeded (§ spec item 14): the
        bounded correction loop below only ever executes NEW step ids a
        correction round produced.
        """
        from astra.ai.gateway_contract import ProviderExecutionResult
        from astra.ai.gateway_task_completion import (
            COMPLETE, FAILED, build_task_completion_contract,
            build_task_completion_messages, verify_task_completion)
        from astra.ai.router import RoutingRequest
        from astra.core.correction import MAX_CORRECTION_ATTEMPTS
        from astra.core.exceptions import ProviderError

        if not any(s.get("tool") != "answer" for s in plan):
            return results, "", ""   # pure conversation: nothing to verify

        gateway = getattr(self.router, "gateway", None) if self.router else None
        if gateway is None or not hasattr(gateway, "supervise_task"):
            # Strict mandatory-Gateway enforcement (Gap 1): a tool-
            # executing task must not be silently reported as verified
            # when there is, in fact, no Gateway control layer attached.
            reason = ("Astra AI Gateway is not attached — task-completion "
                      "verification could not run for this execution")
            self._emit_gv("gateway.final_verification_unavailable",
                         execution_id, reason=reason)
            return results, "UNVERIFIED", reason

        def _semantic_verifier(_contract, _result, ev):
            # Deterministic — no extra AI call needed for this check: a
            # step that genuinely failed means the goal was not actually
            # completed, whatever the plan's own "answer" text claimed.
            if ev.get("step_failures"):
                return ("INCOMPLETE",
                        "one or more executed steps failed: " + ev["step_failures"])
            return (COMPLETE, "")

        def _evidence_for(current_plan: list) -> dict:
            # Scoped to the CURRENT plan's step ids only: a stale failure
            # from an earlier, already-superseded replan attempt (a
            # step id no longer part of this plan at all) must not block
            # completion forever once the current plan's own steps all
            # genuinely succeed — only THIS plan's evidence is what the
            # goal is actually being held to right now.
            plan_ids = {s["id"] for s in current_plan}
            return self._collect_execution_evidence(
                {sid: r for sid, r in results.items() if sid in plan_ids})

        evidence = _evidence_for(plan)
        contract = build_task_completion_contract(
            user_request=goal, goal=goal,
            evidence_required=("steps_completed",), require_semantic=True)
        result = ProviderExecutionResult(
            ok=True, text=self._execution_summary_text(goal, plan, results))
        outcome = verify_task_completion(contract, result, evidence,
                                         _semantic_verifier)

        messages = [{"role": "user", "content": goal}]
        attempts = 0
        while outcome.correctable and attempts < MAX_CORRECTION_ATTEMPTS:
            if not (self.router and self.planner and
                    hasattr(self.router, "route_request")):
                break   # no Existing-Provider path available to correct through
            attempts += 1
            self._emit_gv("gateway.final_verification_correction_requested",
                         execution_id, attempt=attempts, status=outcome.status,
                         reason=outcome.reason)
            messages = build_task_completion_messages(
                messages, result, contract, outcome)
            try:
                # §6/§10 in spirit: goes back through the Existing Provider
                # System (the router's own selection/failover), asking it
                # to continue from exactly what's missing — never a blind
                # "try again". Zero-bypass: uses `route_request()` (never
                # the legacy `.route()` tuple call). No `task_contract` is
                # attached here — this call is already inside the outer
                # Gateway final-task-completion correction loop (this
                # whole method only runs with a Gateway attached, see the
                # UNVERIFIED check above, and the raw JSON reply is
                # re-verified by `verify_task_completion` right below);
                # attaching a second, inner contract here would stack a
                # second Gateway supervision/correction loop underneath
                # this one for every outer attempt, needlessly multiplying
                # provider calls per bounded attempt instead of keeping
                # correction bounded at exactly MAX_CORRECTION_ATTEMPTS
                # provider calls. A real provider/network failure here is
                # a FAILED verification outcome, distinct from INCOMPLETE
                # (§7: recovery/failover concerns stay separate from
                # content correction), and stops the loop immediately.
                rr = self.router.route_request(RoutingRequest(messages=messages))
                if not rr.ok:
                    raise ProviderError(rr.error or "correction routing failed")
                text = rr.text
            except Exception as e:
                outcome_status, outcome_reason = FAILED, str(e)
                self._emit_gv("gateway.final_verification_correction_failed",
                             execution_id, attempt=attempts, error=str(e))
                return results, outcome_status, outcome_reason
            if not text:
                break
            new_steps = self.planner.parse_plan_json(
                self._safe_json(text), max_steps=6)
            if new_steps:
                # Never replay an already-succeeded step: Planner always
                # numbers a freshly-parsed plan starting at "s1", so a
                # correction round's steps routinely collide by NAME with
                # the current plan's ids even though they are logically
                # new work — renumber against every id already in the
                # current plan (successful or not) before executing, so
                # a real collision with an already-succeeded step can
                # never cause it to be silently dropped OR silently
                # re-run under its old id.
                existing_ids = {s["id"] for s in plan}
                new_steps = self._renumber_new_steps(new_steps, existing_ids)
                plan.extend(new_steps)
                executed_ids = []
                for step in self._topo(new_steps):
                    prev = results.get(step["id"])
                    if prev and prev.get("ok"):
                        continue   # never blindly replay a successful side effect
                    self._record_step(execution_id, step, "running")
                    frozen = self._resolve(step, results, {})
                    out = self.executor.execute(frozen, self._tool_ctx(),
                                                self._scratch_run_ctx(execution_id, goal, plan))
                    results[step["id"]] = out
                    executed_ids.append(step["id"])
                    self._record_step(execution_id, step,
                                      "ok" if out.get("ok") else "failed", result=out)
                # Evidence for THIS round's re-verification is scoped to
                # what the correction round itself just did, not the
                # whole history: the correction instruction explicitly
                # tells the Provider "continue from where this left off;
                # do not repeat already-completed work" (see
                # build_task_correction_instruction), so what matters now
                # is whether ITS continuation succeeded — an earlier,
                # already-superseded failed attempt must not keep
                # blocking completion forever once the goal's remaining
                # work is genuinely done.
                evidence = self._collect_execution_evidence(
                    {sid: results[sid] for sid in executed_ids if sid in results})
            result = ProviderExecutionResult(
                ok=True, text=self._execution_summary_text(goal, plan, results))
            outcome = verify_task_completion(contract, result, evidence,
                                             _semantic_verifier)
            if outcome.ok:
                self._emit_gv("gateway.final_verification_correction_succeeded",
                             execution_id, attempt=attempts)

        if not outcome.ok and attempts:
            self._emit_gv("gateway.final_verification_correction_exhausted",
                         execution_id, attempts=attempts, status=outcome.status,
                         reason=outcome.reason)
        # §8-equivalent final gate: never upgrade the status beyond what
        # the last verification actually said.
        return results, outcome.status, outcome.reason

    @staticmethod
    def _safe_json(text: str) -> dict:
        # Lenient on purpose (see astra/ai/json_extract.py): a plain
        # .strip("`") only trims backtick characters off the ends and
        # still fails on a ```json fence or any surrounding prose, which
        # was making a good corrective AI reply look empty/unusable here.
        from astra.ai.json_extract import loads_lenient
        try:
            return loads_lenient(text)
        except Exception:
            return {}

    @staticmethod
    def _renumber_new_steps(new_steps: list, existing_ids: set) -> list:
        """A corrective round's steps are freshly numbered s1, s2, ... by
        Planner.parse_plan_json, which can collide with the original
        plan's ids. Renumber (and fix up depends_on) so they never
        overwrite an already-recorded result."""
        remap = {}
        out = []
        for i, s in enumerate(new_steps):
            new_id = s["id"]
            n = 0
            while new_id in existing_ids or new_id in remap.values():
                n += 1
                new_id = f"{s['id']}-fix{n}"
            remap[s["id"]] = new_id
            s = dict(s, id=new_id,
                     depends_on=[remap.get(d, d) for d in (s.get("depends_on") or [])])
            out.append(s)
        return out

    def _scratch_run_ctx(self, execution_id: str, goal: str, plan: list):
        rc = ExecutionContext(execution_id, goal)
        rc.steps = plan
        return rc

    def _emit_gv(self, kind: str, execution_id: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="orchestrator.gateway_verification",
                                 execution=execution_id, **data)
            except Exception:
                pass

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