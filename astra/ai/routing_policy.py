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
    (like the AgentRouter.org gateway fallback) that needs to filter
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

        # 6. latency / cost penalties
        score -= w["latency"] * (request.max_latency_ms or 0) / 1000.0 * 0.0
        score -= w["cost"] * model.cost_multiplier * 0.25
        score += (0.1 if getattr(model, "preferred", False) else 0)
        score -= (10 if getattr(model, "disabled", False) else 0)

        # 7. reasoning level fit
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