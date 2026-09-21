"""Scheduler: run workflows on a schedule.

Kinds:
  oneshot   — value = ISO datetime ("2026-10-01 09:00")
  interval  — value = seconds
  daily     — value = "HH:MM"
  weekly    — value = "Mon 09:00" (Mon/Tue/…)
  deadline  — value = days-window; runs the workflow when a tracked deadline
              (from a deadline_callback) falls within that window and has not
              been handled yet (deduplicated via params.last_handled).

A daemon tick thread wakes every few seconds and fires due schedules. Every
fire is an event (scheduler.tick) and a workflow run, auditable in the
dashboard. No third-party cron daemon needed; cores are pure functions so
tests can inject a clock.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from astra.core.state import SCHEDULE_KINDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK(kind IN ('oneshot','interval','daily','weekly','deadline')),
    value        TEXT NOT NULL DEFAULT '',
    enabled      INTEGER DEFAULT 1,
    workflow_id  INTEGER DEFAULT 0,
    params       TEXT DEFAULT '{}',
    last_run     TEXT DEFAULT '',
    next_run     TEXT DEFAULT '',
    created_at   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS astra_sched_seen (
    k     TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_weekly(value: str):
    """'Mon 09:00' -> (weekday 0-6, hour, minute)."""
    import re as _re
    m = _re.match(r"^\s*([a-z]{3,9})\s+(\d{1,2}):(\d{2})\s*$", value, _re.I)
    if not m:
        return None
    days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    wd = days.get(m.group(1).lower()[:3])
    if wd is None:
        return None
    return wd, int(m.group(2)), int(m.group(3))


def compute_next_run(kind: str, value: str, after: datetime | None = None,
                     now: datetime | None = None,
                     deadline_due: bool = False) -> str | None:
    """Earliest next run time as ISO string, or None if never (disabled
    deadline when nothing is due). Pure — testable with injected clocks."""
    now = now or (after or datetime.now())
    if kind == "oneshot":
        try:
            d = datetime.strptime(value, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return d.strftime("%Y-%m-%d %H:%M") if d > now else None
    if kind == "interval":
        secs = max(1, int(value))
        if after:
            return (after + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
        return (now + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
    if kind == "daily":
        hh, _, mm = value.partition(":")
        try:
            t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            return None
        if t <= now:
            t += timedelta(days=1)
        return t.strftime("%Y-%m-%d %H:%M:%S")
    if kind == "weekly":
        got = parse_weekly(value)
        if not got:
            return None
        wd, hh, mm = got
        days_ahead = (wd - now.weekday()) % 7
        t = (now + timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=7)
        return t.strftime("%Y-%m-%d %H:%M:%S")
    if kind == "deadline":
        # deadline schedules are due when `deadline_due` (a target is inside
        # the window) — the ticker decides; next_run reflects the window end.
        try:
            days = int(value)
        except ValueError:
            return None
        return (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return None


class SchedulerManager:
    def __init__(self, store, workflows, events=None,
                 deadline_callback=None, tick_every_s: float = 5.0,
                 clock=None):
        self.store = store
        self.workflows = workflows
        self.events = events
        self.deadline_callback = deadline_callback  # callable() -> [(name, iso)]
        self.tick_every_s = tick_every_s
        self.clock = clock or (lambda: datetime.now())
        self._stop = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False
        if not store.table_exists("schedules"):
            store.install(SCHEMA)

    # -- CRUD ----------------------------------------------------------------
    def add(self, name: str, kind: str, value: str = "", workflow_id: int = 0,
            params: dict | None = None) -> dict:
        if kind not in SCHEDULE_KINDS:
            raise ValueError(f"bad schedule kind: {kind} (expected {SCHEDULE_KINDS})")
        next_run = compute_next_run(kind, value, now=self.clock())
        sid = self.store.insert(
            "schedules", name=name, kind=kind, value=value, enabled=1,
            workflow_id=workflow_id, params=json.dumps(params or {}),
            last_run="", next_run=next_run or "", created_at=_now())
        return self.get(sid)

    def get(self, sched_id: int) -> dict | None:
        r = self.store.fetchone("SELECT * FROM schedules WHERE id = ?", (sched_id,))
        if r:
            r["params"] = json.loads(r["params"] or "{}")
        return r

    def list(self) -> list[dict]:
        rows = self.store.fetch("SELECT * FROM schedules ORDER BY id")
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
        return rows

    def set_enabled(self, sched_id: int, enabled: bool) -> dict | None:
        self.store.exec("UPDATE schedules SET enabled = ? WHERE id = ?",
                        (1 if enabled else 0, sched_id))
        return self.get(sched_id)

    def delete(self, sched_id: int) -> None:
        self.store.exec("DELETE FROM schedules WHERE id = ?", (sched_id,))

    # -- ticking -------------------------------------------------------------
    def due(self, now: datetime | None = None) -> list[dict]:
        """Schedules whose next_run is <= now (or deadline kind where a target
        is inside the window and not yet handled)."""
        now = now or self.clock()
        out = []
        for s in self.list():
            if not s["enabled"]:
                continue
            if s["kind"] == "deadline":
                window = int(s["value"] or 0)
                due_targets, untouched = self._deadline_targets(window)
                if untouched:
                    s["_targets"] = due_targets
                    out.append(s)
                continue
            nr = s.get("next_run") or ""
            if nr and nr[:19] <= now.strftime("%Y-%m-%d %H:%M:%S"):
                out.append(s)
        return out

    def _deadline_targets(self, window_days: int) -> tuple[list, bool]:
        """(targets inside window, whether an unhandled target exists)."""
        targets = []
        if not self.deadline_callback:
            return targets, False
        today = self.clock().date()
        limit = today + timedelta(days=max(0, window_days))
        for name, iso in self.deadline_callback() or []:
            try:
                d = datetime.strptime(iso, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if today <= d <= limit:
                targets.append({"name": name, "deadline": iso})
        if not targets:
            return targets, False
        # dedupe: only "untouched" if a target was never seen before
        seen = self.store.fetchone("SELECT value FROM astra_sched_seen WHERE k = ?",
                                   ("deadline_key",))
        current_key = ",".join(sorted(f"{t['name']}|{t['deadline']}" for t in targets))
        return targets, (seen is None or seen["value"] != current_key)

    def tick(self) -> list[dict]:
        """Fire everything currently due. Returns the fired schedule list."""
        fired = []
        for s in self.due():
            self._fire(s)
            if self.events:
                self.events.emit("scheduler.tick", agent="scheduler",
                                 schedule=s["name"], kind=s["kind"])
            fired.append(s)
        return fired

    def _fire(self, s: dict) -> None:
        now = self.clock()
        nxt = ""
        if s["kind"] == "deadline":
            # dedupe mark: record the target key so it doesn't refire
            targets = s.get("_targets", [])
            key = ",".join(sorted(f"{t['name']}|{t['deadline']}" for t in targets))
            self.store.exec(
                "INSERT INTO astra_sched_seen(k, value) VALUES('deadline_key',?) "
                "ON CONFLICT(k) DO UPDATE SET value=excluded.value", (key,))
            params = dict(s["params"]); params["targets"] = targets
            nxt = compute_next_run("deadline", s["value"], now=now)
        else:
            params = s["params"]
            nxt = compute_next_run(s["kind"], s["value"], after=now, now=now)
        self.store.exec(
            "UPDATE schedules SET last_run = ?, next_run = ? WHERE id = ?",
            (_now_iso(now), nxt or "", s["id"]))
        if s.get("workflow_id"):
            tid = threading.Thread(
                target=self._run_in_background,
                args=(s["workflow_id"], params, s["name"]), daemon=True)
            tid.start()

    def _run_in_background(self, workflow_id: int, params: dict, label: str) -> None:
        try:
            self.workflows.run(workflow_id=workflow_id, params=params)
        except Exception:
            pass  # workflow engine records its own errors/events

    # -- thread --------------------------------------------------------------
    def start(self) -> None:
        # Idempotent: two callers (e.g. a rebuild + a lifecycle hook) must not
        # start two tick loops that fire every schedule twice.
        with self._start_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(self.tick_every_s)

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict:
        rows = self.list()
        return {"schedules": len(rows),
                "enabled": sum(1 for r in rows if r["enabled"]),
                "next": [{"name": r["name"], "kind": r["kind"],
                          "next_run": r["next_run"]} for r in rows if r["enabled"]]}


def _now_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")
