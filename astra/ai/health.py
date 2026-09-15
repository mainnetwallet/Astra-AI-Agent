"""Health for AI providers, credentials and models.

Provider health is tracked separately from credential health, and model health
separately from provider health — a dead model never takes its provider down,
and a dead key never takes its provider down.
"""
from __future__ import annotations

import threading
import time


class HealthStatus:
    """Thread-safe rolling health tracker for one provider/model/endpoint."""

    def __init__(self, window: int = 50):
        self._lock = threading.Lock()
        self._ok = 0
        self._err = 0
        self._window = window
        self._last_ok = 0.0
        self._last_err = 0.0
        self._down_until = 0.0

    def note(self, ok: bool, *, cooldown_s: float = 30.0) -> None:
        with self._lock:
            if ok:
                self._ok += 1
                self._last_ok = time.perf_counter()
            else:
                self._err += 1
                self._last_err = time.perf_counter()
                self._down_until = time.perf_counter() + cooldown_s
            # sliding window: decay counts
            total = self._ok + self._err
            if total > self._window:
                drop = total - self._window
                self._ok = max(0, self._ok - drop * self._ok // total)
                self._err = max(0, self._err - drop * self._err // total)

    @property
    def healthy(self) -> bool:
        now = time.perf_counter()
        if now < self._down_until:
            return False
        total = self._ok + self._err
        if total == 0:
            return True
        return (self._ok / total) >= 0.7

    def success_rate(self) -> float | None:
        total = self._ok + self._err
        return round(self._ok / total, 2) if total else None

    def to_dict(self) -> dict:
        return {"healthy": self.healthy, "success_rate": self.success_rate(),
                "ok": self._ok, "errors": self._err}


def provider_health(provider) -> dict:
    """Public (secret-free) health block for one provider."""
    pool = getattr(provider, "pool", None)
    if pool is not None:
        creds = pool.summary()
        if creds["healthy"]:
            state = "healthy"
        elif creds["total_credentials"] == 0:
            state = "not_configured"
        else:
            state = "unhealthy"
        return {
            "name": getattr(provider, "name", "?"),
            "state": state,
            "credentials": creds["healthy"],
            "total_credentials": creds["total_credentials"],
            "in_cooldown": creds["in_cooldown"],
            "calls": creds["calls"],
            "errors": creds["errors"],
            "models": list(getattr(provider, "models", []))[:60],
            "base_url": getattr(provider, "base_url", ""),
        }
    # legacy providers (Claude/OpenAI-compat) without a pool
    try:
        healthy = bool(provider.health_check())
    except Exception:
        healthy = False
    return {
        "name": getattr(provider, "name", "?"),
        "state": "healthy" if healthy else "not_configured",
        "credentials": 1 if healthy else 0,
        "total_credentials": 1 if healthy else 0,
        "calls": 0, "errors": 0,
        "models": list(getattr(provider, "models", []))[:60],
    }


def model_health(registry, provider: str, model_id: str) -> dict:
    m = registry.get(provider, model_id) if registry else None
    if not m:
        return {"model": model_id, "provider": provider, "status": "unavailable"}
    return {"model": model_id, "provider": provider,
            "status": m.availability, "disabled": m.disabled}