"""Astra AI Gateway — multi-provider / multi-model intelligent routing.

This module is the Gateway's OWN routing brain — deliberately separate from
`astra/ai/router.py` (AstraRouter) and `astra/ai/routing_policy.py`
(RoutingDecisionPolicy), which belong exclusively to the existing Provider
system. Nothing here touches ProviderRegistry, Provider adapters, Provider
credentials or Provider model configuration; it only ever reasons about the
Gateway's own connections (`astra/ai/gateway.py`) and their own
GW_-prefixed model lists.

Pieces:
    - `classify_gateway_request`  — deterministic, local request classification
    - `build_gateway_catalog`     — flattens each connection's configured
                                     models into (connection, Model) pairs,
                                     using the existing `astra.ai.models`
                                     metadata heuristics (never invented)
    - `GatewayModelHealth`        — per (provider, model) runtime health
    - `GatewayRoutingState`       — persistent last-successful target +
                                     per-target health, backed by the
                                     project's existing Store (SQLite) when
                                     one is supplied, in-memory otherwise
    - `eligible_targets`          — capability/context/health filtering
    - `rank_targets`              — capability + health + latency + priority
                                     scoring
    - `prefer_last_successful`    — soft "stick to what last worked" nudge

`astra/ai/gateway.py` (AstraAIGateway) is the only caller; it owns the actual
HTTP execution and per-attempt fallback loop.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime

from astra.ai.models import Model, metadata_for

# ── connection name → short provider tag used for model metadata lookup ────
# The Gateway's connection classes are named "astra-gw-<service>"; model
# metadata (astra.ai.models) is keyed by the short, familiar provider name
# so family heuristics (gemini/groq/cloudflare/bedrock model-id patterns)
# resolve the same way they do for the existing Provider system.
GATEWAY_PROVIDER_SHORT = {
    "astra-gw-gemini": "gemini",
    "astra-gw-groq": "groq",
    "astra-gw-cloudflare": "cloudflare",
    "astra-gw-bedrock": "bedrock",
    "astra-gw-openrouter": "openrouter",
    "astra-gw-mistral": "mistral",
    "astra-gw-cerebras": "cerebras",
    "astra-gw-sambanova": "sambanova",
    "astra-gw-cohere": "cohere",
    "astra-gw-zai": "zai",
}

REQUEST_CATEGORIES = (
    "simple", "general", "reasoning", "coding", "long_context",
    "structured_output", "tool_use", "vision",
    # The Gateway's OWN control calls (chat pipeline: understand+assign and
    # verify). They must answer with one strict JSON object and sit on the
    # critical path of every chat turn, so they need a JSON-capable model and
    # the lowest latency — not the best prose model. See `score_target`.
    "control",
)

# Category → capability the model MUST declare (hard filter). Left out on
# purpose: "simple"/"general"/"reasoning"/"coding" are soft *preferences*
# (scored, never hard-excluded) — a request classified "coding" should
# still be servable by a non-coding-tagged model when nothing better is
# configured (spec: "prefer a coding-capable model when available").
CATEGORY_HARD_CAPS: dict[str, tuple[str, ...]] = {
    "structured_output": ("json",),
    "tool_use": ("tools",),
    "vision": ("vision",),
    "control": ("json",),
}

# Baseline latency estimate (ms), used only until a target has real
# measured latency — keyed by the same quality_class values models.py
# already produces ("fast"/"mid"/"high"; see metadata_for()'s `q`).
GATEWAY_SPEED_ESTIMATE_MS = {"fast": 300.0, "mid": 900.0, "high": 2000.0}

DEFAULT_COOLDOWN_S = 30.0
MAX_COOLDOWN_S = 300.0
LONG_CONTEXT_TOKENS = 32000

_COD_RE = re.compile(
    r"\b(code|coding|fix|bug|debug|refactor|function|script|program|"
    r"compile|stack trace|traceback|repo|github)\b", re.I)
_REASON_RE = re.compile(
    r"\b(why|explain|reasoning|prove|derive|step by step|analyze|logic|"
    r"deduce)\b", re.I)
_STRUCT_RE = re.compile(
    r"\bjson\b|\btable\b|\bcsv\b|\bstructured\b|\bschema\b", re.I)
_VISION_HINT_RE = re.compile(
    r"\bimage\b|\bphoto\b|\bpicture\b|\bscreenshot\b|\bvision\b", re.I)

SIMPLE_TEXT_MAX_CHARS = 40


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════
# 10. Request classification — lightweight, deterministic, local
# ═══════════════════════════════════════════════════════════════════════════
def classify_gateway_request(text: str, *, vision: bool = False,
                              structured_output: bool = False,
                              tools: list | None = None,
                              context_tokens: int = 0,
                              long_context_tokens: int = LONG_CONTEXT_TOKENS) -> str:
    """Classify a raw request into one of REQUEST_CATEGORIES.

    Deterministic and local — never calls a Gateway model just to classify.
    Explicit flags (`vision`, `structured_output`, `tools`) always win over
    text heuristics; text heuristics are a conservative fallback only.
    """
    text = str(text or "")
    if vision or _VISION_HINT_RE.search(text):
        return "vision"
    if structured_output or _STRUCT_RE.search(text):
        return "structured_output"
    if tools:
        return "tool_use"
    if context_tokens and context_tokens >= long_context_tokens:
        return "long_context"
    if _COD_RE.search(text):
        return "coding"
    if _REASON_RE.search(text):
        return "reasoning"
    if len(text.strip()) <= SIMPLE_TEXT_MAX_CHARS:
        return "simple"
    return "general"


# ═══════════════════════════════════════════════════════════════════════════
# 9/16. Model catalog — one Model per configured Gateway model, using only
# explicit metadata/heuristics (astra.ai.models), never invented capabilities.
# ═══════════════════════════════════════════════════════════════════════════
def build_gateway_catalog(connections: list) -> list[tuple[object, Model]]:
    """Flatten every connection's configured models into (connection, Model).

    Connection order and each connection's own model order are preserved —
    later used as the "configured priority" tie-break in `rank_targets`.
    Multiple models per connection are fully supported (§1/§16): nothing
    here assumes one model per provider.
    """
    out: list[tuple[object, Model]] = []
    for conn in connections:
        cname = getattr(conn, "name", "")
        short = GATEWAY_PROVIDER_SHORT.get(cname, cname)
        for mid in (getattr(conn, "models", None) or []):
            meta = metadata_for(mid, short)
            meta.pop("provider", None)
            out.append((conn, Model(short, mid, **meta)))
    return out


def _connection_usable(conn) -> bool:
    """Provider-level (connection-level) health — never model-level (§6)."""
    pool = getattr(conn, "pool", None)
    if pool is not None:
        return bool(pool)
    try:
        return bool(conn.health_check())
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 8/14. Per-target (provider+model) health, with persistence
# ═══════════════════════════════════════════════════════════════════════════
class GatewayModelHealth:
    """Runtime health for one (provider, model) Gateway target.

    A model-level failure never marks the whole connection/provider down
    (§6) — cooldown here is scoped strictly to this one (provider, model).
    """
    __slots__ = ("provider", "model", "success_count", "failure_count",
                 "consecutive_failures", "average_latency_ms",
                 "last_success", "last_failure", "cooldown_until")

    def __init__(self, provider: str, model: str, *, success_count: int = 0,
                 failure_count: int = 0, consecutive_failures: int = 0,
                 average_latency_ms: float = 0.0, last_success: str = "",
                 last_failure: str = "", cooldown_until: float = 0.0):
        self.provider = provider
        self.model = model
        self.success_count = int(success_count)
        self.failure_count = int(failure_count)
        self.consecutive_failures = int(consecutive_failures)
        self.average_latency_ms = float(average_latency_ms)
        self.last_success = last_success
        self.last_failure = last_failure
        self.cooldown_until = float(cooldown_until)

    @property
    def healthy(self) -> bool:
        """Out of cooldown. A brand-new (never-tried) target is healthy."""
        return time.time() >= self.cooldown_until

    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return (self.success_count / total) if total else 1.0

    def note_success(self, latency_ms: float) -> None:
        self.success_count += 1
        self.consecutive_failures = 0
        self.last_success = _now_iso()
        self.cooldown_until = 0.0
        # simple exponential moving average — smooths one-off spikes without
        # a growing window.
        self.average_latency_ms = (
            latency_ms if not self.average_latency_ms
            else (self.average_latency_ms * 0.7 + latency_ms * 0.3))

    def note_failure(self, *, cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
        """Enter cooldown — never a permanent blacklist (§8)."""
        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_failure = _now_iso()
        escalation = 1.0 + min(self.consecutive_failures - 1, 4) * 0.5
        self.cooldown_until = time.time() + min(cooldown_s * escalation,
                                                 MAX_COOLDOWN_S)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model,
            "healthy": self.healthy, "success_count": self.success_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "average_latency_ms": round(self.average_latency_ms, 1),
            "last_success": self.last_success, "last_failure": self.last_failure,
            "cooldown_until": self.cooldown_until,
        }


GATEWAY_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_routing_state (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    last_successful_provider TEXT DEFAULT '',
    last_successful_model    TEXT DEFAULT '',
    last_success_timestamp   TEXT DEFAULT '',
    last_success_latency_ms  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gateway_model_health (
    provider              TEXT NOT NULL,
    model                 TEXT NOT NULL,
    success_count         INTEGER DEFAULT 0,
    failure_count         INTEGER DEFAULT 0,
    consecutive_failures  INTEGER DEFAULT 0,
    average_latency_ms    REAL DEFAULT 0,
    last_success          TEXT DEFAULT '',
    last_failure          TEXT DEFAULT '',
    cooldown_until        REAL DEFAULT 0,
    PRIMARY KEY (provider, model)
);
"""


