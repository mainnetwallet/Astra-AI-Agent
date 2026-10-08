"""Reusable workflow engine.

A workflow is a list of steps, each naming a registered tool, with optional
dependencies and an optional `if` condition on earlier step results. Data
flows between steps: any step may reference `{{step_id.param}}` in its params.

Persistence: definitions and runs live in SQLite (`workflow_definitions`,
`workflow_runs`) so a run can be paused, resumed and audited later. Steps run
through the ToolRegistry, so every step is validated, gated and audited.

Identity rules (the contract the Agent Workflow UI depends on):

  * every definition has its own autoincrement `id`, which is the ONLY
    workflow identity; a name is a label, never a key,
  * `define()` always creates a NEW row and returns a fresh dict — it can
    never return, mutate or reuse another workflow's object,
  * `workflow_definitions.name` is UNIQUE, so a duplicate label is resolved
    deterministically ("Untitled Workflow" -> "Untitled Workflow 2") instead
    of raising an opaque sqlite IntegrityError (which used to surface as a
    500 and lose the newly created workflow entirely). A caller that wants
    the strict behaviour passes `uniquify=False` and gets
    `DuplicateNameError` — a structured, catchable error,
  * runs are scoped by `workflow_id`, so two workflows never share history.

Steps are validated before they are stored: a malformed step, a step naming
an unregistered tool, a dangling dependency or a dependency cycle is refused
at write time (`InvalidDefinition`) rather than silently skipped at run time.
(Dependency cycles used to be dropped silently by `_topo`, which meant a
workflow could "complete" while never running some of its steps.)
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from astra.core.exceptions import WorkflowError
from astra.core.state import WORKFLOW_STATUSES


class WorkflowNotFound(WorkflowError):
    """No definition (or run) with that id — maps to HTTP 404."""

    category = "WorkflowNotFound"


class InvalidDefinition(WorkflowError):
    """Malformed steps / unknown tool / dangling or cyclic dependency —
    maps to HTTP 400."""

    category = "InvalidDefinition"


class DuplicateNameError(WorkflowError):
    """The requested workflow name is already taken — maps to HTTP 409."""

    category = "DuplicateNameError"

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    steps       TEXT NOT NULL DEFAULT '[]',   -- JSON list
    layout      TEXT NOT NULL DEFAULT '{}',   -- JSON {step_id: {x, y}}
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT ''
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


def _as_id(value) -> int:
    """Coerce an incoming workflow/run id to int.

    A non-integer id is a client error, not something that should reach a
    query as a string and quietly match nothing."""
    if isinstance(value, bool):
        raise InvalidDefinition("id must be an integer")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidDefinition(f"id must be an integer, got {value!r}")


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
        # Self-heal databases created before `layout`/`updated_at` existed —
        # same idempotent mechanism the Store exposes for every subsystem.
        store.ensure_column("workflow_definitions", "layout",
                            "TEXT NOT NULL DEFAULT '{}'")
        store.ensure_column("workflow_definitions", "updated_at",
                            "TEXT DEFAULT ''")

    # -- step validation -----------------------------------------------------
    @staticmethod
    def normalize_steps(steps) -> list[dict]:
        """Coerce a client-supplied step list into the canonical shape, or
        raise `InvalidDefinition`.

        Missing step ids are filled in (``step1``, ``step2`` …) so every
        stored definition has stable, addressable step ids — the visual
        editor and the run history both key off them. Every failure is a
        named, catchable error: a silent bad definition is exactly how a
        workflow ends up "completing" without running all of its steps.
        """
        if steps is None:
            return []
        if not isinstance(steps, list):
            raise InvalidDefinition("steps must be a list")
        out: list[dict] = []
        used: set[str] = set()
        for i, raw in enumerate(steps, start=1):
            if not isinstance(raw, dict):
                raise InvalidDefinition(f"step {i} must be an object")
            step = dict(raw)
            sid = step.get("id")
            if sid is None or sid == "":
                sid = f"step{i}"
                n = 1
                while sid in used:
                    n += 1
                    sid = f"step{i}_{n}"
                step["id"] = sid
            if not isinstance(sid, str) or not sid.strip():
                raise InvalidDefinition(f"step {i}: id must be a non-empty string")
            if sid in used:
                raise InvalidDefinition(f"duplicate step id: '{sid}'")
            used.add(sid)

            tool = step.get("tool")
            if not isinstance(tool, str) or not tool.strip():
                raise InvalidDefinition(f"step '{sid}' needs a tool")
            step["tool"] = tool.strip()

            params = step.get("params")
            if params is None:
                step["params"] = {}
            elif not isinstance(params, dict):
                raise InvalidDefinition(f"step '{sid}': params must be an object")

            deps = step.get("depends_on") or []
            if isinstance(deps, str):
                deps = [deps]
            if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
                raise InvalidDefinition(
                    f"step '{sid}': depends_on must be a list of step ids")
            step["depends_on"] = list(dict.fromkeys(deps))

            cond = step.get("if")
            if cond is not None:
                if not isinstance(cond, dict) or not isinstance(cond.get("step"), str) \
                        or not cond.get("step"):
                    raise InvalidDefinition(
                        f"step '{sid}': if must be {{'step': <step id>, 'op': 'ok'}}")
                op = cond.get("op") or "ok"
                if op not in ("ok", "not_ok"):
                    raise InvalidDefinition(
                        f"step '{sid}': unknown condition op '{op}' "
                        f"(expected 'ok' or 'not_ok')")
                step["if"] = {"step": cond["step"], "op": op}

            if "name" in step and step["name"] is not None \
                    and not isinstance(step["name"], str):
                raise InvalidDefinition(f"step '{sid}': name must be a string")
            out.append(step)

        ids = {s["id"] for s in out}
        for s in out:
            for dep in s["depends_on"]:
                if dep == s["id"]:
                    raise InvalidDefinition(f"step '{s['id']}' depends on itself")
                if dep not in ids:
                    raise InvalidDefinition(
                        f"step '{s['id']}' depends on unknown step '{dep}'")
            cond = s.get("if")
            if cond and cond["step"] not in ids:
                raise InvalidDefinition(
                    f"step '{s['id']}' has a condition on unknown step "
                    f"'{cond['step']}'")
        WorkflowEngine.check_cycles(out)
        return out

    @staticmethod
    def check_cycles(steps: list[dict]) -> None:
        """Refuse dependency cycles.

        `_topo` can only ever emit steps whose dependencies are already
        satisfied, so a cycle used to be *silently dropped*: the run
        reported "completed" while those steps never executed at all.
        """
        ids = {s["id"] for s in steps}
        deps = {s["id"]: [d for d in (s.get("depends_on") or []) if d in ids]
                for s in steps}
        state: dict[str, int] = {}

        def visit(sid: str, path: list[str]) -> None:
            if state.get(sid) == 1:
                cycle = " -> ".join(path + [sid])
                raise InvalidDefinition(f"dependency cycle: {cycle}")
            if state.get(sid) == 2:
                return
            state[sid] = 1
            for dep in deps.get(sid, []):
                visit(dep, path + [sid])
            state[sid] = 2

        for sid in deps:
            visit(sid, [])

    def check_tools(self, steps: list[dict], registry=None) -> None:
        """Every step must name a tool that is actually registered."""
        reg = registry if registry is not None else self.registry
        if reg is None:
            return
        for s in steps:
            try:
                known = reg.get(s["tool"]) is not None
            except Exception:
                known = True
            if not known:
                raise InvalidDefinition(
                    f"step '{s['id']}' names an unknown tool '{s['tool']}'")

    # -- definitions ---------------------------------------------------------
    @staticmethod
    def _decode(d: dict | None) -> dict | None:
        if not d:
            return None
        d["steps"] = json.loads(d["steps"] or "[]")
        raw_layout = d.get("layout")
        try:
            d["layout"] = json.loads(raw_layout or "{}") if isinstance(raw_layout, str) \
                else (raw_layout or {})
        except Exception:
            d["layout"] = {}
        if not isinstance(d["layout"], dict):
            d["layout"] = {}
        return d

    def _name_taken(self, name: str, exclude_id: int | None = None) -> bool:
        if exclude_id:
            row = self.store.fetchone(
                "SELECT id FROM workflow_definitions WHERE name = ? AND id != ?",
                (name, exclude_id))
        else:
            row = self.store.fetchone(
                "SELECT id FROM workflow_definitions WHERE name = ?", (name,))
        return row is not None

    def _unique_name(self, base: str, exclude_id: int | None = None) -> str:
        """`base`, or the first free "base 2", "base 3", … — deterministic,
        so creating a second "Untitled Workflow" always yields
        "Untitled Workflow 2" instead of a constraint violation."""
        if not self._name_taken(base, exclude_id):
            return base
        n = 2
        while self._name_taken(f"{base} {n}", exclude_id):
            n += 1
        return f"{base} {n}"

    def define(self, name: str, description: str = "", steps: list | None = None,
               *, layout: dict | None = None, uniquify: bool = True,
               registry=None) -> dict:
        """Create a NEW workflow and return it.

        Never updates, returns or touches an existing workflow: the row is
        always a fresh insert, so the caller gets a brand-new id, a new
        steps list and no run history.
        """
        name = str(name or "").strip()
        if not name:
            raise InvalidDefinition("workflow name required")
        norm = self.normalize_steps(steps)
        self.check_tools(norm, registry)
        if not uniquify and self._name_taken(name):
            raise DuplicateNameError(
                f"a workflow named '{name}' already exists")
        final = self._unique_name(name) if uniquify else name
        wf_id = self.store.insert(
            "workflow_definitions", name=final, description=description or "",
            steps=json.dumps(norm, ensure_ascii=False),
            layout=json.dumps(layout or {}, ensure_ascii=False), enabled=1,
            created_at=_now(), updated_at=_now())
        return self.get_definition(wf_id)

    def get_definition(self, wf_id) -> dict | None:
        return self._decode(self.store.fetchone(
            "SELECT * FROM workflow_definitions WHERE id = ?", (_as_id(wf_id),)))

    def find_definition(self, name: str) -> dict | None:
        return self._decode(self.store.fetchone(
            "SELECT * FROM workflow_definitions WHERE lower(name) = lower(?)",
            (name,)))

    def list_definitions(self, enabled_only: bool = False) -> list[dict]:
        rows = self.store.fetch(
            "SELECT * FROM workflow_definitions ORDER BY id" +
            (" WHERE enabled = 1" if enabled_only else ""))
        return [self._decode(d) for d in rows]

    def list_definitions_with_stats(self, enabled_only: bool = False) -> list[dict]:
        """Definitions plus real run metadata (count + latest run) so the
        workflow list can show last-run status without N follow-up calls."""
        defs = self.list_definitions(enabled_only)
        latest: dict[int, dict] = {}
        counts: dict[int, int] = {}
        for r in self.store.fetch(
                "SELECT id, workflow_id, status, started_at, completed_at, error "
                "FROM workflow_runs ORDER BY id DESC"):
            wid = r["workflow_id"]
            counts[wid] = counts.get(wid, 0) + 1
            latest.setdefault(wid, r)
        for d in defs:
            d["run_count"] = counts.get(d["id"], 0)
            last = latest.get(d["id"])
            d["last_run"] = None if not last else {
                "id": last["id"], "status": last["status"],
                "started_at": last["started_at"],
                "completed_at": last["completed_at"],
                "error": last["error"]}
        return defs

    def update_definition(self, wf_id, *, name: str | None = None,
                          description: str | None = None,
                          steps: list | None = None,
                          layout: dict | None = None,
                          enabled: bool | None = None,
                          uniquify: bool = False,
                          registry=None) -> dict:
        """Update ONE existing workflow (never creates one).

        `uniquify=False` by default: renaming onto another workflow's name is
        a client mistake and gets `DuplicateNameError` (HTTP 409) rather than
        silently renaming to "name 2".
        """
        wid = _as_id(wf_id)
        current = self.get_definition(wid)
        if current is None:
            raise WorkflowNotFound(f"workflow {wf_id} not found")
        fields: dict = {"updated_at": _now()}
        if name is not None:
            new_name = str(name).strip()
            if not new_name:
                raise InvalidDefinition("workflow name required")
            if new_name != current["name"]:
                if self._name_taken(new_name, exclude_id=wid):
                    if not uniquify:
                        raise DuplicateNameError(
                            f"a workflow named '{new_name}' already exists")
                    new_name = self._unique_name(new_name, exclude_id=wid)
                fields["name"] = new_name
        if description is not None:
            fields["description"] = str(description)
        if steps is not None:
            norm = self.normalize_steps(steps)
            self.check_tools(norm, registry)
            fields["steps"] = json.dumps(norm, ensure_ascii=False)
        if layout is not None:
            fields["layout"] = json.dumps(layout or {}, ensure_ascii=False)
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.store.exec(
            f"UPDATE workflow_definitions SET {sets} WHERE id = ?",
            tuple(fields.values()) + (wid,))
        return self.get_definition(wid)

    def delete_definition(self, wf_id) -> bool:
        """Delete a definition (its runs cascade). Raises if it is missing —
        a DELETE for a non-existent workflow is a 404, not a silent no-op."""
        wid = _as_id(wf_id)
        if self.get_definition(wid) is None:
            raise WorkflowNotFound(f"workflow {wf_id} not found")
        self.store.exec("DELETE FROM workflow_definitions WHERE id = ?", (wid,))
        return True

    # -- runs ----------------------------------------------------------------
    def create_run(self, wf_id, params: dict | None = None) -> dict:
        wid = _as_id(wf_id)
        d = self.get_definition(wid)
        if not d:
            raise WorkflowNotFound(f"workflow {wf_id} not found")
        rid = self.store.insert(
            "workflow_runs", workflow_id=wid, name=d["name"], status="created",
            current_step="", params=json.dumps(params or {}, ensure_ascii=False),
            results="{}", error="", started_at=_now(), completed_at="")
        run = self.get_run(rid)
        if self.events:
            self.events.emit("workflow.started", agent="workflows",
                             workflow=run["name"], run_id=run["id"],
                             op=f"wf:{run['id']}")
        return run

    def get_run(self, run_id) -> dict | None:
        r = self.store.fetchone("SELECT * FROM workflow_runs WHERE id = ?",
                                (_as_id(run_id),))
        if r:
            r["params"] = json.loads(r["params"] or "{}")
            r["results"] = json.loads(r["results"] or "{}")
        return r

    def list_runs(self, limit: int = 50, workflow_id=None) -> list[dict]:
        """Run history, newest first. `workflow_id` scopes it to ONE
        workflow — two workflows never see each other's runs."""
        if workflow_id is None:
            rows = self.store.fetch(
                "SELECT * FROM workflow_runs ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.store.fetch(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? "
                "ORDER BY id DESC LIMIT ?", (_as_id(workflow_id), limit))
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
            r["results"] = json.loads(r["results"] or "{}")
        return rows

    def latest_run(self, workflow_id) -> dict | None:
        runs = self.list_runs(limit=1, workflow_id=workflow_id)
        return runs[0] if runs else None

    def cancel_run(self, run_id) -> dict | None:
        """Request cancellation of a run.

        The engine re-reads the run status before every step, so this stops
        a run between steps (a step already executing finishes — there is no
        thread to kill, and pretending otherwise would be a lie)."""
        rid = _as_id(run_id)
        run = self.get_run(rid)
        if run is None:
            raise WorkflowNotFound(f"run {run_id} not found")
        if run["status"] in ("completed", "failed", "cancelled"):
            return run
        return self.set_status(rid, "cancelled")

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
        # tolerate steps without explicit ids (s1, s2, …): every step gets
        # one, not just when the FIRST one happens to be missing — a single
        # un-id'd step used to reach `_topo` and raise KeyError.
        if steps:
            steps = [dict(s) for s in steps if isinstance(s, dict)]
            used = {s["id"] for s in steps if isinstance(s.get("id"), str)}
            for i, s in enumerate(steps, start=1):
                if not isinstance(s.get("id"), str):
                    sid = f"step{i}"
                    n = 1
                    while sid in used:
                        n += 1
                        sid = f"step{i}_{n}"
                    s["id"] = sid
                    used.add(sid)
        base_params = run.get("params") or {}

        live = (self.get_run(run["id"]) or {}).get("status")
        if live == "cancelled":
            # cancelled while queued, before the first step started
            return self.get_run(run["id"])
        self.set_status(run["id"], "running")
        results = {}
        done = set()
        ordered = self._topo(steps)
        if len(ordered) < len(steps):
            # _topo can only emit steps whose dependencies are satisfied; a
            # short result means the definition contains a cycle. Refusing
            # beats silently reporting "completed" with steps never run.
            missing = sorted({s.get("id") for s in steps} - {s.get("id") for s in ordered})
            raise InvalidDefinition(
                f"dependency cycle among steps: {', '.join(str(m) for m in missing)}")
        for step in ordered:
            if (self.get_run(run["id"]) or {}).get("status") in ("paused", "cancelled"):
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
        # Run outcome: a workflow whose step errored is NOT "completed" —
        # reporting success there is worse than reporting nothing. Skipped
        # steps (a false condition) are a normal, successful outcome.
        live = (self.get_run(run["id"]) or {}).get("status")
        failed = [sid for sid, res in results.items()
                  if isinstance(res, dict) and (res.get("error")
                                                or res.get("blocked")
                                                or res.get("ok") is False)]
        if live == "paused":
            status = "paused"
        elif live == "cancelled":
            status = "cancelled"
        elif failed:
            status = "failed"
        else:
            status = "completed"
        error = ""
        if status == "failed":
            first = failed[0]
            info = results.get(first) or {}
            error = f"{first}: {info.get('error') or info.get('reason') or 'step failed'}"
        finished = _now() if status in ("completed", "failed", "cancelled") else ""
        self.store.exec(
            "UPDATE workflow_runs SET results = ?, status = ?, error = ?, "
            "completed_at = ? WHERE id = ?",
            (json.dumps(results, ensure_ascii=False), status, error[:500],
             finished, run["id"]))
        if self.events and status in ("completed", "failed"):
            self.events.emit(
                "workflow.completed" if status == "completed" else "workflow.failed",
                agent="workflows", workflow=run.get("name") or "",
                run_id=run["id"], op=f"wf:{run['id']}", terminal=True,
                error=error)
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
        """Evaluate a step's `if`. Unknown/absent ops are permissive (they
        were before this existed, and a definition written by hand must not
        start skipping steps)."""
        step = results.get(cond.get("step"))
        if not isinstance(step, dict):
            step = {}
        op = cond.get("op") or "ok"
        if op == "ok":
            return bool(step.get("ok"))
        if op == "not_ok":
            return not bool(step.get("ok"))
        return True
