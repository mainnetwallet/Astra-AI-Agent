"""Astra AI Gateway — task-level execution recovery for the EXISTING
Provider system's own provider/model catalog (§2-§5, §8-§12, §18).

This is the piece that makes the Gateway "the central AI routing/control-
plane for provider/model failover" (PRIMARY NEW REQUIREMENT) *without*
merging it into ProviderRegistry (§1) and without Gateway ever touching a
Provider adapter, credential or client (§3): everything here only ever
sees `astra.ai.gateway_contract.ProviderExecutionTarget` — plain
provider_id/model_id/capabilities metadata — never a real adapter object.

Deliberately separate from `astra/ai/gateway_routing.py`, which is the
Gateway's OWN four-connection catalog/health/ranking brain. The two never
share a namespace: `GatewayExecutionRecovery` persists its health rows
under provider ids prefixed with `EXISTING_PROVIDER_NS` so a Provider
literally named "gemini" and the Gateway's own "gemini" connection are
never confused with each other in the shared Store, even though both use
the same underlying `gateway_model_health` / `gateway_routing_state`
tables (§18: reuse the existing Store; no second database).

Public surface (mirrors the spec's suggested API almost verbatim):

    select_execution_target(candidates, ...)   -> ProviderExecutionTarget | None
    report_execution_success(target, latency_ms=0.0)
    report_execution_failure(target, category, cooldown_s=None)
    recover_execution_target(candidates, failed_target, category, ...)

No infinite loops: `select_execution_target` only ever returns a target
from the given `candidates` list, minus `exclude` and minus anything
currently in cooldown — bounded by construction (§10).
"""
from __future__ import annotations

from astra.ai.gateway_contract import (COOLDOWN_CATEGORIES,
                                       ProviderExecutionTarget)
from astra.ai.gateway_routing import (DEFAULT_COOLDOWN_S, GatewayModelHealth,
                                      GatewayRoutingState)

# Namespace prefix so recovery-target health for the *existing* Provider
# system's providers never collides with the Gateway's own GW_* connection
# health rows (which use plain short names like "gemini", "groq", ...) even
# though both are persisted through the same GatewayRoutingState tables.
EXISTING_PROVIDER_NS = "existing"

# §7 category -> cooldown seconds. Auth failures get a longer cooldown
# (§7: "do not endlessly retry ... disable/cooldown bad credential/target")
# than a plain rate limit, which is expected to clear soon.
CATEGORY_COOLDOWN_S = {
    "RATE_LIMIT": 45.0,
    "QUOTA_EXCEEDED": 120.0,
    "MODEL_UNAVAILABLE": 60.0,
    "SERVER_ERROR": 30.0,
    "AUTH_FAILURE": 300.0,
}


def _ns(provider_id: str) -> str:
    return f"{EXISTING_PROVIDER_NS}::{provider_id}"