class GatewayRoutingState:
    """Persistent last-successful target + per-target health.

    Uses the project's existing Store (SQLite) when supplied — no new
    database is introduced. Falls back to in-memory-only state when no
    store is given (e.g. tests, or a Gateway built without one), so the
    Gateway keeps working; it simply won't remember across a restart.
    Never persists API keys/credentials — only provider/model names,
    counters and timestamps.
    """

    def __init__(self, store=None):
        self.store = store
        self._lock = threading.RLock()
        self._health: dict[tuple[str, str], GatewayModelHealth] = {}
        self._last_provider = ""
        self._last_model = ""
        self._last_timestamp = ""
        self._last_latency_ms = 0
        if self.store is not None:
            try:
                self.store.install(GATEWAY_STATE_SCHEMA)
            except Exception:
                self.store = None
        self._load()

    # -- load -----------------------------------------------------------------
    def _load(self) -> None:
        if not self.store:
            return
        try:
            row = self.store.fetchone(
                "SELECT * FROM gateway_routing_state WHERE id = 1")
            if row:
                self._last_provider = row.get("last_successful_provider") or ""
                self._last_model = row.get("last_successful_model") or ""
                self._last_timestamp = row.get("last_success_timestamp") or ""
                self._last_latency_ms = int(row.get("last_success_latency_ms") or 0)
            for r in self.store.fetch("SELECT * FROM gateway_model_health"):
                h = GatewayModelHealth(
                    r["provider"], r["model"],
                    success_count=r.get("success_count") or 0,
                    failure_count=r.get("failure_count") or 0,
                    consecutive_failures=r.get("consecutive_failures") or 0,
                    average_latency_ms=r.get("average_latency_ms") or 0.0,
                    last_success=r.get("last_success") or "",
                    last_failure=r.get("last_failure") or "",
                    cooldown_until=r.get("cooldown_until") or 0.0)
                self._health[(h.provider, h.model)] = h
        except Exception:
            # Corrupt/missing table — fail open to a clean in-memory state
            # rather than blocking the Gateway.
            pass

    # -- health -----------------------------------------------------------------
    def get_health(self, provider: str, model: str) -> GatewayModelHealth:
        with self._lock:
            key = (provider, model)
            h = self._health.get(key)
            if h is None:
                h = GatewayModelHealth(provider, model)
                self._health[key] = h
            return h

    def all_health(self) -> list[GatewayModelHealth]:
        with self._lock:
            return list(self._health.values())

    # -- last successful --------------------------------------------------------
    def last_successful(self) -> dict | None:
        with self._lock:
            if not self._last_provider or not self._last_model:
                return None
            return {"provider": self._last_provider, "model": self._last_model,
                    "timestamp": self._last_timestamp,
                    "latency_ms": self._last_latency_ms}

    # -- record -------------------------------------------------------------
    def record_success(self, provider: str, model: str, latency_ms: float) -> None:
        with self._lock:
            h = self.get_health(provider, model)
            h.note_success(latency_ms)
            self._last_provider = provider
            self._last_model = model
            self._last_timestamp = _now_iso()
            self._last_latency_ms = int(latency_ms)
            self._persist_health(h)
            self._persist_last()

    def record_failure(self, provider: str, model: str,
                       cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
        with self._lock:
            h = self.get_health(provider, model)
            h.note_failure(cooldown_s=cooldown_s)
            self._persist_health(h)

    # -- persistence (best-effort; never raises into the caller) --------------
    def _persist_last(self) -> None:
        if not self.store:
            return
        try:
            self.store.exec(
                "INSERT INTO gateway_routing_state "
                "(id, last_successful_provider, last_successful_model, "
                " last_success_timestamp, last_success_latency_ms) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  last_successful_provider = excluded.last_successful_provider, "
                "  last_successful_model    = excluded.last_successful_model, "
                "  last_success_timestamp   = excluded.last_success_timestamp, "
                "  last_success_latency_ms  = excluded.last_success_latency_ms",
                (self._last_provider, self._last_model,
                 self._last_timestamp, self._last_latency_ms))
        except Exception:
            pass

    def _persist_health(self, h: GatewayModelHealth) -> None:
        if not self.store:
            return
        try:
            self.store.exec(
                "INSERT INTO gateway_model_health "
                "(provider, model, success_count, failure_count, "
                " consecutive_failures, average_latency_ms, last_success, "
                " last_failure, cooldown_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, model) DO UPDATE SET "
                "  success_count        = excluded.success_count, "
                "  failure_count        = excluded.failure_count, "
                "  consecutive_failures = excluded.consecutive_failures, "
                "  average_latency_ms   = excluded.average_latency_ms, "
                "  last_success         = excluded.last_success, "
                "  last_failure         = excluded.last_failure, "
                "  cooldown_until       = excluded.cooldown_until",
                (h.provider, h.model, h.success_count, h.failure_count,
                 h.consecutive_failures, h.average_latency_ms,
                 h.last_success, h.last_failure, h.cooldown_until))
        except Exception:
            pass

    # -- reporting --------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_successful": self.last_successful(),
                "model_health": {f"{p}:{m}": h.to_dict()
                                 for (p, m), h in self._health.items()},
            }


