"""AgentRouter: provider/model selection with fallback, retry + backoff,
health checking, latency and cost tracking.

The orchestrator calls `router.route(...)`; it returns
(provider_name, model, text) or (None, None, None) when no provider can
answer. Retries respect the provider's health — a down provider is skipped,
not retried into the ground. Latency and an estimated cost are tracked for
the dashboard.
"""
from __future__ import annotations

import time

from astra.core.exceptions import ProviderError
from astra.core.timeutil import duration_ms, ms_now


class AgentRouter:
    def __init__(self, providers: list | None = None, config=None,
                 max_retries: int = 2, backoff_s: float = 1.0):
        self.config = config
        self.max_retries = max(0, int((config.get("AI_MAX_RETRIES") if config else None) or max_retries))
        self.backoff_s = float((config.get("AI_BACKOFF") if config else None) or backoff_s)
        self.providers = list(providers or [])
        self._latency: dict[str, list[float]] = {}
        self._errors: dict[str, int] = {}
        self._cost_est: dict[str, float] = {}   # rough token cost estimate usd
        self._down: set[str] = set()
        self._last: dict = {}

    def add(self, provider) -> None:
        self.providers.append(provider)
        self._latency.setdefault(provider.name, [])
        self._errors.setdefault(provider.name, 0)

    def ordered(self, capability: str | None = None) -> list:
        order = self.providers
        # config may reorder: AI_PROVIDER=anthropic,openai,… (only ones we have)
        pref = self.config.getlist("AI_PROVIDER") if self.config else []
        if pref:
            by_name = {p.name: p for p in order}
            order = [by_name[n] for n in pref if n in by_name] + \
                    [p for p in order if p.name not in pref]
        if capability:
            order = [p for p in order if capability in (p.capabilities or ["chat"])]
        return [p for p in order if p.name not in self._down]

    def route(self, messages: list[dict], *, capability: str | None = None,
              max_tokens: int | None = None) -> tuple:
        """Return (provider_name, model, reply) or (None, None, None)."""
        for provider in self.ordered(capability):
            if not self._probe_healthy(provider):
                continue
            for attempt in range(1, self.max_retries + 2):
                t0 = ms_now()
                try:
                    reply = provider.chat(messages, max_tokens=(max_tokens or 500))
                    ms = duration_ms(t0)
                    self._latency[provider.name].append(ms)
                    self._cost_est[provider.name] = self._cost_est.get(
                        provider.name, 0.0) + self._estimate_cost(reply)
                    self._last = {"provider": provider.name,
                                  "model": provider.models[0] if provider.models else "",
                                  "latency_ms": ms}
                    return provider.name, provider.models[0] if provider.models else "", reply
                except ProviderError as e:
                    self._errors[provider.name] += 1
                    if provider.name in (p.name for p in self.providers if not p.health_check()):
                        self._down.add(provider.name)
                    if attempt <= self.max_retries:
                        time.sleep(min(self.backoff_s * attempt, 8))
                    # else fall through to next provider
        return None, None, None

    def _probe_healthy(self, provider) -> bool:
        if not provider.health_check():
            self._down.add(provider.name)
            return False
        self._down.discard(provider.name)
        return True

    @staticmethod
    def _estimate_cost(reply: str) -> float:
        # rough: ~$0.25 per 1M tokens in/out on haiku-class model
        return max(0.0, len(reply) / 4 * 0.25e-6)

    # -- introspection -------------------------------------------------------
    def health(self) -> dict:
        out = {}
        for p in self.providers:
            try:
                healthy = p.health_check()
            except Exception:
                healthy = False
            out[p.name] = {
                "healthy": healthy,
                "models": list(p.models),
                "latency_avg_ms": round(sum(self._latency[p.name]) /
                                        len(self._latency[p.name]), 1)
                                if self._latency.get(p.name) else None,
                "calls": len(self._latency.get(p.name, [])),
                "errors": self._errors.get(p.name, 0),
                "cost_usd": round(self._cost_est.get(p.name, 0.0), 6),
            }
        return out

    def stats(self) -> dict:
        return {"providers": self.health(), "last_route": self._last,
                "down": sorted(self._down)}