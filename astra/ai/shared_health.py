"""Shared manual upstream health-check deduplication between the existing
Provider system (astra/ai/router.py) and the Astra AI Gateway
(astra/ai/gateway.py).

The Provider and Gateway systems stay fully independent (separate adapters/
connections, separate credentials, separate local health state, separate
execution paths). The ONLY thing this module coordinates is the manual
"test connection" HTTP probe itself, for the specific case where both
systems would otherwise send an identical real upstream request:

    same canonical upstream provider + same model

    Provider Router                     Gateway Router
         |                                    |
    Provider Health                    Gateway Health
         |                                    |
         +------------> SharedHealthCoordinator <------------+
                              |
                     ONE real upstream call
                              |
                     Shared Health Result
                        /            \\
              Provider local state   Gateway local state

Anything less specific than that pair (different model or
a different canonical provider) is never shared -- see `resolve_identity` /
`SharedHealthIdentity`. Only the manual health-check entry points
(`AstraRouter.test_provider_model`, `AstraAIGateway.test_connection_model`)
ever call into this module; normal chat routing, model selection, credential
rotation and image generation are untouched.

No API key is part of the shared identity. The persisted `shared_health_result`
row contains only canonical_provider / model / ok / error / latency_ms /
timestamps; each caller still records the shared outcome in its own per-key
health state and preserves its own key label.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

# The Gateway's connection classes are named "astra-gw-<service>"; Provider
# adapter names (astra/ai/provider.py, astra/ai/adapters/*.py) are already
# the canonical short form ("groq", "gemini", ...). Reusing gateway_routing's
# existing table (already used to key model-metadata lookups the same way)
# means there is exactly one place a new Gateway connection's canonical name
# ever needs to be taught -- not a second, fragile `"groq" in name` check.
from astra.ai.gateway_routing import GATEWAY_PROVIDER_SHORT

# A saved shared result steers dedup only while this fresh -- same window
# AstraRouter already uses for its own per-key/model health cache
# (router.KEY_MODEL_TTL_S), so "still fresh" means the same thing on both
# sides of this boundary.
DEFAULT_TTL_S = 600.0

# Credential identity is intentionally excluded from shared-health dedup.
def canonical_provider(name: str) -> str:
    """Resolve a Provider adapter name OR a Gateway connection name to the
    single upstream identity they both probe (e.g. "groq" for either
    "groq" or "astra-gw-groq"). Never returns an empty string for a
    non-empty input -- an unrecognized name is returned unchanged (it is
    already canonical, or it is at least stable, which is all identity
    equality needs)."""
    return GATEWAY_PROVIDER_SHORT.get(name, name)


class SharedHealthIdentity(tuple):
    """(canonical_provider, model) -- the manual health-check sharing
    identity. Credentials are deliberately NOT part of this identity."""
    __slots__ = ()

    def __new__(cls, provider: str, model: str):
        return super().__new__(cls, (provider, model))

    @property
    def provider(self) -> str:
        return self[0]

    @property
    def model(self) -> str:
        return self[1]


def resolve_identity(pool, provider_name: str, model_id: str,
                     key_id: str | None = None):
    """Resolve shared provider+model identity plus this caller's owner key.

    key_id controls only the credential used if this caller owns the probe;
    it is deliberately excluded from SharedHealthIdentity.
    """
    if pool is None or not hasattr(pool, "pick") or not hasattr(pool, "pinned"):
        return None, None
    if key_id:
        with pool.pinned(key_id):
            cred = pool.pick(model_id)
    else:
        cred = pool.pick(model_id)
    if cred is None:
        return None, None
    secret = pool.get_secret_for(cred) if hasattr(pool, "get_secret_for") else None
    if not secret:
        return None, None
    return SharedHealthIdentity(canonical_provider(provider_name), model_id), cred

def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SHARED_HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_health_result (
    canonical_provider TEXT NOT NULL,
    model              TEXT NOT NULL,
    ok                 INTEGER NOT NULL DEFAULT 0,
    error              TEXT DEFAULT '',
    latency_ms         REAL DEFAULT 0,
    tested_at          TEXT DEFAULT '',
    ts                 REAL DEFAULT 0,
    source             TEXT DEFAULT 'live',
    PRIMARY KEY (canonical_provider, model)
);
"""