# ═══════════════════════════════════════════════════════════════════════════
# 2/3/15. Filtering, scoring and ranking
# ═══════════════════════════════════════════════════════════════════════════
def meets_gateway_requirements(model: Model, *, category: str,
                                context_tokens: int = 0) -> bool:
    """Binary capability/context gate — never selectable if this fails,
    regardless of score (mirrors routing_policy.meets_hard_requirements()
    for the existing Provider system, kept as a separate function since the
    Gateway's category vocabulary and hard-cap table are its own)."""
    hard = CATEGORY_HARD_CAPS.get(category, ())
    if hard and not set(hard).issubset(set(model.capabilities)):
        return False
    if context_tokens and model.context_window < context_tokens:
        return False
    return True


def eligible_targets(catalog: list[tuple[object, Model]],
                      routing_state: GatewayRoutingState, *, category: str,
                      context_tokens: int = 0
                      ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """Connection-usable + capability/context-suitable + not-in-cooldown."""
    out = []
    for conn, model in catalog:
        if not _connection_usable(conn):
            continue
        if not meets_gateway_requirements(model, category=category,
                                          context_tokens=context_tokens):
            continue
        health = routing_state.get_health(model.provider, model.model_id)
        if not health.healthy:
            continue
        out.append((conn, model, health))
    return out


def score_target(model: Model, health: GatewayModelHealth, *, category: str,
                 priority_bonus: float = 0.0) -> float:
    """Higher is better. Capability suitability is a pre-filter (see
    `meets_gateway_requirements`) — this only ranks among already-suitable
    candidates, so a faster/healthier model is never preferred over a
    slower one that is the only capability-suitable choice (§3/§7)."""
    score = 0.0

    # soft category preference (never a hard requirement here — see
    # CATEGORY_HARD_CAPS for the categories that *are* hard-filtered above)
    if category == "coding" and model.has("coding"):
        score += 6.0
    elif category == "reasoning" and (model.has("reasoning") or
                                      model.quality_class == "high"):
        score += 5.0
    elif category in ("simple", "general") and model.quality_class == "fast":
        score += 3.0
    elif category == "long_context":
        score += min(2.0, model.context_window / 500000.0)

    # health / recent success / recent failures
    score += 2.0 if health.healthy else -20.0
    score += 1.5 * health.success_rate()
    score -= 3.0 * min(1.0, health.consecutive_failures / 3.0)

    # latency — lower is better; use measured average once we have one
    est_latency_ms = (health.average_latency_ms if health.average_latency_ms
                      else GATEWAY_SPEED_ESTIMATE_MS.get(model.quality_class, 900.0))
    if category == "control":
        # Control calls are two extra sequential round-trips around every
        # chat answer, so latency dominates: double weight and a wider
        # window (a 5s model no longer ties with a 2s one at score 0), plus
        # a mild quality tilt so a tiny model is not picked on speed alone.
        score += 2.0 * max(0.0, 3.0 - est_latency_ms / 1000.0)
        score += {"high": 1.0, "mid": 0.5}.get(model.quality_class, 0.0)
    else:
        score += max(0.0, 2.0 - est_latency_ms / 1000.0)

    # baseline quality so higher-tier models aren't starved outside their
    # special-cased categories above
    score += {"high": 0.5, "mid": 0.3, "fast": 0.2}.get(model.quality_class, 0.2)

    # configured priority (list order) tie-break
    score += priority_bonus
    if model.disabled:
        score -= 100.0
    return round(score, 4)


def rank_targets(targets: list[tuple[object, Model, GatewayModelHealth]], *,
                 category: str
                 ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """Score + sort, descending. Ties keep their original (configured)
    order — Python's sort is stable and every tie also gets an explicit,
    small `priority_bonus` favoring earlier configuration, so "configured
    priority" (§9) is honoured even without any other signal."""
    n = len(targets)
    scored = []
    for idx, (conn, model, health) in enumerate(targets):
        priority_bonus = 0.05 * (n - idx)
        s = score_target(model, health, category=category,
                         priority_bonus=priority_bonus)
        scored.append((s, conn, model, health))
    scored.sort(key=lambda t: -t[0])
    return [(c, m, h) for _, c, m, h in scored]


def prefer_last_successful(
        ranked: list[tuple[object, Model, GatewayModelHealth]],
        last: dict | None
        ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """§5: prefer the last successful target IF it's still eligible+healthy
    (i.e. still present in `ranked`, since `eligible_targets` already
    dropped anything unhealthy/unsuitable) — never a permanent lock."""
    if not last:
        return ranked
    for i, (conn, model, health) in enumerate(ranked):
        if model.provider == last.get("provider") and \
                model.model_id == last.get("model"):
            if i == 0:
                return ranked
            item = ranked[i]
            return [item] + ranked[:i] + ranked[i + 1:]
    return ranked
