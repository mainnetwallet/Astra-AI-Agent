"""Reusable workflow engine.

A workflow is a list of steps, each naming a registered tool, with optional
dependencies and an optional `if` condition on earlier step results. Data
flows between steps: any step may reference `{{step_id.param}}` in its params.

Persistence: definitions and runs live in SQLite (`workflow_definitions`,
`workflow_runs`) so a run can be paused, resumed and audited later. Steps run
through the ToolRegistry, so every step is validated, gated and audited.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from astra.core.state import WORKFLOW_STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    steps       TEXT NOT NULL DEFAULT '[]',   -- JSON list
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id  INTEGER NOT NULL REFERENCES workflow_definitions(id)
                 ON DELETE CASCADE,
    name         TEXT DEFAULT '',
    status       TEXT DEFAULT 'created',
    current_step TEXT DEFAULT '',
    params       TEXT DEFAULT '{}',
    results      TEXT DEFAULT '{}',
    error        TEXT DEFAULT '',
    started_at   TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class WorkflowEngine:
    def __init__(self, store, registry, events=None, context=None):
        self.store = store
        self.registry = registry
        self.events = events
        # Optional ToolContext handed to every step. Without it a
        # context-dependent tool (remember/recall/create_task/…) raised
        # AttributeError on `ctx.store` and the step silently recorded an
        # error; the built-in tools that need shared state get it from here.
        self.context = context
        if not store.table_exists("workflow_definitions"):
            store.install(SCHEMA)

    # -- definitions ---------------------------------------------------------
    def define(self, name: str, description: str = "", steps: list | None = None) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("workflow name required")
        steps = steps or []
        wf_id = self.store.insert(
            "workflow_definitions", name=name, description=description,
            steps=json.dumps(steps, ensure_ascii=False), enabled=1,
            created_at=_now())
        return self.get_definition(wf_id)

    def get_definition(self, wf_id: int) -> dict | None:
        d = self.store.fetchone("SELECT * FROM workflow_definitions WHERE id = ?",
                                (wf_id,))
        if d:
            d["steps"] = json.loads(d["steps"] or "[]")
        return d

    def find_definition(self, name: str) -> dict | None:
        d = self.store.fetchone(
            "SELECT * FROM workflow_definitions WHERE lower(name) = lower(?)", (name,))
        if d:
            d["steps"] = json.loads(d["steps"] or "[]")
        return d

    def list_definitions(self, enabled_only: bool = False) -> list[dict]:
        rows = self.store.fetch(
            "SELECT * FROM workflow_definitions ORDER BY id" +
            (" WHERE enabled = 1" if enabled_only else ""))
        for d in rows:
            d["steps"] = json.loads(d["steps"] or "[]")
        return rows

    def delete_definition(self, wf_id: int) -> None:
        self.store.exec("DELETE FROM workflow_definitions WHERE id = ?", (wf_id,))

    # -- runs ----------------------------------------------------------------
    def create_run(self, wf_id: int, params: dict | None = None) -> dict:
        d = self.get_definition(wf_id)
        if not d:
            raise ValueError("workflow not found")
        rid = self.store.insert(
            "workflow_runs", workflow_id=wf_id, name=d["name"], status="created",
            current_step="", params=json.dumps(params or {}, ensure_ascii=False),
            results="{}", error="", started_at=_now(), completed_at="")
        run = self.get_run(rid)
        if self.events:
            self.events.emit("workflow.started", agent="workflows",
                             workflow=run["name"], run_id=run["id"],
                             op=f"wf:{run['id']}")
        return run

    def get_run(self, run_id: int) -> dict | None:
        r = self.store.fetchone("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if r:
            r["params"] = json.loads(r["params"] or "{}")
            r["results"] = json.loads(r["results"] or "{}")
        return r

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.store.fetch(
            "SELECT * FROM workflow_runs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
            r["results"] = json.loads(r["results"] or "{}")
        return rows

    def set_status(self, run_id: int, status: str, error: str = "") -> dict | None:
        if status not in WORKFLOW_STATUSES:
            raise ValueError(f"bad workflow status: {status}")
        now = _now()
        done = {"completed", "failed", "cancelled"}
        self.store.exec(
            "UPDATE workflow_runs SET status = ?, error = ?, completed_at = ? WHERE id = ?",
            (status, error, now if status in done else "", run_id))
        return self.get_run(run_id)

    # -- execution -----------------------------------------------------------
    def run(self, workflow_id: int | None = None, name: str | None = None,
            params: dict | None = None, steps: list | None = None,
            run_id: int | None = None) -> dict:
        """Execute a workflow. Returns the finished run dict.

        If `steps` is provided and no definition exists, run ad-hoc (used by
        scheduler deadline workflows and tests)."""
        if run_id is None:
            if workflow_id:
                run = self.create_run(workflow_id, params)
            elif name:
                d = self.find_definition(name)
                run = self.create_run(d["id"], params) if d else None
            else:
                run = None
        else:
            run = self.get_run(run_id)
        if run is None:
            if steps is None:
                raise ValueError("no workflow found")
            rid = self.store.insert(
                "workflow_runs", workflow_id=0, name=name or "adhoc", status="created",
                current_step="", params=json.dumps(params or {}),
                results="{}", error="", started_at=_now(), completed_at="")
            run = self.get_run(rid)
        if steps is None:
            steps = (self.get_definition(run.get("workflow_id")) or {}).get("steps", [])
        # tolerate steps without explicit ids: s1, s2, …
        if steps and isinstance(steps[0], dict) and not isinstance(steps[0].get("id"), str):
            steps = [dict(s) for s in steps]
            for i, s in enumerate(steps, start=1):
                if not isinstance(s.get("id"), str):
                    s["id"] = f"step{i}"
        base_params = run.get("params") or {}

        self.set_status(run["id"], "running")
        results = {}
        done = set()
        for step in self._topo(steps):
            if (self.get_run(run["id"]) or {}).get("status") == "paused":
                break
            sid = step["id"]
            # resolve {{...}} references from earlier results
            call_params, _ = self._resolve(step, results, base_params)
            if step.get("if") and not self._condition(step["if"], results):
                results[sid] = {"skipped": True, "reason": "condition false"}
                done.add(sid)
                continue
            self.store.exec("UPDATE workflow_runs SET current_step = ? WHERE id = ?",
                            (sid, run["id"]))
            step_op = f"wf:{run['id']}:{sid}"
            step_ok, step_error = True, ""
            if self.events:
                self.events.emit("task.started", agent="workflows",
                                 workflow=run.get("name"), step=sid,
                                 run_id=run["id"], op=step_op)
            try:
                out = self.registry.execute(step["tool"], call_params,
                                            ctx=self.context,
                                            allow_confirmation=False,
                                            trace=f"wf:{run['id']}")
                if out.get("decision") == "ask":
                    results[sid] = {"blocked": True, "reason": out.get("reason")}
                    step_ok = False
                    step_error = str(out.get("reason") or "blocked")
                else:
                    results[sid] = {"ok": out.get("ok", False),
                                    "output": out.get("result")}
                    step_ok = bool(out.get("ok", False))
                    if not step_ok:
                        step_error = str(out.get("reason") or "step did not complete")
            except Exception as e:
                results[sid] = {"ok": False,
                                "error": f"{type(e).__name__}: {e}"}
                step_ok = False
                step_error = f"{type(e).__name__}: {e}"
            if self.events:
                # terminal counterpart of task.started (same op) so the step
                # row resolves instead of staying "running" forever.
                self.events.emit(
                    "task.completed" if step_ok else "task.failed",
                    agent="workflows", workflow=run.get("name"), step=sid,
                    run_id=run["id"], op=step_op, terminal=True,
                    error=step_error)
            done.add(sid)
        status = "paused" if (self.get_run(run["id"]) or {}).get("status") == "paused" else "completed"
        self.store.exec(
            "UPDATE workflow_runs SET results = ?, status = ?, completed_at = ? "
            "WHERE id = ?",
            (json.dumps(results, ensure_ascii=False), status,
             _now() if status == "completed" else "", run["id"]))
        if self.events and status == "completed":
            self.events.emit("workflow.completed", agent="workflows",
                             workflow=run.get("name") or run.get("name") or "",
                             run_id=run["id"], op=f"wf:{run['id']}",
                             terminal=True)
        return self.get_run(run["id"])

    # -- step helpers --------------------------------------------------------
    @staticmethod
    def _topo(steps: list) -> list[dict]:
        """Stable topological-ish order that keeps dependencies first."""
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

    def _resolve(self, step: dict, results: dict, params: dict) -> tuple[dict, bool]:
        """Fill {{x}} placeholders from run params and earlier step results."""
        out = {}
        for k, v in (step.get("params") or {}).items():
            if isinstance(v, str) and "{{" in v:
                v = re.sub(r"\{\{\s*([\w.]+)\s*\}\}",
                           lambda m: str(self._lookup(m.group(1), results, params)), v)
            out[k] = v
        return out, True

    @staticmethod
    def _lookup(path: str, results: dict, params: dict):
        parts = path.split(".")
        if parts[0] in results:
            cur = results[parts[0]]
            for p in parts[1:]:
                cur = cur.get(p, "") if isinstance(cur, dict) else ""
        elif parts[0] in params:
            cur = params[parts[0]]
        else:
            cur = ""
        return cur

    @staticmethod
    def _condition(cond: dict, results: dict) -> bool:
        step = results.get(cond.get("step"), {})
        return bool(step.get("ok")) if cond.get("op") == "ok" else True
