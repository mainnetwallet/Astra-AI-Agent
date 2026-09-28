"""Astra AI Gateway — task-level execution recovery for the EXISTING
Provider system's own provider/model catalog (§2-§5, §8-§12, §18).

This is the piece that makes the Gateway "the central AI routing/control-
plane for provider/model failover" (PRIMARY NEW REQUIREMENT) *without*
merging it into ProviderRegistry (§1) and without Gateway ever touching a
Provider adapter, credential or client (§3): everything here only ever
sees `astra.ai.gateway_contract.ProviderExecutionTarget` — plain
provider_id/model_id/capabilities metadata — never a real adapter object.

Deliberately separate from `astra/ai/gateway_routing.py`, which is the
Gateway's OWN connection catalog/health/ranking brain. The two never
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

    def __init__(self, store=None, events=None, primary_provider: str = "gemini"):
        # A dedicated GatewayRoutingState instance, backed by the same
        # Store (no new database — §18) but writing exclusively under
        # `EXISTING_PROVIDER_NS`-prefixed provider ids.
        self.routing_state = GatewayRoutingState(store)
        self.events = events
        self.primary_provider = str(primary_provider or "").strip().lower()

    def _is_primary(self, target: ProviderExecutionTarget) -> bool:
        return bool(self.primary_provider) and \
            str(target.provider_id).lower() == self.primary_provider

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

    def _health_sort_key(self, target: ProviderExecutionTarget):
        """Lower is better. Ranks already-eligible (healthy) targets by their
        OWN measured Gateway health — never by provider identity/serial
        position — so the best-tested target is tried first and the next-best
        second, serially, exactly mirroring how `gateway_routing.score_target`
        ranks the Gateway's own GW_* connections.

        - success_rate (higher is better) dominates
        - consecutive_failures (fewer is better) breaks a success-rate tie
        - measured average latency (lower is better) breaks what's left; a
          target with no successful call yet (average_latency_ms == 0) gets a
          neutral placeholder instead of an unearned "fastest" ranking
        Python's sort is stable, so a true tie preserves the caller's own
        `candidates` order (its RoutingDecisionPolicy ranking) — unchanged
        from before for targets whose health is genuinely indistinguishable.
        """
        h = self.target_health(target)
        latency = h.average_latency_ms if h.success_count else 500.0
        return (-h.success_rate(), h.consecutive_failures, latency)

    # -- selection (§2, §4, §10) ----------------------------------------------
    def select_execution_target(
            self, candidates: list[ProviderExecutionTarget], *,
            required_capabilities: tuple[str, ...] | list[str] = (),
            exclude: set[tuple[str, str]] | None = None,
    ) -> ProviderExecutionTarget | None:
        """Pick the best eligible candidate, or None if every one of them is
        excluded, missing a required capability, or currently in cooldown.

        Ordering: only candidates that PASSED the health/cooldown check
        (`is_eligible`) are ever considered — an unhealthy target never
        reaches selection. Among those eligible candidates, the one with the
        best measured Gateway health goes first, the next-best second, and so
        on (`_health_sort_key`) — serial, by health, never by provider name
        or the caller's original list position. The primary provider
        (`primary_provider`, default Gemini) is tried first; everything else
        follows purely by health. The last successful target, while still
        eligible, is sticky and wins outright; a failure clears it.
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
        # Sticky last_success: if the last successful target is still an
        # eligible candidate it wins outright (Gemini is not tried first).
        last = self.routing_state.last_successful()
        if last:
            for t in eligible:
                if (_ns(t.provider_id) == last["provider"]
                        and t.model_id == last["model"]):
                    return t
        # Otherwise (no/failed last_success): primary provider (Gemini)
        # first, best health first; then every other target by health only.
        eligible.sort(key=lambda t: (0 if self._is_primary(t) else 1,
                                     *self._health_sort_key(t)))
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
