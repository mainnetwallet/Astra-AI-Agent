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
    # Image *production* — deliberately separate from "vision" (image
    # *understanding*). A request in either category may only ever be served
    # by a model whose (provider, model) entry in astra.ai.image_models
    # proves it can produce image output. See CATEGORY_HARD_CAPS below.
    "image_generation", "image_editing",
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
    # Hard, not soft: a text-only or vision-only model must never be handed an
    # image-generation request just because it is the fastest/healthiest one.
    "image_generation": ("image_generation",),
    "image_editing": ("image_editing",),
    "control": ("json",),
}

# Category → output modality the target MUST declare. This is the second,
# independent half of the image gate: even a model that somehow carries the
# `image_generation` capability label is rejected unless its output modality
# really includes "image".
CATEGORY_REQUIRED_OUTPUT_MODALITY: dict[str, str] = {
    "image_generation": "image",
    "image_editing": "image",
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

# Image *production* intent, English + Bangla/Banglish. The noun list carries
# both Latin transliteration and Bengali script; the verb list carries the
# inflected stems actually used in Banglish ("banao"/"banai"/"baniye"/"bana"/
# "banate", "toiri", "create", "generate", "draw", "make", "design"). The
# two orders (verb-first English, noun-first Banglish) are both matched.
# Deliberately omitted: bare "image"/"photo"/"picture" (that alone is a
# vision request — "what is in this photo?") and the word "vision".
_IMG_NOUNS = (r"(?:image|picture|photo|photograph|illustration|diagram|logo|"
              r"icon|art|artwork|chobi|chhobi|ছবি|ফটো|ফোটো)")
_IMG_VERBS = (r"(?:generate|create|draw|make|design|render|paint|"
              r"banao|banawo|banai|baniye|banate|bana|banan|toiri|"
              r"বানাও|বানান|বানাতে|তৈরি|করো|তৈরী)")
_IMAGE_GEN_RE = re.compile(
    _IMG_VERBS + r"\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,3}" + _IMG_NOUNS +
    r"|" + _IMG_NOUNS + r"[^\n]{0,24}?" + _IMG_VERBS, re.I)

# Image *editing* of an existing image (inpaint / retouch / instruction edit).
_IMG_EDIT_NOUNS = (r"(?:image|picture|photo|photograph|chobi|chhobi|"
                   r"ছবি|ফটো|ফোটো)")
_IMG_EDIT_VERBS = r"(?:edit|editing|modify|retouch|inpaint|outpaint|restyle|"\
                  r"এডিট|পরিবর্তন|badle|badol|change)"
_IMAGE_EDIT_RE = re.compile(
    _IMG_EDIT_VERBS + r"\s+(?:this|the|my|ei|এই)?\s*" + _IMG_EDIT_NOUNS +
    r"|" + _IMG_EDIT_NOUNS + r"[^\n]{0,24}?" + _IMG_EDIT_VERBS, re.I)

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
    if structured_output or _STRUCT_RE.search(text):
        return "structured_output"
    if tools:
        return "tool_use"
    # Image PRODUCTION is checked before the generic vision hint: "create a
    # photo" contains the word "photo", but the user is asking Astra to make
    # an image, not to look at one. "describe this screenshot" matches neither
    # verb list and still classifies as `vision` below.
    if _IMAGE_EDIT_RE.search(text):
        return "image_editing"
    if _IMAGE_GEN_RE.search(text):
        return "image_generation"
    if vision or _VISION_HINT_RE.search(text):
        return "vision"
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
def build_gateway_catalog(connections: list, *,
                          include_image_models: bool = False
                          ) -> list[tuple[object, Model]]:
    """Flatten every connection's configured models into (connection, Model).

    Connection order and each connection's own model order are preserved —
    later used as the "configured priority" tie-break in `rank_targets`.
    Multiple models per connection are fully supported (§1/§16): nothing
    here assumes one model per provider.
    """
    out: list[tuple[object, Model]] = []
    seen: set[tuple[str, str]] = set()
    for conn in connections:
        cname = getattr(conn, "name", "")
        short = GATEWAY_PROVIDER_SHORT.get(cname, cname)
        for mid in (getattr(conn, "models", None) or []):
            meta = metadata_for(mid, short)
            meta.pop("provider", None)
            out.append((conn, Model(short, mid, **meta)))
            seen.add((short, mid))
        # include_image_models=True: append this connection's separately
        # configured image models. Default False, so the ordinary catalog is
        # exactly the configured chat models and image models only ever enter
        # an image request's decision.
        if include_image_models:
            for mid in (getattr(conn, "image_models", None) or []):
                if (short, mid) in seen:
                    continue
                meta = metadata_for(mid, short)
                meta.pop("provider", None)
                if "image" not in meta.get("output_modalities", []):
                    # The connection claims an image model the evidence table
                    # does not recognize. Never invent the capability.
                    continue
                seen.add((short, mid))
                out.append((conn, Model(short, mid, **meta)))
    return out


def _connection_usable(conn, *, use_image_pool: bool = False) -> bool:
    """Provider-level (connection-level) health — never model-level (§6).

    ``use_image_pool=True`` (image generation only) checks the connection's
    OWN dedicated image-provider credentials
    (`conn.image_credentials_configured()`) instead of its normal chat
    `pool` — ImageRouter must never treat a connection as image-eligible
    just because its chat GW_* credentials are configured (see
    `eligible_image_generation_targets`).
    """
    if use_image_pool:
        fn = getattr(conn, "image_credentials_configured", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        pool = getattr(conn, "image_pool", None)
        if pool is not None:
            return bool(pool)
        # No dedicated image-credential plumbing at all (e.g. a connection
        # double without `image_pool`/`image_credentials_configured`): fall
        # back to the connection's normal chat pool -- same documented
        # fallback `_GatewayCompatibleConnection.image_pool` applies when a
        # real connection has no dedicated IMAGE_* credential configured.
        pool = getattr(conn, "pool", None)
        return bool(pool) if pool is not None else False
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
    need_mod = CATEGORY_REQUIRED_OUTPUT_MODALITY.get(category)
    if need_mod and need_mod not in (model.output_modalities or ["text"]):
        return False
    if category in ("image_generation", "image_editing"):
        # Free-only image pool: capability alone is not enough -- the model
        # must be verified FREE/free-tier eligible (a paid image model must
        # never be selected for a free request).
        from astra.ai.image_models import is_free_image_model
        if not is_free_image_model(model.provider, model.model_id):
            return False
    if context_tokens and model.context_window < context_tokens:
        return False
    return True


def eligible_image_generation_targets(
        catalog: list[tuple[object, Model]],
        routing_state: "GatewayRoutingState", *, editing: bool = False,
        context_tokens: int = 0
        ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """The ONLY targets an image request may ever be sent to.

    A target qualifies only when all of the following hold:

    1. its connection is configured/usable (`_connection_usable`),
    2. its model declares the exact image capability for this category
       (`image_generation`, or `image_editing` when editing an existing
       image) — granted only by astra.ai.image_models,
    3. its output modalities genuinely include "image",
    4. it is not explicitly disabled.

    Deliberately NO health/cooldown filter: image generation uses a simple
    serial fallback, so the ACTUAL generation request -- not a proactive
    health probe -- is the availability signal. A model is only skipped
    within the current request, once it has actually failed there.

    Returns (connection, model, health) triples, preserving configured
    order (call `rank_targets`/`rank_image_targets` for the serial order).
    """
    category = "image_editing" if editing else "image_generation"
    return eligible_targets(catalog, routing_state, category=category,
                            context_tokens=context_tokens,
                            require_health=False, use_image_pool=True)


#: Short alias (the explicit name is the one the Gateway's decision path
#: uses, and the one tests assert against).
eligible_image_targets = eligible_image_generation_targets


def describe_image_targets(
        targets: list[tuple[object, Model, GatewayModelHealth]],
        ) -> list[dict]:
    """Machine/human-readable inventory of eligible image targets, for the
    Activity Log and for tests asserting exactly which models the Gateway
    considered. Never contains credentials."""
    from astra.ai.image_models import image_priority_index
    return [
        {
            "provider": model.provider,
            "model": model.model_id,
            "capabilities": list(model.capabilities),
            "output_modalities": list(model.output_modalities),
            "healthy": health.healthy,
            "latency_ms": round(health.average_latency_ms, 1),
            "consecutive_failures": health.consecutive_failures,
            "cost_class": model.cost_class,
            # deterministic serial-fallback position (lower runs first)
            "priority": image_priority_index(model.provider,
                                             model.model_id),
            "order": idx,
        }
        for idx, (_conn, model, health) in enumerate(targets)
    ]


def eligible_targets(catalog: list[tuple[object, Model]],
                      routing_state: GatewayRoutingState, *, category: str,
                      context_tokens: int = 0,
                      require_health: bool = True,
                      use_image_pool: bool = False
                      ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """Connection-usable + capability/context-suitable + enabled.

    `require_health=True` (the default -- unchanged for chat/vision/coding
    /reasoning/... routing) additionally excludes a target whose
    (provider, model) entry is in cooldown. Image generation passes
    `require_health=False`: there is no proactive image health check, so a
    model is never excluded before the real generation request was attempted.

    `use_image_pool=True` (image generation only) makes `_connection_usable`
    check the connection's dedicated image-provider credentials instead of
    its normal chat pool (see `_connection_usable`).
    """
    out = []
    for conn, model in catalog:
        if not _connection_usable(conn, use_image_pool=use_image_pool):
            continue
        # An explicitly disabled model is never selectable for any category
        # (its score is also heavily penalised, but a hard filter is what
        # spec section 5 requires for the image target list).
        if getattr(model, "disabled", False):
            continue
        if not meets_gateway_requirements(model, category=category,
                                          context_tokens=context_tokens):
            continue
        health = routing_state.get_health(model.provider, model.model_id)
        if require_health and not health.healthy:
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
    elif category in ("image_generation", "image_editing"):
        # Every candidate here already passed the image hard filter, so this
        # is an explicit capability-match bonus (never a way for a text-only
        # model to rank in) plus a real recent-success tilt.
        score += 3.0
        score += 2.0 * health.success_rate()

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


def rank_image_targets(
        targets: list[tuple[object, Model, GatewayModelHealth]], *,
        preferred_ids=None
        ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """Deterministic serial-fallback order for image generation.

    Sorts by the curated/verified FREE-pool priority (astra.ai.image_models)
    and never by health, latency or past success -- so the order is identical
    for every request and each model is always tried in the same place. An
    explicit `preferred_ids` (IMAGE_GENERATION_PRIORITY /
    GW_IMAGE_GENERATION_PRIORITY) reorders the pool but can never add a model
    that failed the static eligibility checks.
    """
    from astra.ai.image_models import image_priority_index, ordered_image_pool
    preferred = {}
    if preferred_ids:
        for i, spec in enumerate(ordered_image_pool(preferred_ids)):
            preferred[spec.model] = i
    return sorted(
        targets,
        key=lambda t: (preferred.get(t[1].model_id, 10_000),
                       image_priority_index(t[1].provider, t[1].model_id),
                       t[1].model_id))


def rank_targets(targets: list[tuple[object, Model, GatewayModelHealth]], *,
                 category: str
                 ) -> list[tuple[object, Model, GatewayModelHealth]]:
    """Score + sort, descending. Ties keep their original (configured)
    order — Python's sort is stable and every tie also gets an explicit,
    small `priority_bonus` favoring earlier configuration, so "configured
    priority" (§9) is honoured even without any other signal."""
    # Image categories use the deterministic serial-fallback order
    # (astra.ai.image_models priority) instead of health/latency scoring.
    if category in ("image_generation", "image_editing"):
        return rank_image_targets(targets)
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
