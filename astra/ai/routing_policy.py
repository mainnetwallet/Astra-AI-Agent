"""Deterministic route-scoring policy for the AgentRouter.

Candidate (provider, model) pairs are scored on capability fit, historical
success, context fit, tool/vision/JSON support and quality — penalised by
latency, cost, errors and rate-limit status. The user preference (fastest /
best_quality / lowest_cost / balanced / provider / model) reshapes the weights.

Nothing here calls a provider. It is pure decision logic, unit-testable.
"""
from __future__ import annotations

from astra.ai.models import Model

PREFERENCES = ("fastest", "best_quality", "lowest_cost", "balanced",
               "specific_provider", "specific_model")

CLASS_WEIGHT = {"cheap": 1, "mid": 3, "premium": 9}
SPEED_WEIGHT = {"fast": 2, "mid": 1, "slow": 0}
QUALITY_WEIGHT = {"high": 2, "mid": 1, "fast": 1}

# Coarse expected-latency estimate (ms) used only when a candidate has no
# historical avg_latency_ms yet, so max_latency_ms still has *something* to
# compare against for a brand-new candidate.
SPEED_ESTIMATE_MS = {"fast": 400, "mid": 1500, "slow": 4000}

# Same per-token baseline AgentRouter._estimate_cost()/_estimate_cost_adapter()
# use for post-hoc cost accounting ("cheap" class, ~4 chars/token). No exact
# pricing feed exists at this layer, so max_cost_usd is checked against this
# conservative estimate scaled by the model's cost_class multiplier, per the
# spec's "use the existing cost class/multiplier conservatively" guidance.
BASE_COST_PER_TOKEN_USD = 0.25e-6

# Finite penalty applied when a candidate violates a soft caller-supplied
# budget (max_latency_ms / max_cost_usd). Large enough to always rank below
# any budget-compliant eligible candidate, but — unlike the -1.0e6 used for
# meets_hard_requirements() — not an absolute exclusion, so a violating
# candidate remains selectable as a last resort when nothing else is
# eligible (see spec: "should not be selected when an eligible alternative
# exists", not "must never be selected").
BUDGET_VIOLATION_PENALTY = 1000.0

# Bonus applied when a candidate matches the caller's explicit
# preferred_provider / preferred_model. A *preference*, not a hard filter:
# meets_hard_requirements() never checks these, so an unavailable specific
# provider/model simply loses this bonus rather than breaking routing.
PREFERENCE_MATCH_BONUS = 5.0


def estimated_cost_usd(model: Model, request) -> float:
    """Conservative per-request cost estimate for `model` under `request`.

    Used to give max_cost_usd real influence over scoring even though no
    per-token pricing table exists at this layer — see BASE_COST_PER_TOKEN_USD.
    """
    tokens = max(1, int(getattr(request, "max_tokens", 0) or 500))
    return tokens * BASE_COST_PER_TOKEN_USD * model.cost_multiplier


def preference_weights(preference: str) -> dict:
    """Weights that turn a user preference into concrete scoring terms."""
    pref = (preference or "balanced").lower()
    if pref == "fastest":
        return {"latency": 4, "cost": 1, "quality": 1, "capability": 3}
    if pref == "best_quality":
        return {"latency": 1, "cost": 1, "quality": 5, "capability": 3}
    if pref == "lowest_cost":
        return {"latency": 1, "cost": 5, "quality": 1, "capability": 3}
    # balanced, specific_provider, specific_model use the neutral default
    return {"latency": 2, "cost": 2, "quality": 2, "capability": 3}


def _task_class(task_type: str) -> str:
    t = (task_type or "simple_chat").lower()
    if t in ("vision",) or "vision" in t:
        return "vision"
    if t in ("research", "web3", "browser", "tool_selection"):
        return "quality"
    if t in ("coding", "reasoning", "planning"):
        return "quality"
    if t in ("summarization", "translation", "simple_chat"):
        return "fast"
    return "balanced"


def meets_hard_requirements(model: Model, request) -> bool:
    """Binary capability/context gate shared by scoring and any code path
    (like the Astra AI Gateway fallback) that needs to filter
    candidates without going through the full scorer. A model that fails
    this is never selectable for the request, regardless of score."""
    need = set(request.required_capabilities)
    caps = set(model.capabilities)
    if need and not need.issubset(caps):
        return False
    for flag, cap in ((request.vision, "vision"),
                      (request.structured_output, "json"),
                      (bool(request.required_tools), "tools"),
                      (request.streaming, "stream")):
        if flag and cap not in caps:
            return False
    ctx = int(request.context_tokens or 0)
    if ctx > model.context_window:
        return False
    return True