class GatewayExecutionRecovery:
    """Recovery-target routing for the Existing Provider system.

    Receives only `ProviderExecutionTarget` metadata — never credentials,
    adapters, HTTP clients or ProviderRegistry objects (§3). Health/cooldown
    state is tracked with the same generic per-(provider, model) machinery
    the Gateway's own catalog uses (`GatewayModelHealth`), so a
    model-specific failure only cools that one (provider, model) pair,
    never the whole provider (§6/§8), and a provider whose every model has
    failed simply has none of its targets left eligible — the caller
    naturally falls through to another provider (§9) without this class
    ever needing to know what "provider-level failure" means beyond that.
    """

    def __init__(self, store=None, events=None):
        # A dedicated GatewayRoutingState instance, backed by the same
        # Store (no new database — §18) but writing exclusively under
        # `EXISTING_PROVIDER_NS`-prefixed provider ids.
        self.routing_state = GatewayRoutingState(store)
        self.events = events

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="gateway.execution_recovery", **data)
            except Exception:
                pass

    # -- health / eligibility -------------------------------------------------
    def target_health(self, target: ProviderExecutionTarget) -> GatewayModelHealth:
        return self.routing_state.get_health(_ns(target.provider_id), target.model_id)

    def is_eligible(self, target: ProviderExecutionTarget) -> bool:
        return self.target_health(target).healthy

    # -- selection (§2, §4, §10) ----------------------------------------------
    def select_execution_target(
            self, candidates: list[ProviderExecutionTarget], *,
            required_capabilities: tuple[str, ...] | list[str] = (),
            exclude: set[tuple[str, str]] | None = None,
    ) -> ProviderExecutionTarget | None:
        """Pick the best eligible candidate, or None if every one of them is
        excluded, missing a required capability, or currently in cooldown.

        Ordering: candidates already in `candidates` order (the caller's own
        priority — e.g. its RoutingDecisionPolicy ranking) is preserved
        among equally-healthy targets; the *last successful* target for its
        own (namespaced) provider/model, if present among the candidates,
        is nudged to the front — a soft preference, never a lock (§12) —
        unless a healthier-ranked candidate already leads.
        """
        exclude = exclude or set()
        need = set(required_capabilities)
        eligible = []
        for t in candidates:
            if t.key() in exclude:
                continue
            if need and not need.issubset(set(t.capabilities)):
                continue
            if not self.is_eligible(t):
                continue
            eligible.append(t)
        if not eligible:
            return None
        last = self.routing_state.last_successful()
        if last and last.get("provider", "").startswith(f"{EXISTING_PROVIDER_NS}::"):
            last_provider = last["provider"][len(EXISTING_PROVIDER_NS) + 2:]
            for i, t in enumerate(eligible):
                if t.provider_id == last_provider and t.model_id == last.get("model"):
                    if i:
                        eligible.insert(0, eligible.pop(i))
                    break
        return eligible[0]

    # -- reporting (§4, §8, §12) -----------------------------------------------
    def report_execution_success(self, target: ProviderExecutionTarget,
                                 latency_ms: float = 0.0, *,
                                 op: str = "", trace: str = "") -> None:
        self.routing_state.record_success(_ns(target.provider_id),
                                          target.model_id, latency_ms)
        self._emit("gateway.execution_recovered" if latency_ms else
                   "gateway.execution_completed",
                   provider=target.provider_id, model=target.model_id,
                   op=op, trace=trace)

    def report_execution_failure(self, target: ProviderExecutionTarget,
                                 category: str,
                                 cooldown_s: float | None = None, *,
                                 op: str = "", trace: str = "") -> None:
        cd = (cooldown_s if cooldown_s is not None else
              CATEGORY_COOLDOWN_S.get(category, DEFAULT_COOLDOWN_S))
        self.routing_state.record_failure(_ns(target.provider_id),
                                          target.model_id, cooldown_s=cd)
        self._emit("gateway.target_cooldown" if category in COOLDOWN_CATEGORIES
                   else "gateway.execution_failed",
                   provider=target.provider_id, model=target.model_id,
                   category=category, cooldown_s=cd,
                   op=op, trace=trace)

    # -- recovery (§4, §5) ------------------------------------------------------
    def recover_execution_target(
            self, candidates: list[ProviderExecutionTarget],
            failed_target: ProviderExecutionTarget, category: str, *,
            required_capabilities: tuple[str, ...] | list[str] = (),
            exclude: set[tuple[str, str]] | None = None,
            op: str = "", trace: str = "",
    ) -> ProviderExecutionTarget | None:
        """Report `failed_target`'s failure, then select the next suitable
        target from `candidates` — excluding `failed_target` itself and
        anything already in `exclude` (the caller's own "already tried this
        run" set, keeping recovery bounded — §10)."""
        self.report_execution_failure(failed_target, category, op=op, trace=trace)
        exclude = set(exclude or set())
        exclude.add(failed_target.key())
        target = self.select_execution_target(
            candidates, required_capabilities=required_capabilities,
            exclude=exclude)
        if target is not None:
            self._emit("gateway.recovery_target_selected",
                       fallback_from=f"{failed_target.provider_id}/{failed_target.model_id}",
                       fallback_to=f"{target.provider_id}/{target.model_id}",
                       category=category)
        return target

    # -- introspection ----------------------------------------------------------
    def snapshot(self) -> dict:
        return self.routing_state.snapshot()