class SharedHealthCoordinator:
    """Deduplicates MANUAL upstream health-check probes across the Provider
    and Gateway systems (see module docstring). Purely a coordination +
    persistence layer -- it never talks to an upstream API itself (the
    caller's `probe_fn` does that), never touches normal chat/routing
    execution, and never stores a secret.

    Uses the project's existing Store (SQLite) when supplied, for
    freshness that survives a restart (Test 10); falls back to in-memory
    only otherwise. Reuses the SAME instance across both systems' `run()`
    calls is what actually makes the concurrent case (Test 7) safe --
    `AstraRouter` and `AstraAIGateway` converge onto one shared instance
    when wired together (see their `__init__`), since a Python-level lock
    is what enforces "first caller owns the probe" in-process.
    """

    def __init__(self, store=None, ttl_s: float = DEFAULT_TTL_S):
        self.store = store
        self.ttl_s = float(ttl_s)
        self._lock = threading.RLock()
        self._cache: dict[SharedHealthIdentity, dict] = {}
        self._inflight: dict[SharedHealthIdentity, threading.Event] = {}
        self._inflight_result: dict[SharedHealthIdentity, dict] = {}
        if self.store is not None:
            try:
                self._ensure_schema()
            except Exception:
                # Persistence is an optimization; in-memory sharing remains valid.
                self.store = None

    def _ensure_schema(self) -> None:
        """Install the provider+model schema and migrate the previous
        provider+credential+model table. New identity ignores credentials."""
        self.store.install(SHARED_HEALTH_SCHEMA)
        cols = self.store.fetch("PRAGMA table_info(shared_health_result)")
        names = {r["name"] for r in cols}
        if "credential_fingerprint" not in names:
            return
        self.store.exec(
            "CREATE TABLE IF NOT EXISTS shared_health_result_v2 ("
            "canonical_provider TEXT NOT NULL, model TEXT NOT NULL, "
            "ok INTEGER NOT NULL DEFAULT 0, error TEXT DEFAULT '', "
            "latency_ms REAL DEFAULT 0, tested_at TEXT DEFAULT '', "
            "ts REAL DEFAULT 0, source TEXT DEFAULT 'live', "
            "PRIMARY KEY (canonical_provider, model))"
        )
        self.store.exec(
            "INSERT OR REPLACE INTO shared_health_result_v2 "
            "(canonical_provider, model, ok, error, latency_ms, tested_at, ts, source) "
            "SELECT old.canonical_provider, old.model, old.ok, old.error, "
            "old.latency_ms, old.tested_at, old.ts, old.source "
            "FROM shared_health_result old "
            "WHERE old.ts = (SELECT MAX(newer.ts) FROM shared_health_result newer "
            "WHERE newer.canonical_provider=old.canonical_provider "
            "AND newer.model=old.model)"
        )
        self.store.exec("DROP TABLE shared_health_result")
        self.store.exec("ALTER TABLE shared_health_result_v2 RENAME TO shared_health_result")

    # -- freshness ----------------------------------------------------------
    def _fresh(self, row: dict | None) -> dict | None:
        if not row:
            return None
        if time.time() - row["ts"] > self.ttl_s:
            return None
        return row

    def _load_locked(self, identity: SharedHealthIdentity) -> dict | None:
        """Must be called with `self._lock` held."""
        row = self._cache.get(identity)
        if row is not None:
            return row
        if not self.store:
            return None
        try:
            r = self.store.fetchone(
                "SELECT * FROM shared_health_result WHERE canonical_provider=? "
                "AND model=?", tuple(identity))
        except Exception:
            return None
        if not r:
            return None
        row = {"ok": bool(r["ok"]), "error": r["error"] or "",
              "latency_ms": r["latency_ms"] or 0.0,
              "tested_at": r["tested_at"] or "", "ts": r["ts"] or 0.0}
        self._cache[identity] = row
        return row

    def get_fresh(self, identity: SharedHealthIdentity) -> dict | None:
        """A still-fresh shared result for `identity`, or None. Read-only:
        never triggers a probe, never claims ownership."""
        with self._lock:
            fresh = self._fresh(self._load_locked(identity))
            return dict(fresh) if fresh is not None else None

    def _save_locked(self, identity: SharedHealthIdentity, result: dict) -> dict:
        """Must be called with `self._lock` held. Returns the saved row."""
        row = {"ok": bool(result.get("ok")),
              "error": str(result.get("error") or ""),
              "latency_ms": float(result.get("latency_ms") or 0.0),
              "tested_at": _now_iso(), "ts": time.time()}
        self._cache[identity] = row
        if self.store:
            try:
                self.store.exec(
                    "INSERT INTO shared_health_result (canonical_provider, model, ok, error, "
                    "latency_ms, tested_at, ts, source) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(canonical_provider, model) "
                    "DO UPDATE SET ok=excluded.ok, error=excluded.error, "
                    "latency_ms=excluded.latency_ms, tested_at=excluded.tested_at, "
                    "ts=excluded.ts, source=excluded.source",
                    (identity.provider, identity.model, int(row["ok"]),
                     row["error"], row["latency_ms"], row["tested_at"],
                     row["ts"], "live"))
            except Exception:
                pass
        return row

    # -- the coordinated probe ------------------------------------------------
    def run(self, identity: SharedHealthIdentity, probe_fn):
        """Return (result, reused) for `identity`.

        `probe_fn()` performs exactly ONE real upstream call when this
        caller turns out to be the owner, and must return a dict with at
        least {ok, error, latency_ms}. Across every concurrent caller for
        the same identity, `probe_fn` runs at most once per freshness
        window: the first caller to find no fresh result becomes the
        owner and everyone else waits for (then reuses) its result --
        including a fresh result that already existed before this call.
        """
        while True:
            with self._lock:
                fresh = self._fresh(self._load_locked(identity))
                if fresh is not None:
                    return dict(fresh), True
                ev = self._inflight.get(identity)
                if ev is None:
                    ev = threading.Event()
                    self._inflight[identity] = ev
                    owner = True
                else:
                    owner = False

            if owner:
                try:
                    result = probe_fn()
                except Exception:
                    # A raising probe must never wedge waiters forever --
                    # record it as a terminal failure so everyone gets an
                    # answer, then let the owner's own caller see the
                    # exception exactly as it would have without sharing.
                    with self._lock:
                        row = self._save_locked(identity, {
                            "ok": False, "error": "health probe raised",
                            "latency_ms": 0.0})
                        self._inflight_result[identity] = row
                        self._inflight.pop(identity, None)
                        ev.set()
                    raise
                with self._lock:
                    row = self._save_locked(identity, result)
                    self._inflight_result[identity] = row
                    self._inflight.pop(identity, None)
                    ev.set()
                return dict(result), False

            # Waiter: block for the owner's result, then reuse it -- never
            # make a second real call for the same identity while one is
            # already in flight.
            ev.wait(timeout=max(self.ttl_s, 30.0))
            with self._lock:
                row = self._inflight_result.pop(identity, None)
                if row is None:
                    row = self._fresh(self._load_locked(identity))
            if row is not None:
                return dict(row), True
            # The owner finished without publishing anything retrievable
            # (should not happen -- every path above publishes before
            # clearing `_inflight`) -- loop and try again rather than ever
            # leaving a waiter stuck with nothing.
