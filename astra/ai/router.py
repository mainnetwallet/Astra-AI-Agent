"""AgentRouter — Astra's central AI routing core.

NOT a provider. It holds no API keys, no model catalog of its own, no base URL
and no `AGENTROUTER_*` config. It receives a task, classifies it, scores every
eligible (adapter, model) candidate, executes with per-credential retry and
cross-provider fallback, measures latency, records a normalized results and
learns from outcomes so future routes improve.

Classify → score → execute → retry/fallback → record → learn.

Backward compatibility is preserved: `route(messages, capability=…)` still
returns `(provider, model, reply)` exactly as before.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from astra.ai.credentials import CredentialPool
from astra.ai.models import Model, metadata_for
from astra.ai.routing_policy import RoutingDecisionPolicy
from astra.core.exceptions import ProviderError, TimeoutError
from astra.core.timeutil import duration_ms, ms_now

STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type     TEXT DEFAULT '',
    provider      TEXT DEFAULT '',
    model         TEXT DEFAULT '',
    latency_ms    INTEGER DEFAULT 0,
    success       INTEGER DEFAULT 0,
    fallback      INTEGER DEFAULT 0,
    tokens        INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    tool_usage    TEXT DEFAULT '{}',
    route_reason  TEXT DEFAULT '{}',
    ts            TEXT DEFAULT ''
);
"""

TASK_TYPES = ("simple_chat", "reasoning", "research", "coding", "vision",
              "browser", "structured_output", "translation", "summarization",
              "planning", "tool_selection", "web3")

# adapter names that count as "configured AI" for the boot banner
NON_OFFLINE = ("offline",)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RoutingRequest:
    """Normalized input to the router (§6 of the spec)."""

    def __init__(self, task_type: str = "simple_chat", messages: list | None = None,
                 preferred_model: str | None = None, preferred_provider: str | None = None,
                 required_capabilities: list | None = None,
                 required_tools: list | None = None, context_tokens: int = 0,
                 max_latency_ms: int | None = None, max_cost_usd: float | None = None,
                 structured_output: bool = False, streaming: bool = False,
                 vision: bool = False, reasoning_level: str = "auto",
                 user_preference: str | None = None, max_tokens: int = 500):
        self.task_type = task_type
        self.messages = messages or []
        self.preferred_model = preferred_model
        self.preferred_provider = preferred_provider
        self.required_capabilities = list(required_capabilities or [])
        self.required_tools = list(required_tools or [])
        self.context_tokens = int(context_tokens or 0)
        self.max_latency_ms = max_latency_ms
        self.max_cost_usd = max_cost_usd
        self.structured_output = structured_output
        self.streaming = streaming
        self.vision = vision
        self.reasoning_level = reasoning_level
        self.user_preference = user_preference or "balanced"
        self.max_tokens = int(max_tokens or 500)

    def __repr__(self):
        return (f"RoutingRequest(task_type={self.task_type!r}, "
                f"preferred={self.preferred_provider}/{self.preferred_model}, "
                f"caps={self.required_capabilities!r}, "
                f"structured={self.structured_output}, stream={self.streaming})")


class RoutingResult:
    """Normalized outcome of one routing request (§6)."""

    def __init__(self, provider: str = "", model: str = "", text: str = "",
                 latency_ms: int = 0, usage: dict | None = None,
                 estimated_cost_usd: float = 0.0, attempts: int = 0,
                 fallback_used: bool = False, route_reason: dict | None = None,
                 ok: bool = False, error: str = "", _reason: str = ""):
        self.provider = provider
        self.model = model
        self.text = text
        self.latency_ms = latency_ms
        self.usage = usage or {}
        self.estimated_cost_usd = estimated_cost_usd
        self.attempts = attempts
        self.fallback_used = fallback_used
        self.route_reason = route_reason
        self._reason = _reason
        self.ok = ok
        self.error = error

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "text": self.text,
                "latency_ms": self.latency_ms, "usage": self.usage,
                "estimated_cost_usd": self.estimated_cost_usd,
                "attempts": self.attempts, "fallback_used": self.fallback_used,
                "route_reason": self.route_reason, "ok": self.ok, "error": self.error}


def classify(text: str) -> str:
    """Task-type classification for a user message (deterministic)."""
    import re
    low = text.lower()
    if re.search(r"read|analyse|analyze|research|compare|report|what is|about", low):
        return "research"
    if re.search(r"\b(code|fix|test|debug|refactor|github|repo)\b", low):
        return "coding"
    if re.search(r"open .*website|navigate|click|browser|visit ", low):
        return "browser"
    if re.search(r"wallet|token|stake|send .*eth|contract|transaction|balance", low):
        return "web3"
    if re.search(r"image|photo|picture|screenshot|vision", low):
        return "vision"
    if re.search(r"\bsummarize\b|tl;dr|short version", low):
        return "summarization"
    if re.search(r"\btranslate\b|\banglish\b|\btranslate to\b|\bbangla\b|\bbangali\b", low):
        return "translation"
    if re.search(r"plan|workflow|steps to|how do i|schedule", low):
        return "planning"
    if re.search(r"json|table|csv|structured", low):
        return "structured_output"
    return "simple_chat"


