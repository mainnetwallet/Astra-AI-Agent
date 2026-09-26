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
own AI connections (Gemini, Groq, Cloudflare, Bedrock, OpenRouter,
Mistral, Cerebras, SambaNova, Cohere, Z.AI) — each with
independent credentials/models/endpoints (GW_* config). It is never added to
`self.providers`, never appears in ProviderRegistry, provider health, or the
provider dashboard table, and — just as important — AstraRouter NEVER
executes a routing request against it. Provider routing/retry/fallback in
this file only ever moves between the real provider adapters in
`self.providers`; when every one of them fails, routing fails honestly
instead of dropping down into the Gateway. The Gateway has its own separate
execution path (`AstraAIGateway.chat()` in gateway.py) with its own internal
fallback across its own connections — that is the only fallback chain the
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

from astra.ai.gateway_contract import ProviderExecutionPort
from astra.ai.models import Model, metadata_for
from astra.ai.shared_health import SharedHealthCoordinator, canonical_provider, resolve_identity
from astra.ai.token_limits import resolve_output_tokens
from astra.ai.routing_policy import RoutingDecisionPolicy
from astra.core.exceptions import ProviderError, TimeoutError
from astra.core.events import new_op_id
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

# Saved health of every (provider, API key, model) triple — which key was used
# for which model and whether it worked. Keys are identified by a non-secret
# fingerprint (see Credential.key_id), never by the key itself.
KEY_MODEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS key_model_health (
    provider   TEXT NOT NULL,
    key_id     TEXT NOT NULL,
    model      TEXT NOT NULL,
    key_label  TEXT DEFAULT '',
    ok         INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    error      TEXT DEFAULT '',
    source     TEXT DEFAULT 'live',
    tested_at  TEXT DEFAULT '',
    ts         REAL DEFAULT 0,
    PRIMARY KEY (provider, key_id, model)
);
"""

# A saved per-key/model result steers routing only while it is this fresh.
KEY_MODEL_TTL_S = 600.0

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
    # Image production is a hard capability, distinct from vision (image
    # understanding): only a model whose (provider, model) entry in
    # astra.ai.image_models proves it can produce image output is eligible.
    "image_generation": ("image_generation",),
    "image_editing": ("image_editing",),
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
                 user_preference: str | None = None, max_tokens: int | None = None,
                 no_fallback: bool = False, task_contract=None,
                 evidence: dict | None = None, semantic_verifier=None,
                 required_input_modalities: list | None = None,
                 required_output_modalities: list | None = None,
                 trace: str = ""):
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
        self.user_preference = user_preference or "fastest"
        # None => let the provider/model decide (no Astra-imposed cap).
        self.max_tokens = max_tokens
        self.no_fallback = bool(no_fallback)
        self.task_contract = task_contract
        self.evidence = evidence
        self.semantic_verifier = semantic_verifier
        self.required_input_modalities = list(required_input_modalities or [])
        self.required_output_modalities = list(required_output_modalities or [])
        # Correlation id of the higher-level operation this request belongs to
        # (e.g. a chat turn). Propagated onto every emitted lifecycle event so
        # the Activity Log can resolve a request's children when it ends.
        self.trace = trace or ""

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
    # Image-generation intent, in either word order: English tends to put
    # the verb first ("create an image"), but Banglish/Bangla phrasing
    # written in Latin script often puts the noun first ("photo create
    # koro", "akta chobi banao"). Matching only the first order was
    # silently falling through to the "vision" branch below, which sets
    # required_capabilities=["vision"] (image *understanding*) — the wrong
    # capability axis entirely for an image *generation* request — and
    # that hard-filters out every text-only model, failing the request
    # for a reason that has nothing to do with what the user actually
    # asked for. Bengali written in its own script (not just transliterated
    # Banglish) uses the same noun-then-verb order — "ছবি বানাও" (chobi
    # banao), "তৈরি করো" (toiri koro) — so the noun/verb token lists carry
    # both the Latin-script transliteration AND the Bengali-script word.
    # NOTE: the noun<->verb connector below does NOT use \b at the join —
    # Bengali dependent vowel signs ("ি" in ছবি, "ো" in ফোটো, "ৈ" in তৈরি,
    # Unicode category Mc) are not \w to Python's re engine, so a \b placed
    # right after a noun ending in one of these never matches and silently
    # kills the whole branch. The join only needs "nearby", not "exactly
    # adjacent", so requiring a hard word boundary there was never doing
    # useful work anyway — see test_multimodal.TestCapabilityRouting for
    # the exact phrases this must (and, for plain "কাজ করো"/"do work" with
    # no image noun in range, must not) match.
    _img_nouns = (r"(?:image|picture|photo|photograph|illustration|diagram|"
                 r"logo|icon|art|artwork|chobi|chhobi|ছবি|ফটো|ফোটো)")
    _img_edit_verbs = (r"(?:edit|editing|modify|retouch|inpaint|outpaint|"
                       r"restyle|এডিট|badle|badol|change)")
    _img_verbs = (r"(?:generate|create|draw|make|design|render|paint|"
                  r"banao|banawo|banai|baniye|banate|bana|banan|toiri|"
                  r"বানাও|বানান|বানাতে|তৈরি|তৈরী|করো)")
    # Editing an EXISTING image is its own task type, checked before both
    # generation and "vision": "ei photo ta edit kore dao" must never be
    # served by a model that only *understands* images.
    if re.search(_img_edit_verbs + r"\s+(?:this|the|my|ei|এই)?\s*" + _img_nouns, low) or \
       re.search(_img_nouns + r".{0,24}" + _img_edit_verbs, low):
        return "image_editing"
    if re.search(_img_verbs + r"\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,3}" + _img_nouns, low) or \
       re.search(_img_nouns + r".{0,24}" + _img_verbs, low):
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

    def execute(self, target, messages: list, max_tokens: int | None = None,
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
            # Carried over so a correction round-trip (this port is how
            # every gateway_task_completion/gateway_supervision retry
            # actually executes) still gets JSON mode + the higher token
            # floor in _attempt below — a plan-contract request whose
            # first reply wasn't JSON needs that on the retry even more
            # than on the first try.
            task_contract=self._req.task_contract,
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
                 registry=None, preference: str = "fastest", store=None,
                 gateway=None, shared_health=None):
        self.config = config
        self.max_retries = max(0, int((config and config.get("AI_MAX_RETRIES")) or max_retries))
        self.backoff_s = float((config and config.get("AI_BACKOFF")) or backoff_s)
        self.providers = list(providers or [])
        self.registry = registry
        self.store = store
        self.preference = preference or "fastest"
        # optional Astra AI Gateway — deliberately NOT part of
        # self.providers / ProviderRegistry (see module docstring).
        self.gateway = gateway
        # Manual-health-check dedup, shared with `self.gateway` (see
        # astra/ai/shared_health.py). Whichever of Router/Gateway is built
        # first "owns" the coordinator instance; the other adopts it here,
        # so the two always converge on ONE instance when wired together —
        # required for in-process concurrent-probe coordination — while
        # still working standalone (own instance) if used without the other.
        self.shared_health = (shared_health
                              or getattr(self.gateway, "shared_health", None)
                              or SharedHealthCoordinator(store=store))
        if self.gateway is not None:
            self.gateway.shared_health = self.shared_health
        self.policy = RoutingDecisionPolicy(stats=self._load_aggregate(),
                                            preference=self.preference)
        self._lock = threading.RLock()
        self._latency: dict[str, list[float]] = {}
        self._errors: dict[str, int] = {}
        self._cost_est: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._down: set[str] = set()
        self._last: dict = {}
        # (provider, key_id, model) -> {ok, latency_ms, error, source, tested_at, ts, key_label}
        self._key_model: dict[tuple, dict] = {}
        for p in self.providers:
            self._slots(p)
        if self.gateway is not None:
            self._slots(self.gateway)
        if store is not None and store:
            store.install(STATS_SCHEMA)
            store.install(KEY_MODEL_SCHEMA)
            self._load_key_model()

    # -- plumbing -------------------------------------------------------------
    def _slots(self, provider) -> None:
        name = getattr(provider, "name", "provider")
        self._latency.setdefault(name, [])
        self._errors.setdefault(name, 0)
        self._calls.setdefault(name, 0)
        self._cost_est.setdefault(name, 0.0)
        pool = getattr(provider, "pool", None)
        if pool is not None and hasattr(pool, "model_status"):
            pool.model_status = (
                lambda key_id, model, _n=name: self._key_model_state(_n, key_id, model))

    def add(self, provider) -> None:
        # Both steps under the lock: `health()` (any request thread, since
        # uvicorn serves requests concurrently) iterates self.providers and
        # indexes straight into self._latency[name] — if it observed the
        # provider appended here before _slots() had run, that indexing
        # raised KeyError and 500'd the whole /api/providers response
        # (which the UI then treated as "wipe the provider list").
        with self._lock:
            self.providers.append(provider)
            self._slots(provider)

    def _provider_usable(self, provider) -> bool:
        pool = getattr(provider, "pool", None)
        if pool is not None:
            if getattr(pool, "pinned_key", lambda: None)() and getattr(pool, "count", 0):
                return True     # a manual per-key test must reach even a disabled key
            return bool(pool)                   # pool has healthy credentials
        try:
            return bool(provider.health_check())
        except Exception:
            return False

    def _adapter_models(self, adapter, *,
                       discover_images: bool = False) -> list[Model]:
        mids = list(getattr(adapter, "models", []) or [])
        name = getattr(adapter, "name", "")
        # Image-generation models are configured separately from the chat
        # list; only ones the evidence registry recognizes are ever added, so
        # a stray id can never become an image candidate.
        from astra.ai.image_models import (FREE_TRUE, PROTOCOL_OPENROUTER_IMAGES,
                                           documented_image_models,
                                           is_image_model, is_free_image_model,
                                           make_image_spec)
        image_ids = []
        # Live image-model discovery (OpenRouter) only runs for an actual
        # image request -- an ordinary chat route must never pay for (or be
        # stalled by) a discovery HTTP call.
        list_fn = getattr(adapter, "list_image_models", None)
        if callable(list_fn):
            try:
                image_ids = list(list_fn(discover=discover_images) or [])
            except Exception:
                image_ids = []
        if not image_ids:
            image_ids = list(getattr(adapter, "image_models", None) or [])
        if not image_ids and discover_images:
            # Only an actual image request defaults to the registry, so a
            # registry image model can never become a chat/vision/coding
            # candidate. A non-empty env list always wins.
            image_ids = list(documented_image_models(name))
        live = set()
        live_fn = getattr(adapter, "live_image_models", None)
        if discover_images and callable(live_fn):
            try:
                live = set(live_fn(discover=True) or [])
            except Exception:
                live = set()
        image_overrides = {}
        for mid in image_ids:
            if mid in mids:
                continue
            if is_free_image_model(name, mid):
                mids.append(mid)
            elif mid in live:
                # The provider's own API reported a FREE image model for this
                # exact id -- authoritative even when the static registry has
                # no entry yet. A model that is not provably free stays out.
                image_overrides[mid] = make_image_spec(
                    name, mid, PROTOCOL_OPENROUTER_IMAGES,
                    capabilities=("image_generation",),
                    input_modalities=("text", "image"),
                    free_tier=FREE_TRUE,
                    free_evidence="provider live catalog: free image output")
                mids.append(mid)
        out = []
        for mid in mids:
            m = (self.registry.get(name, mid) if self.registry else None)
            if m is None:
                meta = metadata_for(
                    mid, name,
                    image_spec_override=image_overrides.get(mid))
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
            is_img = req.task_type in ("image_generation",
                                      "image_editing")
            for model in self._adapter_models(
                    adapter, discover_images=is_img) or []:
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
        # `classify()` already recognizes image-generation intent in both
        # English ("create an image") and Bangla/Banglish ("akta chobi
        # banao", "photo create koro") word orders and returns task_type
        # "image_generation". Execution for those task types is owned
        # EXCLUSIVELY by the Gateway's ImageRouter (see `route_request`'s
        # fail-closed guard, which refuses them before any candidate is
        # ranked). The derived `required_output_modalities` is kept so
        # `meets_hard_requirements()` (routing_policy.py) stays honest about
        # the task type: anything that inspects a normalized request —
        # candidate filtering, health reporting, a future caller — never sees
        # an image request as text-only. Set centrally here, mirroring the
        # `vision`/`structured_output` derivations above. A caller that
        # already set its own required_output_modalities (e.g. an explicit
        # audio/video request layered on top) is never overridden.
        if req.task_type in ("image_generation", "image_editing") \
                and not req.required_output_modalities:
            req.required_output_modalities = ["image"]

    def _route_end(self, req: RoutingRequest, op: str, kind: str, **data) -> None:
        """Emit the terminal event for one route operation, carrying the same
        `op` as its `router.request` so the Activity Log resolves the row."""
        self._emit(kind, task=req.task_type, op=op, trace=req.trace,
                   terminal=True, **data)

    def _route_failed(self, req: RoutingRequest, op: str, error: str,
                      **kw) -> RoutingResult:
        """Terminal failure for a route operation that never dispatched (or
        whose every candidate failed) — closes the 'Agent Router' row."""
        kw.pop("ok", None)
        kw.setdefault("requested_provider", req.preferred_provider or "")
        kw.setdefault("requested_model", req.preferred_model or "")
        self._route_end(req, op, "ai.failed", aggregate=True, error=error, **kw)
        return RoutingResult(ok=False, error=error, **kw)

    def route_request(self, req: RoutingRequest) -> RoutingResult:
        self._normalize_requirements(req)
        op = new_op_id()
        self._emit("router.request", task=req.task_type, op=op, trace=req.trace)
        # Strict mandatory-Gateway enforcement (Gap 1 defense-in-depth):
        # `req.task_contract` is how a caller (the chat pipeline, or any
        # post-execution final-verification pass) declares
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
            return self._route_failed(
                req, op,
                ok=False,
                error="Astra AI Gateway is not attached to this router: a "
                      "task-completion-verified request cannot be routed "
                      "without the mandatory Gateway control layer",
                requested_provider=req.preferred_provider or "",
                requested_model=req.preferred_model or "")
        # Image generation/editing is owned EXCLUSIVELY by the Gateway's
        # ImageRouter (astra/ai/image_router.py): IT performs the provider
        # dispatch, the serial fallback and the image API-call logging. The
        # Provider router must never execute an image request itself -- its
        # own serial image loop was a SECOND image execution owner and has
        # been removed. Fail closed here instead of ranking the request onto
        # a text/vision model, which would answer with a written description.
        if req.task_type in ("image_generation", "image_editing") or \
                "image" in (req.required_output_modalities or []):
            return self._route_failed(
                req, op,
                error="image generation is owned by the Gateway ImageRouter "
                      "(astra.ai.image_router); the Provider router does not "
                      "execute image requests")
        candidates = self._candidates(req)
        if not candidates:
            return self._route_failed(
                req, op, ok=False, error="no eligible provider/model available",
                requested_provider=req.preferred_provider or "",
                requested_model=req.preferred_model or "")
        ranked = self.policy.rank(candidates, req) if self.policy else \
            [(0.0, c[0], c[1]) for c in candidates]
        # Healthy first: a (provider, model) whose every key recently failed
        # for it goes behind the rest. Stable sort — the policy's own score
        # order is untouched within each group, and untested models are not
        # penalised (only *known-bad* ones move).
        ranked.sort(key=lambda r: 1 if self._model_status(r[1], r[2].model_id) == "failed" else 0)

        # §11: an explicit "use exactly this model, no fallback" request
        # only ever gets that one target — never silently substituted.
        if req.no_fallback and req.preferred_model:
            ranked = [r for r in ranked if r[2].model_id == req.preferred_model and
                     (not req.preferred_provider or
                      getattr(r[1], "name", "") == req.preferred_provider)]
            if not ranked:
                return self._route_failed(
                    req, op, requested_model=req.preferred_model,
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
            return self._route_via_gateway(req, ranked, considered, op)

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
                # The fallback notice is progress on the SAME route
                # operation, so it is emitted before the terminal decision
                # and carries the same `op`/`trace` — the Activity Log then
                # refines one "Agent Router" row instead of appending a
                # second, orphan one.
                if fallback:
                    self._emit("router.fallback", task=req.task_type, provider=rr.provider,
                               model=rr.model, candidates_considered=considered,
                               fallback_from=req.preferred_model or "",
                               fallback_reason=last_failure_category,
                               reason=last_failure_category or "switched to the next provider",
                               op=op, trace=req.trace, terminal=False)
                self._emit("router.decision", task=req.task_type, provider=rr.provider,
                           model=rr.model, score=score, reason=rr.route_reason.get("reason", ""),
                           latency_ms=rr.latency_ms, fallback=fallback,
                           op=op, trace=req.trace, terminal=True)
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
        # attempts == 0: nothing in `ranked` was tried at all — see the
        # matching comment in `_route_via_gateway` for why this needs the
        # same "no eligible provider/model available" wording as the
        # early `if not candidates` return above.
        if attempts == 0:
            error = "no eligible provider/model available"
        else:
            error = "; ".join(results) or "all providers failed"
        last = RoutingResult(ok=False, error=error,
                             attempts=attempts, fallback_used=fallback,
                             requested_provider=req.preferred_provider or "",
                             requested_model=req.preferred_model or "",
                             fallback_reason=last_failure_category)
        self._route_end(req, op, "ai.failed", aggregate=True, error=error,
                        attempts=attempts)
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
                           considered: int, op: str = "") -> RoutingResult:
        from astra.ai.gateway_contract import classify_execution_failure

        gw_targets = self._execution_targets(ranked)
        by_key = {t.key(): (score, adapter, model)
                  for t, (score, adapter, model) in zip(gw_targets, ranked)}
        tried: set = set()
        results, attempts, fallback = [], 0, False
        last_failure_category = ""
        last_completion_status = ""
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
                if gateway_ok:
                    self._report_gateway_recovery(current, success=True,
                                                  latency_ms=rr.latency_ms,
                                                  op=op, trace=req.trace)
                    # §6-§12: Gateway-owned result validation + bounded
                    # correction — same target first (see
                    # _maybe_supervise_result's docstring). If the Gateway
                    # ultimately can't confirm/correct the result (contract
                    # FAILED/INCOMPLETE/UNCERTAIN even after its own
                    # correction round-trip, or supervise_execution rejects
                    # it), this attempt is NOT returned as a success — it
                    # falls through to the ordinary failure path below,
                    # which tries the next ranked candidate/provider via
                    # recover_execution_target. Previously this always
                    # `return`ed here regardless of supervision outcome, so
                    # one bad/misconfigured top-ranked provider that still
                    # answered (just badly) could block every other
                    # configured provider from ever being tried.
                    supervised_ok = self._maybe_supervise_result(
                        current, rr, req, by_key, op=op)
                    if not supervised_ok:
                        if rr.completion_status:
                            last_completion_status = rr.completion_status
                        results.append(rr.error or "gateway supervision failed")
                        last_failure_category = classify_execution_failure(
                            message=rr.error or "")
                        try:
                            current = self.gateway.recover_execution_target(
                                gw_targets, current, last_failure_category,
                                required_capabilities=req.required_capabilities,
                                exclude=tried, op=op, trace=req.trace)
                        except Exception:
                            gateway_ok, current = False, None
                        continue
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
                if fallback:
                    self._emit("router.fallback", task=req.task_type, provider=rr.provider,
                               model=rr.model, candidates_considered=considered,
                               fallback_from=req.preferred_model or "",
                               fallback_reason=last_failure_category,
                               reason=last_failure_category or "switched to the next provider",
                               op=op, trace=req.trace, terminal=False)
                self._emit("router.decision", task=req.task_type, provider=rr.provider,
                           model=rr.model, score=score, reason=rr.route_reason.get("reason", ""),
                           latency_ms=rr.latency_ms, fallback=fallback,
                           op=op, trace=req.trace, terminal=True)
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
                        exclude=tried, op=op, trace=req.trace)
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
        #
        # `attempts == 0` here means nothing in `ranked` was ever actually
        # tried — every candidate was excluded up front (typically a hard
        # capability mismatch via meets_hard_requirements, e.g. no
        # configured model supports the required vision/output modality).
        # That is a materially different situation from "we called N
        # providers and each one failed", so it gets the same wording the
        # earlier `if not candidates` early-return already uses — callers
        # key off that exact phrase to give the
        # user an accurate "no configured Provider/Model supports this"
        # message instead of a generic, misleading one.
        if attempts == 0:
            error = "no eligible provider/model available"
        else:
            error = "; ".join(results) or "all providers failed"
        last = RoutingResult(ok=False, error=error,
                             attempts=attempts, fallback_used=fallback,
                             requested_provider=req.preferred_provider or "",
                             requested_model=req.preferred_model or "",
                             fallback_reason=last_failure_category)
        last.completion_status = last_completion_status
        self._route_end(req, op, "ai.failed", aggregate=True, error=error,
                        attempts=attempts)
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
                                 latency_ms: int = 0, error: str = "",
                                 op: str = "", trace: str = "") -> str:
        """Best-effort report to the attached Gateway's recovery API. Never
        raises into the caller — a Gateway hiccup must never affect routing.
        Returns the classified §7 category on failure (empty on success),
        so the caller can attach it to `fallback_reason`."""
        if self.gateway is None:
            return ""
        try:
            if success:
                self.gateway.report_execution_success(
                    target, latency_ms=latency_ms, op=op, trace=trace)
                return ""
            from astra.ai.gateway_contract import classify_execution_failure
            category = classify_execution_failure(message=error)
            self.gateway.report_execution_failure(
                target, category, op=op, trace=trace)
            return category
        except Exception:
            return ""

    # -- Gateway-owned result supervision (§6-§12; additive, fail-open) -------
    def _maybe_supervise_result(self, target, rr: RoutingResult,
                                req: RoutingRequest, by_key: dict,
                                op: str = "") -> bool:
        """After a successful attempt, give the attached Gateway a chance to
        deterministically validate the response and — if it's invalid or
        incomplete — drive a bounded correction round-trip back through
        `_RouterExecutionPort` to the SAME `target` (never a different
        provider/model for the correction messages themselves; recovery/
        failover across *providers* is `recover_execution_target`'s job,
        which the caller now invokes when this returns False).

        Returns True when the result is confirmed good (or supervision is
        a no-op for this request), False when the Gateway's own
        verify/correct/re-verify loop could not confirm success even after
        its bounded corrections — in which case `rr.ok`/`rr.error` are
        updated and the caller should treat this candidate as failed and
        move on to the next one, instead of returning it as the final
        answer.

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
                           attempts=attempts, op=op, trace=req.trace)
                # INCOMPLETE/UNCERTAIN with real text is a legitimate
                # best-effort answer (e.g. a multi-step task the model
                # partially finished) — the caller still gets it, same as
                # before this change, just tagged via completion_status.
                # Only a genuine execution FAILED (the port itself raised
                # during a correction round — credential/auth/network
                # errors, exactly what happens when a provider's key is
                # bad) or a response that ends up completely empty is
                # treated as this candidate having failed outright, so the
                # router falls over to the next configured provider
                # instead of surfacing a dead/misconfigured one's error.
                if outcome.status == "FAILED" or not (rr.text or "").strip():
                    rr.ok = False
                    rr.error = (f"{target.provider_id}: task completion "
                               f"{outcome.status.lower()}"
                               + (f" ({outcome.reason})" if outcome.reason else ""))
                    return False
                return True
            except Exception:
                return True  # fail-open: a supervision bug must not sink a good reply
            return True

        if not hasattr(self.gateway, "supervise_execution"):
            return True
        needs_check = req.structured_output or not (rr.text or "").strip()
        if not needs_check:
            return True
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
                       ok=outcome.ok, reason=outcome.reason,
                       op=op, trace=req.trace)
            if not outcome.ok:
                rr.ok = False
                rr.error = (f"{target.provider_id}: {outcome.reason or 'supervision rejected response'}")
                return False
            return True
        except Exception:
            return True  # fail-open: a supervision bug must not sink a good reply

    # -- Astra AI Gateway (not a provider; reporting only — see module
    #    docstring: AstraRouter never executes a request against it) --------
    def _gateway_models(self) -> list[str]:
        return list(getattr(self.gateway, "models", []) or [])

    def gateway_health(self) -> dict:
        """Astra AI Gateway status, reported separately from provider health
        (never as a provider). Includes the per-connection health of its
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

    # -- manual health probe ("🔌 AI Providers health" test buttons) ----------
    # A real, tiny request against exactly one provider — `no_fallback=True`
    # plus a pinned `preferred_model` guarantees route_request() never
    # silently substitutes another provider (§11), so "test Groq" actually
    # tests Groq. The result is recorded through the same `_record_route`
    # path a live chat request uses, so it's saved (routing_stats + this
    # provider's rolling health) the instant this one call finishes —
    # independent of whatever other provider tests are running.
    _TEST_MESSAGES = [{"role": "user", "content": "ping"}]

    def test_provider_model(self, name: str, model_id: str,
                            key_id: str | None = None) -> dict:
        """Probe exactly ONE (provider, model) pair and return its result the
        instant this single call finishes — saved via `_record_route` /
        `_mark_down` immediately, independent of any other model's test.
        This is what lets the UI show each model's result as soon as it
        arrives instead of waiting on the whole provider's model list.

        With ``key_id`` the call is forced through that one API key (one
        attempt, no rotation), and the outcome is saved against
        (provider, key, model) — see `_record_key_model`. Without it the
        pool picks a key as usual and the result is still saved against
        whichever key it used.

        When this (provider, key, model) resolves to the same canonical
        upstream identity as a Gateway-side manual test
        (astra.ai.gateway.AstraAIGateway.test_connection_model) running
        concurrently or recently, the real upstream call is shared through
        `self.shared_health` instead of firing a second identical request
        — see astra/ai/shared_health.py. Only this manual-test path is
        affected; live routing is untouched."""
        adapter = next((p for p in self.providers
                        if getattr(p, "name", "?") == name), None)
        pool = getattr(adapter, "pool", None)
        if key_id and pool is not None and hasattr(pool, "pinned"):
            if key_id not in {k["key_id"] for k in pool.keys()}:
                return {"model": model_id, "ok": False, "latency_ms": 0,
                        "error": f"unknown key {key_id!r}", "key_id": key_id,
                        "key": key_id}

        identity, id_cred = (resolve_identity(pool, name, model_id, key_id)
                             if self.shared_health is not None else (None, None))

        def _do_route(pinned_key_id):
            req = RoutingRequest(task_type="health_check",
                                 messages=self._TEST_MESSAGES,
                                 preferred_provider=name, preferred_model=model_id,
                                 no_fallback=True, max_tokens=8)
            if pinned_key_id and pool is not None and hasattr(pool, "pinned"):
                with pool.pinned(pinned_key_id):
                    return self.route_request(req)
            return self.route_request(req)

        if identity is not None:
            holder: dict = {}

            def _probe_fn():
                # Pin to the exact credential the shared identity was
                # resolved against — whether or not the caller passed an
                # explicit key_id — so the identity and the actual upstream
                # call (if this caller owns the probe) always agree on
                # which key is being tested.
                rr = _do_route(id_cred.key_id if id_cred else key_id)
                holder["rr"] = rr
                return {"ok": bool(rr.ok),
                       "error": "" if rr.ok else (rr.error or "test failed"),
                       "latency_ms": round(rr.latency_ms, 1)}

            result, reused = self.shared_health.run(identity, _probe_fn)
            # The shared identity already resolved the exact credential. Use it
            # for display so concurrent key selection cannot mislabel the test.
            cred = id_cred or (pool.last_key() if pool is not None and hasattr(pool, "last_key") else None)
            if reused:
                # No real upstream call was made by this caller — the
                # Gateway-side probe owns it. Local Provider health state
                # (key_health / model routing) must still reflect the
                # shared result.
                self._record_key_model(adapter, model_id, result["ok"],
                                       result["latency_ms"], result["error"],
                                       req=RoutingRequest(task_type="health_check"))
                return {"model": model_id, "ok": result["ok"],
                        "latency_ms": round(result["latency_ms"], 1),
                        "error": result["error"],
                        "key_id": cred.key_id if cred else (key_id or ""),
                        "key": cred.label if cred else (key_id or ""),
                        "key_label": cred.label if cred else (key_id or ""),
                        "reused": True}
            rr = holder["rr"]
            return {
                "model": rr.model or model_id,
                "ok": bool(rr.ok),
                "latency_ms": round(rr.latency_ms, 1),
                "error": "" if rr.ok else (rr.error or "test failed"),
                "key_id": cred.key_id if cred else (key_id or ""),
                "key": cred.label if cred else "",
            }

        # Identity couldn't be resolved (no pool / no usable credential) —
        # unchanged direct-probe behavior.
        rr = _do_route(key_id)
        cred = pool.last_key() if pool is not None and hasattr(pool, "last_key") else None
        return {
            "model": rr.model or model_id,
            "ok": bool(rr.ok),
            "latency_ms": round(rr.latency_ms, 1),
            "error": "" if rr.ok else (rr.error or "test failed"),
            "key_id": cred.key_id if cred else (key_id or ""),
            "key": cred.label if cred else "",
        }

    # -- per-(provider, key, model) health ------------------------------------
    @staticmethod
    def _pinned(adapter) -> bool:
        pool = getattr(adapter, "pool", None)
        return bool(pool is not None and getattr(pool, "pinned_key", lambda: None)())

    def _load_key_model(self) -> None:
        try:
            for r in self.store.fetch("SELECT * FROM key_model_health"):
                self._key_model[(r["provider"], r["key_id"], r["model"])] = {
                    "ok": bool(r["ok"]), "latency_ms": r["latency_ms"] or 0,
                    "error": r["error"] or "", "source": r["source"] or "live",
                    "tested_at": r["tested_at"] or "", "ts": r["ts"] or 0.0,
                    "key_label": r["key_label"] or ""}
        except Exception:
            pass

    def _record_key_model(self, adapter, model_id: str, ok: bool,
                          latency_ms, error: str, req=None) -> None:
        """Save which key served (or failed) this model. Called for every
        provider attempt — live traffic and manual tests alike. Nothing is
        saved when no key was actually picked (e.g. "no healthy credential")."""
        pool = getattr(adapter, "pool", None)
        cred = pool.last_key() if pool is not None and hasattr(pool, "last_key") else None
        if cred is None:
            return
        name = getattr(adapter, "name", "")
        now = time.time()
        source = "test" if (req is not None and req.task_type == "health_check") else "live"
        row = {"ok": bool(ok), "latency_ms": int(latency_ms or 0),
               "error": "" if ok else str(error or "")[:300], "source": source,
               "tested_at": _now(), "ts": now, "key_label": cred.label}
        with self._lock:
            self._key_model[(name, cred.key_id, model_id)] = row
            if self.store:
                try:
                    self.store.exec(
                        "INSERT INTO key_model_health (provider, key_id, model, "
                        "key_label, ok, latency_ms, error, source, tested_at, ts) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(provider, key_id, model) DO UPDATE SET "
                        "key_label=excluded.key_label, ok=excluded.ok, "
                        "latency_ms=excluded.latency_ms, error=excluded.error, "
                        "source=excluded.source, tested_at=excluded.tested_at, "
                        "ts=excluded.ts",
                        (name, cred.key_id, model_id, cred.label, int(ok),
                         row["latency_ms"], row["error"], source, row["tested_at"], now))
                except Exception:
                    pass

    def _key_model_state(self, provider: str, key_id: str, model_id: str):
        """True = recently OK, False = recently failed, None = unknown/stale."""
        row = self._key_model.get((provider, key_id, model_id))
        if not row or time.time() - row["ts"] > KEY_MODEL_TTL_S:
            return None
        return row["ok"]

    def _model_status(self, adapter, model_id: str) -> str:
        """'ok' if any key recently worked for this model, 'failed' if EVERY
        key recently failed for it, else 'unknown'."""
        pool = getattr(adapter, "pool", None)
        if pool is None or not hasattr(pool, "keys"):
            return "unknown"
        name = getattr(adapter, "name", "")
        states = [self._key_model_state(name, k["key_id"], model_id) for k in pool.keys()]
        if not states:
            return "unknown"
        if any(st is True for st in states):
            return "ok"
        if all(st is False for st in states):
            return "failed"
        return "unknown"

    def key_health(self, provider: str) -> dict:
        """Saved per-key results for one provider:
        {model: {key_id: {ok, latency_ms, error, source, tested_at, key_label}}}."""
        out: dict = {}
        for (p, key_id, model), row in list(self._key_model.items()):
            if p != provider:
                continue
            out.setdefault(model, {})[key_id] = {
                k: row[k] for k in ("ok", "latency_ms", "error", "source",
                                    "tested_at", "key_label")}
        return out

    def test_provider(self, name: str) -> dict:
        """Probe EVERY model this provider exposes, one at a time — not just
        the first. Each model's result is recorded (via `_record_route` /
        `_mark_down`) the instant that model's own probe returns, so a
        slow/dead model never blocks the rest of the provider's models from
        being tested and saved. Kept for the "test-all" bulk endpoint; the
        per-model UI test now calls `test_provider_model` directly, one
        model at a time, so each result can reach the browser as soon as
        it's ready instead of waiting for this whole list."""
        provider = next((p for p in self.providers
                         if getattr(p, "name", "?") == name), None)
        if provider is None:
            return {"provider": name, "ok": False, "models": [],
                    "error": "unknown provider"}
        models = list(getattr(provider, "models", []) or [])
        if not models:
            return {"provider": name, "ok": False, "models": [],
                    "error": "no model configured"}
        results = [self.test_provider_model(name, model_id) for model_id in models]
        return {"provider": name, "ok": any(r["ok"] for r in results),
                "models": results, "error": ""}

    def test_all_providers(self) -> list:
        """Probe every provider's every model, one at a time. Each result is
        saved (via `test_provider` -> `_record_route`) the moment its own
        probe completes — no need to wait for every provider to finish, and
        one provider erroring never stops the rest from being tested."""
        return [self.test_provider(getattr(p, "name", "?"))
                for p in self.providers]

    @staticmethod
    def _fit_messages(messages, model, max_tokens=None):
        """Provider-aware context fitting against the target model's real
        context window (keep everything when it fits; drop oldest middle
        turns only when it genuinely does not). See astra.ai.context_budget."""
        from astra.ai.context_budget import default_reserve_tokens, fit_messages
        from astra.ai.token_limits import resolve_output_tokens
        ctx = int(getattr(model, "context_window", 0) or 0)
        out = resolve_output_tokens(max_tokens,
                                    provider=getattr(model, "provider", ""),
                                    model_meta=model)
        reserve = out if out else default_reserve_tokens(ctx)
        return fit_messages(messages, context_window=ctx,
                            reserve_tokens=reserve)

    def _attempt(self, adapter, model: Model, req: RoutingRequest) -> RoutingResult | None:
        name = getattr(adapter, "name", "")
        op = new_op_id()
        self._emit("ai.started", provider=name, model=model.model_id,
                   op=op, trace=req.trace)
        t0 = ms_now()
        last_error = ""
        # per-credential + per-model retries: a failed key rolls to the next
        # key on the same model, then same provider's next model, then provider.
        retries = self.max_retries
        for attempt in range(1, retries + 2):
            if attempt > 1:
                self._emit("credential.rotation", provider=name, model=model.model_id,
                           attempt=attempt, op=op, trace=req.trace)
                self._emit("router.retry", provider=name, model=model.model_id,
                           attempt=attempt, op=op, trace=req.trace)
            try:
                messages = self._fit_messages(req.messages, model,
                                              req.max_tokens)
                # Multimodal dispatch: use a specialized adapter method for
                # the non-chat TTS output modality before falling through to
                # the normal chat path. Image generation is deliberately NOT
                # dispatched here: it is owned exclusively by the Gateway's
                # ImageRouter (see route_request's fail-closed guard).
                out_mods = req.required_output_modalities or []
                if "audio" in out_mods and hasattr(adapter, "text_to_speech"):
                    prompt = self._extract_prompt(req.messages)
                    text = adapter.text_to_speech(prompt, model=model.model_id)
                elif req.required_tools or req.structured_output or req.task_contract is not None:
                    # §JSON mode: ask the provider API to enforce JSON, not
                    # just the prompt text. No invented token floor: an
                    # explicit budget is honoured, otherwise the field is
                    # omitted (or derived from the model for providers whose
                    # API requires it) so a reasoning model gets its real
                    # maximum instead of a small Astra cap.
                    text = adapter.chat(
                        messages, model=model.model_id,
                        max_tokens=resolve_output_tokens(
                            req.max_tokens, provider=name, model_meta=model),
                        response_format="json_object")
                else:
                    text = adapter.chat(
                        messages, model=model.model_id,
                        max_tokens=resolve_output_tokens(
                            req.max_tokens, provider=name, model_meta=model))
                ms = duration_ms(t0)
                cost = self._estimate_cost_adapter(adapter, text)
                test_cred = (adapter.pool.last_key()
                              if getattr(adapter, "pool", None) is not None
                              and hasattr(adapter.pool, "last_key") else None)
                key_label = test_cred.label if test_cred else ""
                self._latency[name].append(ms)
                self._calls[name] += 1
                self._cost_est[name] = self._cost_est.get(name, 0.0) + cost
                # Manual health probes must not mutate aggregate provider
                # availability. Test-all intentionally probes many models/keys
                # concurrently; one model's auth/rate-limit result must not
                # make the whole provider "down" or generate misleading
                # recovered/down flapping in the Activity Log.
                if req.task_type != "health_check" and name in self._down:
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
                           latency_ms=ms, op=op, trace=req.trace, terminal=True,
                           key_id=test_cred.key_id if test_cred else "",
                           key_label=key_label)
                self._record_key_model(adapter, model.model_id, True, ms, "", req)
                return rr
            except (ProviderError, TimeoutError) as e:
                last_error = e.message or getattr(e, "category", type(e).__name__)
                self._errors[name] = self._errors.get(name, 0) + 1
                self._record_key_model(adapter, model.model_id, False,
                                       duration_ms(t0), last_error, req)
                # per-key test: one attempt, on that key only -> terminal.
                retry = (not self._pinned(adapter) and attempt <= retries
                         and getattr(e, "retryable", True))
                test_cred = (adapter.pool.last_key()
                              if getattr(adapter, "pool", None) is not None
                              and hasattr(adapter.pool, "last_key") else None)
                key_label = test_cred.label if test_cred else ""
                self._emit("ai.failed", provider=name, model=model.model_id,
                           error=last_error, attempt=attempt, op=op,
                           trace=req.trace, terminal=not retry, retrying=retry,
                           key_id=test_cred.key_id if test_cred else "",
                           key_label=key_label)
                if self._pinned(adapter):
                    break
                # A NON-retryable error (e.g. "no healthy credential
                # configured": no key to try, and cooldowns outlast our 1-2s
                # backoff) or the last allowed attempt ends this candidate
                # now — falling through would re-call it for nothing and let
                # the router move on to the next provider immediately.
                if not retry:
                    break
                # the adapter's credential pool has already cooled the bad key;
                # a fresh key on the same model may succeed, so keep retrying up
                # to max_retries, respecting backoff only for transient errors.
                time.sleep(min(self.backoff_s * attempt, 8))
            except Exception as e:           # never let a provider kill routing
                last_error = f"{type(e).__name__}: {e}"
                self._errors[name] = self._errors.get(name, 0) + 1
                retry = (not self._pinned(adapter) and attempt <= retries)
                test_cred = (adapter.pool.last_key()
                              if getattr(adapter, "pool", None) is not None
                              and hasattr(adapter.pool, "last_key") else None)
                key_label = test_cred.label if test_cred else ""
                self._emit("ai.failed", provider=name, model=model.model_id,
                           error=last_error, attempt=attempt, op=op,
                           trace=req.trace, terminal=not retry, retrying=retry,
                           key_id=test_cred.key_id if test_cred else "",
                           key_label=key_label)
                if self._pinned(adapter):
                    break
                if retry:
                    time.sleep(min(self.backoff_s * attempt, 8))
        # A manual health probe records its per-key/model result but must
        # not change aggregate provider availability. The probe is only a
        # diagnostic observation; normal routing failures are still allowed
        # to mark the provider down and trigger recovery/cooldown behavior.
        if req.task_type != "health_check":
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

    def available_targets(self) -> list[dict]:
        """Sanitized (provider, model) targets that are usable right now.

        Plain data only — no adapters, credentials or pools — so callers
        (e.g. the chat pipeline's Gateway "assign the work" step) can show
        the Gateway what it may choose from without touching provider
        internals. Same eligibility as routing itself (`_candidates`):
        provider has healthy credentials and the model is not disabled.

        Each entry also carries `health` — 'ok' | 'failed' | 'unknown' from
        `_model_status()`, the SAME per-(provider, key, model) data the
        "🔌 AI Providers health" page's test buttons and live traffic both
        write into (`key_model_health` / `_record_key_model`), and the
        SAME signal `route_request()`'s own ranking already demotes
        known-failed models with. Surfacing it here means the Gateway's
        "assign" pick (Call #1) is no longer blind to real-time health —
        it sees exactly what routing will actually honor, instead of only
        capability/quality/context. Entries are sorted health-first
        ('ok' > 'unknown' > 'failed') so a truncated prompt (see
        `MAX_TARGETS_IN_PROMPT`) never drops a healthy model in favor of a
        known-bad one.
        """
        out = []
        try:
            candidates = self._candidates(RoutingRequest())
        except Exception:
            return out
        for adapter, model in candidates:
            out.append({
                "provider": getattr(adapter, "name", ""),
                "model": model.model_id,
                "capabilities": list(getattr(model, "capabilities", []) or []),
                "quality": getattr(model, "quality_class", ""),
                "context_window": int(getattr(model, "context_window", 0) or 0),
                "health": self._model_status(adapter, model.model_id),
            })
        _health_rank = {"ok": 0, "unknown": 1, "failed": 2}
        out.sort(key=lambda t: _health_rank.get(t["health"], 1))
        return out

    # -- shared agent tool loop (Provider as the driving brain) --------------
    def run_tool_loop(self, task, *, registry, system_prompt="", history=None,
                      context_blocks=None, terminal=None, runtime=None,
                      execution_history=None,
                      session_id=None, scope=None, task_type: str = "coding",
                      vision: bool = False, provider: str | None = None,
                      model: str | None = None, no_fallback: bool = False,
                      max_steps: int = 8, max_tokens: int | None = None,
                      trace: str = ""):
        """Drive the shared `AgentToolLoop` with the existing Provider
        system (`route_request`). Identical loop, identical `ToolRegistry`,
        therefore the identical shared Terminal as the Gateway path.

        Returns an `astra.ai.agent_tool_loop.ToolLoopResult`."""
        from astra.ai.agent_tool_loop import AgentToolLoop, ProviderToolCaller
        loop = AgentToolLoop(registry, terminal=terminal, runtime=runtime,
                             events=self.events,
                             max_steps=max_steps,
                             execution_history=execution_history)
        caller = ProviderToolCaller(self, task_type=task_type, vision=vision,
                                    provider=provider, model=model,
                                    no_fallback=no_fallback, trace=trace)
        return loop.run(task, caller, system_prompt=system_prompt,
                        history=history, context_blocks=context_blocks,
                        session_id=session_id, scope=scope,
                        max_tokens=max_tokens, trace=trace)

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
                             messages=messages, max_tokens=max_tokens)
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
        # Snapshot under the lock so a concurrent add()/reset_health() (a
        # "Test all" burst fires plenty of these in parallel threads) can
        # never be caught mid-mutation. `.get(name, ...)` on every
        # bookkeeping dict below is defense-in-depth on top of that: a
        # provider that's in self.providers but not yet in these dicts
        # must never raise KeyError and 500 this whole endpoint.
        with self._lock:
            providers_snapshot = list(self.providers)
        for p in providers_snapshot:
            name = getattr(p, "name", "?")
            info = self._provider_info(p)
            models = list(getattr(p, "models", []) or [])
            lat = self._latency.get(name, [])
            out[name] = {
                "healthy": info["state"] == "healthy",
                "state": info["state"],
                "models": models,
                "base_url": getattr(p, "base_url", "") or "",
                "latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else None,
                "calls": self._calls.get(name, 0),
                "errors": self._errors.get(name, 0),
                "cost_usd": round(self._cost_est.get(name, 0.0), 6),
                "credentials": self._credential_count(p),
                "keys": (p.pool.keys() if getattr(p, "pool", None) is not None
                         and hasattr(p.pool, "keys") else []),
                "key_results": self.key_health(name),
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
        """Clear ALL of one provider's accumulated health bookkeeping —
        down-state, call count, error count, latency samples — without
        touching its pool/credentials. Called right before a fresh manual
        test run (single-provider or "test all") so that test's numbers
        start from zero instead of piling onto whatever was saved from
        every earlier test."""
        with self._lock:
            self._down.discard(name)
            self._calls[name] = 0
            for by_name in (self._errors, self._latency):
                bucket = by_name.get(name)
                if isinstance(bucket, list):
                    bucket.clear()
                elif isinstance(bucket, int):
                    by_name[name] = 0
        if self.shared_health is not None:
            self.shared_health.invalidate(canonical_provider(name))

    def reset_all_health(self) -> None:
        """Reset every provider plus the shared manual-health cache.

        This starts a genuinely fresh Test All run. The cache is cleared once
        here; the individual provider workers do NOT clear it again, so a
        simultaneous Gateway probe can still reuse the Provider probe.
        """
        for p in self.providers:
            name = getattr(p, "name", "?")
            with self._lock:
                self._down.discard(name)
                self._calls[name] = 0
                for by_name in (self._errors, self._latency):
                    bucket = by_name.get(name)
                    if isinstance(bucket, list):
                        bucket.clear()
                    elif isinstance(bucket, int):
                        by_name[name] = 0
        if self.shared_health is not None:
            self.shared_health.invalidate()

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