class RoutingDecisionPolicy:
    """Scores candidate routes for one RoutingRequest."""

    def __init__(self, stats=None, preference: str = "balanced"):
        self.stats = stats or {}          # routing_stats: provider/model -> rows
        self.preference = preference

    # -- scoring --------------------------------------------------------------
    def score(self, model: Model, request, provider_info: dict | None = None,
              stat: dict | None = None) -> float:
        if not meets_hard_requirements(model, request):
            return -1.0e6
        w = preference_weights(self.preference)
        score = 0.0
        provider_info = provider_info or {}

        # 1. capability match (soft: proportional bonus on top of the hard
        # gate above, using either the explicit requirement or the task_type
        # string as a loose proxy for capability names — see router.py's
        # TASK_HARD_CAPABILITIES for where that proxy is promoted to a hard
        # requirement for unambiguous, binary capabilities)
        need = set(request.required_capabilities) or set(request.task_type.split(","))
        if request.task_type == "vision":
            need.add("vision")
        caps = set(model.capabilities)
        if need:
            have = len(need & caps)
            score += w["capability"] * (have / max(1, len(need)))

        # 2. context fit
        ctx = int(request.context_tokens or 0)
        score += 0.5 * min(1.0, (model.context_window - ctx) /
                           max(1, model.context_window))

        # 3. quality vs task class
        task_class = _task_class(request.task_type)
        quality = model.quality_class
        if task_class == "quality":
            score += w["quality"] * QUALITY_WEIGHT.get(quality, 1)
        elif task_class == "fast":
            score += w["quality"] * (2 if quality in ("fast", "mid") else 0)
        else:
            score += w["quality"] * QUALITY_WEIGHT.get(quality, 1)

        # 4. provider health / credential availability
        state = provider_info.get("state", "")
        if state == "healthy":
            score += 2
        elif state == "not_configured":
            score -= 4
        elif state == "unhealthy":
            score -= 10

        # 5. historical success (deterministic stats)
        if stat:
            calls = int(stat.get("calls") or 0)
            success = float(stat.get("success_rate") or 1.0 if calls else 1.0)
            if calls:
                score += w["capability"] * success * 1.5
            score -= w["latency"] * (
                (float(stat.get("avg_latency_ms") or 0.0) / 4000.0))

        # 6. latency / cost budgets — max_latency_ms and max_cost_usd must
        # actually shape the ranking, not just be recorded on the request.
        if request.max_latency_ms:
            if stat and stat.get("avg_latency_ms"):
                est_latency_ms = float(stat["avg_latency_ms"])
            else:
                est_latency_ms = SPEED_ESTIMATE_MS.get(
                    getattr(model, "speed_class", "mid"), 1500)
            if est_latency_ms > request.max_latency_ms:
                # Exceeds the caller's latency budget: strongly deprioritized
                # (see BUDGET_VIOLATION_PENALTY) rather than hard-excluded,
                # so it's still usable as a last resort if it's the only
                # eligible candidate.
                score -= BUDGET_VIOLATION_PENALTY
            else:
                score += w["latency"] * (1.0 - est_latency_ms / request.max_latency_ms)
        if request.max_cost_usd is not None:
            est_cost = estimated_cost_usd(model, request)
            if est_cost > request.max_cost_usd:
                score -= BUDGET_VIOLATION_PENALTY
            else:
                score += w["cost"] * (1.0 - est_cost / request.max_cost_usd)
        score -= w["cost"] * model.cost_multiplier * 0.25
        score += (0.1 if getattr(model, "preferred", False) else 0)
        score -= (10 if getattr(model, "disabled", False) else 0)

        # 7. explicit specific_provider / specific_model preference. This is
        # a caller-requested ranking preference, not a hard requirement —
        # meets_hard_requirements() never checks it, so an unavailable
        # specific provider/model just loses the bonus instead of breaking
        # routing. Kept independent of `self.preference`/`user_preference`
        # so it applies whenever the caller names a specific provider/model,
        # regardless of which weighting profile ("balanced",
        # "specific_provider", ...) is otherwise in effect.
        if getattr(request, "preferred_provider", None) and \
                model.provider == request.preferred_provider:
            score += PREFERENCE_MATCH_BONUS
        if getattr(request, "preferred_model", None) and \
                model.model_id == request.preferred_model:
            score += PREFERENCE_MATCH_BONUS

        # 8. reasoning level fit
        rl = getattr(model, "reasoning_level", "auto")
        if request.reasoning_level == "high" and rl not in ("high", "auto"):
            score -= 2
        return round(score, 3)

    def rank(self, candidates: list, request) -> list:
        """Sort candidates (provider_adapter, model) by score, descending."""
        scored = []
        for adapter, model in candidates:
            info = getattr(adapter, "health_info", None) or {}
            stat = self.stats.get(f"{model.provider}:{model.model_id}") or \
                self.stats.get(model.provider) or {}
            s = self.score(model, request, provider_info=info, stat=stat)
            if s == -1.0e6:
                continue
            scored.append((s, adapter, model))
        scored.sort(key=lambda t: -t[0])
        return scored