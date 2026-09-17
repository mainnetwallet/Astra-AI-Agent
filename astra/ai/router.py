"""AstraRouter — Astra's central AI routing core.

NOT a provider. It holds no API keys, no model catalog and no base URL of
its own. It receives a task, classifies it, scores every eligible (adapter,
model) candidate, executes with per-credential retry and cross-provider
fallback, measures latency, records a normalized result and learns from
outcomes so future routes improve.

An optional `Astra AI Gateway` may be attached as `self.gateway` (see
astra/ai/gateway.py). Beyond separate status reporting via
`gateway_health()`, an attached Gateway also owns real-time target
selection and failure recovery for THIS router's own (adapter, model)
candidates (§2-§12: `_route_via_gateway` asks it who to try next, and its
classify → cooldown → select decision is what actually gets dispatched —
not just recorded after the fact). This is still strictly one-directional
and credential-free: the Gateway only ever receives/returns sanitized
`ProviderExecutionTarget` provider_id/model_id metadata (astra/ai/
gateway_contract.py) and never an adapter, a credential or ProviderRegistry
itself. The Gateway is a completely independent system with its
own four AI connections — Gemini, Groq, Cloudflare, Bedrock — each with
independent credentials/models/endpoints (GW_* config). It is never added to
`self.providers`, never appears in ProviderRegistry, provider health, or the
provider dashboard table, and — just as important — AstraRouter NEVER
executes a routing request against it. Provider routing/retry/fallback in
this file only ever moves between the real provider adapters in
`self.providers`; when every one of them fails, routing fails honestly
instead of dropping down into the Gateway. The Gateway has its own separate
execution path (`AstraAIGateway.chat()` in gateway.py) with its own internal
fallback across its four connections — that is the only fallback chain the
Gateway ever participates in. The two systems share no code path: Provider →
Gateway and Gateway → ProviderRegistry are both absent by design.

Classify → score → execute → retry/fallback → record → learn.

Backward compatibility is preserved: `route(messages, capability=…)` still
returns `(provider, model, reply)` exactly as before.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from astra.ai.credentials import CredentialPool
from astra.ai.gateway_contract import ProviderExecutionPort
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
              "planning", "tool_selection", "web3",
              "audio", "video", "image_generation", "multimodal")

TASK_HARD_CAPABILITIES = {
    "coding": ("coding",),
    "vision": ("vision",),
    "structured_output": ("json",),
    "translation": ("translation",),
    "tool_selection": ("tools",),
}

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
                 user_preference: str | None = None, max_tokens: int = 500,
                 no_fallback: bool = False, task_contract=None,
                 evidence: dict | None = None, semantic_verifier=None,
                 required_input_modalities: list | None = None,
                 required_output_modalities: list | None = None):
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
        self.no_fallback = bool(no_fallback)
        self.task_contract = task_contract
        self.evidence = evidence
        self.semantic_verifier = semantic_verifier
        self.required_input_modalities = list(required_input_modalities or [])
        self.required_output_modalities = list(required_output_modalities or [])

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
                 ok: bool = False, error: str = "", _reason: str = "",
                 requested_provider: str = "", requested_model: str = "",
                 fallback_reason: str = "", completion_status: str = ""):
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
        # §11: what the caller asked for vs. what actually served the
        # request, plus *why* a fallback happened (a §7 failure category,
        # when one is known) — so callers can audit fallback decisions.
        self.requested_provider = requested_provider
        self.requested_model = requested_model
        self.fallback_reason = fallback_reason
        # §8 final result gate: set only when a Task Completion Contract
        # was supplied (RoutingRequest.task_contract) and Gateway task
        # supervision actually ran; empty otherwise (no behavior change
        # for existing callers). One of COMPLETE/INCOMPLETE/FAILED/
        # UNCERTAIN from astra.ai.gateway_task_completion.
        self.completion_status = completion_status

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "text": self.text,
                "latency_ms": self.latency_ms, "usage": self.usage,
                "estimated_cost_usd": self.estimated_cost_usd,
                "attempts": self.attempts, "fallback_used": self.fallback_used,
                "route_reason": self.route_reason, "ok": self.ok, "error": self.error,
                "requested_provider": self.requested_provider,
                "requested_model": self.requested_model,
                "fallback_reason": self.fallback_reason,
                "completion_status": self.completion_status}


def classify(text: str) -> str:
    """Task-type classification for a user message (deterministic)."""
    import re
    low = text.lower()
    if re.search(r"generate\s+(an?\s+)?image|create\s+(an?\s+)?image|draw\s|make\s+(an?\s+)?picture", low):
        return "image_generation"
    if re.search(r"generate\s+(an?\s+)?audio|create\s+(an?\s+)?audio|text.to.speech|tts\b", low):
        return "audio"
    if re.search(r"generate\s+(an?\s+)?video|create\s+(an?\s+)?video", low):
        return "video"
    if re.search(r"\[.*file.*attached\]|\[.*attachment", low):
        return "multimodal"
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


class _RouterExecutionPort(ProviderExecutionPort):
    """Concrete `ProviderExecutionPort` (§3): executes a
    `ProviderExecutionTarget` against the Existing Provider system's real
    adapter via `AstraRouter._attempt`. This is the ONLY bridge that lets
    Gateway-owned result supervision (astra.ai.gateway_supervision) send a
    correction back to a real Provider — Gateway code itself never imports
    an adapter class, credential, or ProviderRegistry; it only ever calls
    `port.execute(target, messages, max_tokens)` on whatever it's handed.

    `no_fallback=True` on the request built here is deliberate (§11): a
    correction round-trip must land on the SAME (provider, model) target
    it was given, never silently substitute a different one — a different
    target is `recover_execution_target`'s job, not this one's.
    """

    def __init__(self, router: "AstraRouter", by_key: dict, req: RoutingRequest):
        self._router = router
        self._by_key = by_key
        self._req = req

    def execute(self, target, messages: list, max_tokens: int = 500,
                **kwargs) -> str:
        entry = self._by_key.get(target.key())
        if entry is None:
            raise ProviderError(
                f"no adapter mapped for {target.provider_id}/{target.model_id}")
        _score, adapter, model = entry
        corrected_req = RoutingRequest(
            task_type=self._req.task_type, messages=messages,
            preferred_provider=target.provider_id,
            preferred_model=target.model_id,
            required_capabilities=self._req.required_capabilities,
            required_tools=self._req.required_tools,
            structured_output=self._req.structured_output,
            max_tokens=max_tokens, no_fallback=True)
        rr = self._router._attempt(adapter, model, corrected_req)
        if rr is None or not rr.ok:
            raise ProviderError(
                rr.error if rr is not None else "correction execution failed")
        return rr.text


class AstraRouter:
    """Central routing brain. Consumes adapters + model registry; is itself
    neither provider nor model catalog."""

    def __init__(self, providers: list | None = None, config=None,
                 max_retries: int = 2, backoff_s: float = 1.0,
                 registry=None, preference: str = "balanced", store=None,
                 gateway=None):
        self.config = config
        self.max_retries = max(0, int((config and config.get("AI_MAX_RETRIES")) or max_retries))
        self.backoff_s = float((config and config.get("AI_BACKOFF")) or backoff_s)
        self.providers = list(providers or [])
        self.registry = registry
        self.store = store
        self.preference = preference or "balanced"
        # optional Astra AI Gateway — deliberately NOT part of
        # self.providers / ProviderRegistry (see module docstring).
        self.gateway = gateway
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
        if self.gateway is not None:
            self._slots(self.gateway)
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
            if not self._provider_usable(adapter):
                self._down.add(name)
                continue
            if name in self._down:
                # Self-heal: it was down (cooldown/rate-limit/transient
                # failure) but is usable again right now — clear the flag
                # instead of leaving it excluded forever. Previously,
                # anything in `_down` was skipped before this check ever
                # ran, so a provider could never recover on its own once
                # marked down.
                self._down.discard(name)
                self._emit("provider.health_changed", provider=name,
                           healthy=True, reason="recovered")
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
    def _normalize_requirements(self, req: RoutingRequest) -> None:
        """Fill in hard capability requirements implied by task_type, without
        overriding anything the caller already set explicitly."""
        if req.task_type == "vision":
            req.vision = True
        if req.task_type == "structured_output":
            req.structured_output = True
        if not req.required_capabilities:
            hard = TASK_HARD_CAPABILITIES.get(req.task_type)
            if hard:
                req.required_capabilities = list(hard)

    def route_request(self, req: RoutingRequest) -> RoutingResult:
        self._normalize_requirements(req)
        self._emit("router.request", task=req.task_type)
        # Strict mandatory-Gateway enforcement (Gap 1 defense-in-depth):
        # `req.task_contract` is how a caller (Planner, or the post-
        # execution final-verification pass in Orchestrator) declares
        # "this is a normal AI request that MUST be Gateway-verified."
        # `build_astra_ai_gateway` now always returns a real Gateway
        # instance in production (see its docstring), so `self.gateway`
        # being None here should never actually happen for a real
        # deployment — but if some caller ever constructs an AstraRouter
        # without wiring a Gateway in at all, a contract-bearing request
        # must fail closed and say so, not silently fall through to the
        # plain loop below and report a false, unverified "ok=True".
        # A plain request with no contract (task_contract is None) is
        # completely unaffected — that is today's behavior, unchanged.
        if req.task_contract is not None and self.gateway is None:
            return RoutingResult(
                ok=False,
                error="Astra AI Gateway is not attached to this router: a "
                      "task-completion-verified request cannot be routed "
                      "without the mandatory Gateway control layer",
                requested_provider=req.preferred_provider or "",
                requested_model=req.preferred_model or "")
        candidates = self._candidates(req)
        if not candidates:
            return RoutingResult(ok=False, error="no eligible provider/model available",
                                 requested_provider=req.preferred_provider or "",
                                 requested_model=req.preferred_model or "")
        ranked = self.policy.rank(candidates, req) if self.policy else \
            [(0.0, c[0], c[1]) for c in candidates]

        # §11: an explicit "use exactly this model, no fallback" request
        # only ever gets that one target — never silently substituted.
        if req.no_fallback and req.preferred_model:
            ranked = [r for r in ranked if r[2].model_id == req.preferred_model and
                     (not req.preferred_provider or
                      getattr(r[1], "name", "") == req.preferred_provider)]
            if not ranked:
                return RoutingResult(
                    ok=False, requested_provider=req.preferred_provider or "",
                    requested_model=req.preferred_model,
                    error=f"requested model {req.preferred_model!r} unavailable "
                          f"and no_fallback=True: failing honestly instead of "
                          f"substituting another model")

        considered = len(ranked)

        # §2-§12, §17: when a Gateway is attached, its select/recover
        # decision — not just `ranked`'s static index order — actually
        # decides which (adapter, model) is attempted next. A target the
        # Gateway has cooled down from a previous failure is genuinely
        # skipped; on a fresh failure, the Gateway classifies it and picks
        # the next target, and THAT is what gets attempted here.
        if self.gateway is not None:
            return self._route_via_gateway(req, ranked, considered)

        results, attempts, fallback = [], 0, False
        last_failure_category = ""
        for score, adapter, model in ranked:
            rr = self._attempt(adapter, model, req)
            attempts += 1
            if rr is not None and rr.ok:
                if attempts > 1:
                    fallback = True
                rr.fallback_used = fallback
                rr.requested_provider = req.preferred_provider or ""
                rr.requested_model = req.preferred_model or ""
                rr.fallback_reason = last_failure_category if fallback else ""
                rr.route_reason = {
                    "task_type": req.task_type,
                    "score": score,
                    "preference": req.user_preference if not fallback else "fallback",
                    "reason": getattr(rr, "_reason", ""),
                    "candidates_considered": considered,
                }
                self._emit("router.decision", task=req.task_type, provider=rr.provider,
                           model=rr.model, score=score, reason=rr.route_reason.get("reason", ""),
                           latency_ms=rr.latency_ms, fallback=fallback)
                if fallback:
                    self._emit("router.fallback", task=req.task_type, provider=rr.provider,
                               model=rr.model, candidates_considered=considered,
                               fallback_from=req.preferred_model or "",
                               fallback_reason=last_failure_category)
                self._record_route(req, rr)
                return rr
            if rr is not None:
                results.append(rr.error)
        # every real provider candidate failed → routing fails honestly.
        # The Astra AI Gateway is a completely separate system (see module
        # docstring) and is NEVER used as a fallback here, even when every
        # provider candidate above has failed — isolation is absolute in both
        # directions: Provider routing never drops into the Gateway, and the
        # Gateway (astra/ai/gateway.py) never reads from or falls back into
        # ProviderRegistry.
        last = RoutingResult(ok=False, error="; ".join(results) or
                             "all providers failed",
                             attempts=attempts, fallback_used=fallback,
                             requested_provider=req.preferred_provider or "",
                             requested_model=req.preferred_model or "",
                             fallback_reason=last_failure_category)
        self._emit("ai.failed", provider=(results[-1] if results else ""))
        return last

    # -- Gateway-DRIVEN execution loop (§2-§12, §17-§18) -----------------------
    # This is the real runtime wiring: `_execution_targets`/
    # `_report_gateway_recovery` used to build sanitized targets and report
    # outcomes AFTER the router had already picked, in its own static
    # `ranked` order, who to call — the Gateway's select/recover API was
    # only ever exercised directly by unit tests, never by this loop, so a
    # target the Gateway had cooled down was still attempted again on the
    # very next request. Now the Gateway's decision IS the loop: each
    # iteration asks the Gateway "who's next" (first `select_execution_target`,
    # then `recover_execution_target` after every failure) and dispatches to
    # whatever `ProviderExecutionTarget` it returns, mapped back to the real
    # (adapter, model) pair via `by_key` — the Gateway itself never touches
    # an adapter, a credential or `ProviderRegistry` (§2/§3): it only ever
    # hands back provider_id/model_id, and this method does the actual
    # `self._attempt(adapter, model, req)` call against the Existing
    # Provider system. Fail-open (module docstring): if the Gateway API
    # itself raises mid-loop, remaining candidates are drained in plain
    # `ranked` order instead of failing the whole request — a Gateway bug
    # must never break otherwise-working routing.
    def _route_via_gateway(self, req: RoutingRequest, ranked: list,
                           considered: int) -> RoutingResult:
        from astra.ai.gateway_contract import classify_execution_failure

        gw_targets = self._execution_targets(ranked)
        by_key = {t.key(): (score, adapter, model)
                  for t, (score, adapter, model) in zip(gw_targets, ranked)}
        tried: set = set()
        results, attempts, fallback = [], 0, False
        last_failure_category = ""
        gateway_ok = True

        try:
            current = self.gateway.select_execution_target(
                gw_targets, required_capabilities=req.required_capabilities)
        except Exception:
            gateway_ok, current = False, None

        while attempts < considered:
            if current is None:
                if gateway_ok:
                    break  # Gateway: nothing eligible left (all excluded/cooling down)
                remaining = [t for t in gw_targets if t.key() not in tried]
                if not remaining:
                    break
                current = remaining[0]        # fail-open drain, plain order

            score, adapter, model = by_key[current.key()]
            rr = self._attempt(adapter, model, req)
            attempts += 1
            tried.add(current.key())

            if rr is not None and rr.ok:
                if attempts > 1:
                    fallback = True
                rr.fallback_used = fallback
                rr.requested_provider = req.preferred_provider or ""
                rr.requested_model = req.preferred_model or ""
                rr.fallback_reason = last_failure_category if fallback else ""
                rr.route_reason = {
                    "task_type": req.task_type,
                    "score": score,
                    "preference": req.user_preference if not fallback else "fallback",
                    "reason": getattr(rr, "_reason", ""),
                    "candidates_considered": considered,
                }
                if gateway_ok:
                    self._report_gateway_recovery(current, success=True,
                                                  latency_ms=rr.latency_ms)
                    # §6-§12: Gateway-owned result validation + bounded
                    # correction — same target, no failover. See
                    # _maybe_supervise_result's docstring for when this
                    # actually does anything.
                    self._maybe_supervise_result(current, rr, req, by_key)
                self._emit("router.decision", task=req.task_type, provider=rr.provider,
                           model=rr.model, score=score, reason=rr.route_reason.get("reason", ""),
                           latency_ms=rr.latency_ms, fallback=fallback)
                if fallback:
                    self._emit("router.fallback", task=req.task_type, provider=rr.provider,
                               model=rr.model, candidates_considered=considered,
                               fallback_from=req.preferred_model or "",
                               fallback_reason=last_failure_category)
                self._record_route(req, rr)
                return rr

            error = rr.error if rr is not None else "execution failed"
            results.append(error)
            last_failure_category = classify_execution_failure(message=error)

            if gateway_ok:
                try:
                    current = self.gateway.recover_execution_target(
                        gw_targets, current, last_failure_category,
                        required_capabilities=req.required_capabilities,
                        exclude=tried)
                except Exception:
                    gateway_ok, current = False, None
            else:
                current = None

        # every real provider candidate failed → routing fails honestly.
        # The Astra AI Gateway is a completely separate system (see module
        # docstring) and is NEVER used as a fallback here, even when every
        # provider candidate above has failed — isolation is absolute in both
        # directions: Provider routing never drops into the Gateway, and the
        # Gateway (astra/ai/gateway.py) never reads from or falls back into
        # ProviderRegistry.
        last = RoutingResult(ok=False, error="; ".join(results) or
                             "all providers failed",
                             attempts=attempts, fallback_used=fallback,
                             requested_provider=req.preferred_provider or "",
                             requested_model=req.preferred_model or "",
                             fallback_reason=last_failure_category)
        self._emit("ai.failed", provider=(results[-1] if results else ""))
        return last

    # -- Gateway execution-recovery reporting (§3-§5; additive, fail-open) ----
    def _execution_targets(self, ranked: list) -> list:
        """Build sanitized ProviderExecutionTarget metadata for each ranked
        (score, adapter, model) candidate, in the same order — never an
        adapter/credential object, just plain ids (§3)."""
        from astra.ai.gateway_contract import ProviderExecutionTarget
        out = []
        for _score, adapter, model in ranked:
            out.append(ProviderExecutionTarget(
                provider_id=getattr(adapter, "name", ""),
                model_id=model.model_id,
                capabilities=tuple(getattr(model, "capabilities", []) or [])))
        return out

    def _report_gateway_recovery(self, target, *, success: bool,
                                 latency_ms: int = 0, error: str = "") -> str:
        """Best-effort report to the attached Gateway's recovery API. Never
        raises into the caller — a Gateway hiccup must never affect routing.
        Returns the classified §7 category on failure (empty on success),
        so the caller can attach it to `fallback_reason`."""
        if self.gateway is None:
            return ""
        try:
            if success:
                self.gateway.report_execution_success(target, latency_ms=latency_ms)
                return ""
            from astra.ai.gateway_contract import classify_execution_failure
            category = classify_execution_failure(message=error)
            self.gateway.report_execution_failure(target, category)
            return category
        except Exception:
            return ""

    # -- Gateway-owned result supervision (§6-§12; additive, fail-open) -------
    def _maybe_supervise_result(self, target, rr: RoutingResult,
                                req: RoutingRequest, by_key: dict) -> None:
        """After a successful attempt, give the attached Gateway a chance to
        deterministically validate the response and — if it's invalid or
        incomplete — drive a bounded correction round-trip back through
        `_RouterExecutionPort` to the SAME `target` (never a different
        provider/model; recovery/failover is `recover_execution_target`'s
        job, not this one's).

        A no-op for the common case (§36: don't drag a simple request into
        supervision it never needs) — only actually asks the Gateway to
        look when the caller wanted structured output (a genuine
        deterministic JSON-shape check) or the reply came back empty (a
        genuine deterministic "did we get anything at all" check). Fully
        fail-open: any Gateway-side exception here leaves `rr` exactly as
        `_attempt` produced it — a Gateway bug must never turn an
        already-successful response into a failure.
        """
        # §1-§8: when the caller supplied a Task Completion Contract, that
        # path takes over entirely (it already does its own deterministic
        # shape check, plus evidence/semantic checks) — never run both
        # supervisors over the same response.
        if req.task_contract is not None and hasattr(self.gateway, "supervise_task"):
            try:
                from astra.ai.gateway_contract import ProviderExecutionResult
                port = _RouterExecutionPort(self, by_key, req)
                result = ProviderExecutionResult(ok=True, text=rr.text)
                supervised, outcome, attempts = self.gateway.supervise_task(
                    port, target, req.messages, result, req.task_contract,
                    evidence=req.evidence, semantic_verifier=req.semantic_verifier,
                    max_tokens=req.max_tokens)
                if supervised.text != rr.text:
                    rr.text = supervised.text
                rr.completion_status = outcome.status
                self._emit("router.gateway_task_completion", task=req.task_type,
                           provider=target.provider_id, model=target.model_id,
                           status=outcome.status, reason=outcome.reason,
                           attempts=attempts)
            except Exception:
                pass
            return

        if not hasattr(self.gateway, "supervise_execution"):
            return
        needs_check = req.structured_output or not (rr.text or "").strip()
        if not needs_check:
            return
        try:
            from astra.ai.gateway_contract import ProviderExecutionResult
            port = _RouterExecutionPort(self, by_key, req)
            result = ProviderExecutionResult(ok=True, text=rr.text)
            supervised, outcome = self.gateway.supervise_execution(
                port, target, req.messages, result,
                max_tokens=req.max_tokens, require_json=req.structured_output)
            if supervised.text != rr.text:
                rr.text = supervised.text
            self._emit("router.gateway_supervision", task=req.task_type,
                       provider=target.provider_id, model=target.model_id,
                       ok=outcome.ok, reason=outcome.reason)
        except Exception:
            pass

    # -- Astra AI Gateway (not a provider; reporting only — see module
    #    docstring: AstraRouter never executes a request against it) --------
    def _gateway_models(self) -> list[str]:
        return list(getattr(self.gateway, "models", []) or [])

    def gateway_health(self) -> dict:
        """Astra AI Gateway status, reported separately from provider health
        (never as a provider). Includes the per-connection health of its four
        AI connections (Gemini, Groq, Cloudflare, Bedrock)."""
        gw = self.gateway
        if gw is None:
            return {"state": "not_configured", "connections": []}
        detail = gw.health() if hasattr(gw, "health") else {}
        configured = len(detail)
        healthy = sum(1 for c in detail.values()
                      if c.get("state") == "healthy")
        if healthy == configured:
            state = "healthy"
        elif healthy > 0:
            state = "degraded"
        elif configured == 0:
            state = "not_configured"
        else:
            state = "unavailable"
        return {"state": state, "models": self._gateway_models(),
                "connections": detail}

    def _attempt(self, adapter, model: Model, req: RoutingRequest) -> RoutingResult | None:
        name = getattr(adapter, "name", "")
        self._emit("ai.started", provider=name, model=model.model_id)
        t0 = ms_now()
        last_error = ""
        # per-credential + per-model retries: a failed key rolls to the next
        # key on the same model, then same provider's next model, then provider.
        for attempt in range(1, self.max_retries + 2):
            if attempt > 1:
                self._emit("credential.rotation", provider=name, model=model.model_id,
                           attempt=attempt)
                self._emit("router.retry", provider=name, model=model.model_id,
                           attempt=attempt)
            try:
                # Multimodal dispatch: use specialized adapter methods
                # for non-chat output modalities (image generation, TTS)
                # before falling through to the normal chat path.
                out_mods = req.required_output_modalities or []
                if "image" in out_mods and hasattr(adapter, "generate_image"):
                    prompt = self._extract_prompt(req.messages)
                    text = adapter.generate_image(prompt, model=model.model_id)
                    streamed = False
                elif "audio" in out_mods and hasattr(adapter, "text_to_speech"):
                    prompt = self._extract_prompt(req.messages)
                    text = adapter.text_to_speech(prompt, model=model.model_id)
                    streamed = False
                elif req.required_tools or req.structured_output:
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

    @staticmethod
    def _extract_prompt(messages: list) -> str:
        """Extract the user's text prompt from messages for non-chat APIs."""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    return " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text")
                return str(content)
        return ""

    def _mark_down(self, name: str, error: str) -> None:
        """Flag a provider down only when it's genuinely down (not merely a
        rate-limit blip on one key)."""
        was_down = name in self._down
        self._down.add(name)
        if not was_down:
            self._emit("provider.health_changed", provider=name, healthy=False,
                       reason=error)

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

    def enable(self, name: str) -> None:
        """Re-enable a provider after an operator-disable or transient failure."""
        was_down = name in self._down
        self._down.discard(name)
        if was_down:
            self._emit("provider.health_changed", provider=name, healthy=True, reason="")

    def disable(self, name: str) -> None:
        """Operator force-disable: excluded from candidates; health reports down."""
        self._down.add(name)

    def reset_health(self, name: str) -> None:
        """Clear transient health bookkeeping (down-state, error counts,
        latency samples) for one provider without touching its pool."""
        self._down.discard(name)
        for by_name in (self._errors, self._latency):
            bucket = by_name.get(name)
            if isinstance(bucket, list):
                bucket.clear()
            elif isinstance(bucket, int):
                by_name[name] = 0

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
                "astra_ai_gateway": self.gateway_health(),
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