class AgentRouter:
    """Central routing brain. Consumes adapters + model registry; is itself
    neither provider nor model catalog."""

    def __init__(self, providers: list | None = None, config=None,
                 max_retries: int = 2, backoff_s: float = 1.0,
                 registry=None, preference: str = "balanced", store=None):
        self.config = config
        self.max_retries = max(0, int((config and config.get("AI_MAX_RETRIES")) or max_retries))
        self.backoff_s = float((config and config.get("AI_BACKOFF")) or backoff_s)
        self.providers = list(providers or [])
        self.registry = registry
        self.store = store
        self.preference = preference or "balanced"
        self.policy = RoutingDecisionPolicy(stats=self._load_aggregate(),
                                            preference=self.preference)
        self._lock = threading.RLock()
        self._latency: dict[str, list[float]] = {}
        self._errors: dict[str, int] = {}
        self._cost_est: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._down: set[str] = set()
        self._last: dict = {}
        for p in self.providers:
            self._slots(p)
        if store is not None and store:
            store.install(STATS_SCHEMA)

    # -- plumbing -------------------------------------------------------------
    def _slots(self, provider) -> None:
        name = getattr(provider, "name", "provider")
        self._latency.setdefault(name, [])
        self._errors.setdefault(name, 0)
        self._calls.setdefault(name, 0)
        self._cost_est.setdefault(name, 0.0)

    def add(self, provider) -> None:
        self.providers.append(provider)
        self._slots(provider)

    def _provider_usable(self, provider) -> bool:
        pool = getattr(provider, "pool", None)
        if pool is not None:
            return bool(pool)                   # pool has healthy credentials
        try:
            return bool(provider.health_check())
        except Exception:
            return False

    def _adapter_models(self, adapter) -> list[Model]:
        mids = list(getattr(adapter, "models", []) or [])
        name = getattr(adapter, "name", "")
        out = []
        for mid in mids:
            m = (self.registry.get(name, mid) if self.registry else None)
            if m is None:
                meta = metadata_for(mid, name)
                meta.pop("provider", None)
                m = Model(name, mid, **meta)
            if getattr(m, "disabled", False):
                continue
            out.append(m)
        return out

    # -- candidate scoring ----------------------------------------------------
    def _candidates(self, req: RoutingRequest):
        out = []
        for adapter in self.providers:
            name = getattr(adapter, "name", "")
            if name in self._down or name == "offline":
                continue
            if not self._provider_usable(adapter):
                # genuinely-down providers are surfaced as "down" (health
                # probing discipline); the offline sentinel never is.
                if name != "offline":
                    self._down.add(name)
                continue
            adapter.health_info = self._provider_info(adapter)
            for model in self._adapter_models(adapter) or []:
                out.append((adapter, model))
        return out

    def _provider_info(self, adapter) -> dict:
        pool = getattr(adapter, "pool", None)
        if pool is not None and hasattr(pool, "summary"):
            try:
                s = pool.summary()
                if s["healthy"]:
                    state = "healthy"
                elif s["total_credentials"] == 0:
                    state = "not_configured"
                else:
                    state = "unhealthy"
                return {"name": getattr(adapter, "name", ""), "state": state}
            except Exception:
                pass
        try:
            ok = bool(adapter.health_check())
        except Exception:
            ok = False
        return {"name": getattr(adapter, "name", ""),
                "state": "healthy" if ok else
                ("unhealthy" if hasattr(adapter, "pool") and
                 getattr(adapter, "pool", None) else "not_configured")}

    # -- execution ------------------------------------------------------------
    def route_request(self, req: RoutingRequest) -> RoutingResult:
        candidates = self._candidates(req)
        if not candidates:
            return RoutingResult(ok=False, error="no eligible provider/model available")
        ranked = self.policy.rank(candidates, req) if self.policy else \
            [(0.0, c[0], c[1]) for c in candidates]
        results, attempts, fallback = [], 0, False
        for score, adapter, model in ranked:
            rr = self._attempt(adapter, model, req)
            attempts += 1
            if rr is not None and rr.ok:
                if attempts > 1:
                    fallback = True
                rr.fallback_used = fallback
                rr.route_reason = {
                    "task_type": req.task_type,
                    "score": score,
                    "preference": req.user_preference if not fallback else "fallback",
                    "reason": getattr(rr, "_reason", ""),
                }
                self._record_route(req, rr)
                return rr
            if rr is not None:
                results.append(rr.error)
        # everything failed → learn and report honestly
        last = RoutingResult(ok=False, error="; ".join(results) or
                             "all providers failed",
                             attempts=attempts, fallback_used=fallback)
        self._emit("ai.failed", provider=(results[-1] if results else ""))
        return last

    def _attempt(self, adapter, model: Model, req: RoutingRequest) -> RoutingResult | None:
        name = getattr(adapter, "name", "")
        self._emit("ai.started", provider=name, model=model.model_id)
        t0 = ms_now()
        last_error = ""
        # per-credential + per-model retries: a failed key rolls to the next
        # key on the same model, then same provider's next model, then provider.
        for attempt in range(1, self.max_retries + 2):
            try:
                if req.required_tools or req.structured_output:
                    text = adapter.chat(req.messages, model=model.model_id,
                                        max_tokens=req.max_tokens)
                    streamed = False
                else:
                    text = adapter.chat(req.messages, model=model.model_id,
                                        max_tokens=req.max_tokens)
                    streamed = False
                ms = duration_ms(t0)
                cost = self._estimate_cost_adapter(adapter, text)
                self._latency[name].append(ms)
                self._calls[name] += 1
                self._cost_est[name] = self._cost_est.get(name, 0.0) + cost
                if name in self._down:
                    self._down.discard(name)
                self._last = {"provider": name, "model": model.model_id,
                              "latency_ms": ms, "task_type": req.task_type}
                rr = RoutingResult(provider=name, model=model.model_id, text=text,
                                   latency_ms=ms, estimated_cost_usd=cost,
                                   attempts=attempt, ok=True,
                                   usage=getattr(adapter, "_last_usage", None) or {},
                                   _reason=("matched preference" if not attempt else
                                            f"retry #{attempt}"))
                self._emit("ai.completed", provider=name, model=model.model_id,
                           latency_ms=ms)
                return rr
            except (ProviderError, TimeoutError) as e:
                last_error = e.message or getattr(e, "category", type(e).__name__)
                self._errors[name] = self._errors.get(name, 0) + 1
                self._emit("ai.failed", provider=name, model=model.model_id,
                           error=last_error, attempt=attempt)
                # the adapter's credential pool has already cooled the bad key;
                # a fresh key on the same model may succeed, so keep retrying up
                # to max_retries, respecting backoff only for transient errors.
                if attempt <= self.max_retries and getattr(e, "retryable", True):
                    time.sleep(min(self.backoff_s * attempt, 8))
            except Exception as e:           # never let a provider kill routing
                last_error = f"{type(e).__name__}: {e}"
                self._errors[name] = self._errors.get(name, 0) + 1
                self._emit("ai.failed", provider=name, error=last_error)
                if attempt <= self.max_retries:
                    time.sleep(min(self.backoff_s * attempt, 8))
        self._mark_down(name, last_error)
        return RoutingResult(ok=False, error=f"{name}: {last_error}",
                             attempts=0)

    def _mark_down(self, name: str, error: str) -> None:
        """Flag a provider down only when it's genuinely down (not merely a
        rate-limit blip on one key)."""
        self._down.add(name)

    # -- streaming ------------------------------------------------------------
    def stream_request(self, req: RoutingRequest):
        """Yield normalized text chunks from the best eligible candidate."""
        for score, adapter, model in self._candidates_ranked(req):
            try:
                chunks = adapter.stream(req.messages, model=model.model_id,
                                        max_tokens=req.max_tokens)
                for c in chunks:
                    yield c
                return
            except Exception:
                continue
        return

    def _candidates_ranked(self, req):
        candidates = self._candidates(req)
        if self.policy:
            return self.policy.rank(candidates, req)
        return [(0.0, c[0], c[1]) for c in candidates]

    # -- legacy compat --------------------------------------------------------
    def ordered(self, capability: str | None = None) -> list:
        order = self.providers
        pref = self.config.getlist("AI_PROVIDER") if self.config else []
        if pref:
            by_name = {getattr(p, "name", "?"): p for p in order}
            order = [by_name[n] for n in pref if n in by_name] + \
                    [p for p in order if getattr(p, "name", "?") not in pref]
        if capability:
            order = [p for p in order if capability in (p.capabilities or ["chat"])]
        return [p for p in order
                if getattr(p, "name", "?") not in self._down and
                self._provider_usable(p)]

    def route(self, messages: list[dict], *, capability: str | None = None,
              max_tokens: int | None = None) -> tuple:
        """Legacy tuple interface, now routed through the full pipeline."""
        req = RoutingRequest(task_type=capability or "simple_chat",
                             messages=messages, max_tokens=max_tokens or 500)
        rr = self.route_request(req)
        return (rr.provider or None, rr.model or None,
                rr.text if rr.ok else None)

    # -- learning -------------------------------------------------------------
    def _record_route(self, req: RoutingRequest, rr: RoutingResult) -> None:
        with self._lock:
            key = f"{(rr.provider or '?' )}:{(rr.model or '?')}"
            agg = self.policy.stats
            row = agg.setdefault(key, {"provider": rr.provider, "model": rr.model,
                                       "calls": 0, "successes": 0, "errors": 0,
                                       "avg_latency_ms": 0.0,
                                       "success_rate": None})
            row["calls"] = row.get("calls", 0) + 1
            if rr.ok:
                row["successes"] = row.get("successes", 0) + 1
            else:
                row["errors"] = row.get("errors", 0) + 1
            calls = row["calls"]
            row["success_rate"] = round(row["successes"] / calls, 2)
            row["avg_latency_ms"] = round(
                ((row.get("avg_latency_ms") or 0) * (calls - 1) + rr.latency_ms) / calls, 1)
            key_by_task = f"{req.task_type}:{rr.provider}"
            trow = agg.setdefault("task:" + key_by_task,
                                  {"calls": 0, "successes": 0, "errors": 0})
            trow["calls"] += 1
            trow["successes" if rr.ok else "errors"] += 1
            if self.store:
                try:
                    self.store.insert(
                        "routing_stats", task_type=req.task_type,
                        provider=rr.provider or "", model=rr.model or "",
                        latency_ms=int(rr.latency_ms), success=1 if rr.ok else 0,
                        fallback=1 if rr.fallback_used else 0,
                        tokens=int(rr.usage.get("total_tokens") or 0),
                        estimated_cost_usd=round(rr.estimated_cost_usd, 6),
                        tool_usage="{}", route_reason="{}", ts=_now())
                except Exception:
                    pass

    def _load_aggregate(self) -> dict:
        if not self.store:
            return {}
        try:
            rows = self.store.fetch(
                "SELECT provider, model, COUNT(*) n, SUM(success) s, "
                "AVG(latency_ms) lat FROM routing_stats "
                "GROUP BY provider, model")
            agg = {}
            for r in rows:
                agg[f"{r['provider']}:{r['model']}"] = {
                    "provider": r["provider"], "model": r["model"],
                    "calls": r["n"], "successes": r["s"] or 0,
                    "errors": (r["n"] or 0) - (r["s"] or 0),
                    "avg_latency_ms": round(r["lat"] or 0, 1),
                    "success_rate": round((r["s"] or 0) / r["n"], 2) if r["n"] else None}
            return agg
        except Exception:
            return {}

    def routing_stats(self) -> dict:
        return {k: v for k, v in self.policy.stats.items() if not k.startswith("task:")}

    def task_stats(self) -> dict:
        return {k[5:]: v for k, v in self.policy.stats.items() if k.startswith("task:")}

    # -- introspection (legacy contract, secret-free) -------------------------
    def health(self) -> dict:
        out = {}
        for p in self.providers:
            name = getattr(p, "name", "?")
            info = self._provider_info(p)
            models = list(getattr(p, "models", []) or [])
            lat = self._latency[name]
            out[name] = {
                "healthy": info["state"] == "healthy",
                "state": info["state"],
                "models": models,
                "latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else None,
                "calls": self._calls.get(name, 0),
                "errors": self._errors.get(name, 0),
                "cost_usd": round(self._cost_est.get(name, 0.0), 6),
                "credentials": self._credential_count(p),
            }
        return out

    def _credential_count(self, provider) -> int:
        pool = getattr(provider, "pool", None)
        if pool is not None and hasattr(pool, "summary"):
            try:
                return pool.summary().get("healthy", 0)
            except Exception:
                return 0
        try:
            return 1 if provider.health_check() else 0
        except Exception:
            return 0

    def stats(self) -> dict:
        return {"providers": self.health(), "last_route": self._last,
                "down": sorted(self._down),
                "router": {"preference": self.preference,
                           "task_stats": self.task_stats()}}

    def last_route(self) -> dict:
        """The most recent route decision, as {provider, model, ...}.

        Safe to call at any time; returns the last provider/model actually
        attempted (empty dict if the router has never run).
        """
        return dict(self._last)

    # -- cost -----------------------------------------------------------------
    @staticmethod
    def _estimate_cost(reply: str) -> float:
        return max(0.0, len(reply) / 4 * 0.25e-6)

    @staticmethod
    def _estimate_cost_adapter(adapter, text: str) -> float:
        fn = getattr(adapter, "estimate_cost", None)
        if callable(fn):
            try:
                return float(fn(text))
            except Exception:
                pass
        return max(0.0, len(text) / 4 * 0.25e-6)

    # -- events ---------------------------------------------------------------
    def _emit(self, kind: str, **data):
        events = getattr(self, "_events", None)
        if events:
            events.emit(kind, agent="router", **data)

    def attach_events(self, events) -> None:
        self._events = events