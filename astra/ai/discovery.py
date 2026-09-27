"""Model discovery + validation.

Supports: static configured lists (already in the registry), provider
`GET /models` discovery where available, and validation of configured models.
Discovery results are cached in memory, can be refreshed manually, and — for
the registry — merged as extra models. Unavailable models are marked, never
taken as proof the whole provider is down.
"""
from __future__ import annotations

import threading
import time

from astra.ai.models import metadata_for


class ModelDiscovery:
    def __init__(self, registry, adapter_by_name=None, cache_ttl_s: int = 300,
                events=None):
        self.registry = registry
        self.adapter_by_name = adapter_by_name or {}
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, dict] = {}      # provider -> {at, models}
        self._lock = threading.Lock()
        self.events = events

    def attach_events(self, events) -> None:
        self.events = events

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="discovery", **data)
            except Exception:
                pass

    def _adapter_for(self, provider: str):
        a = self.adapter_by_name.get(provider)
        if a is None:
            for adapter in self.adapter_by_name.values():
                if getattr(adapter, "name", "") == provider:
                    return adapter
        return a

    def _list_models(self, provider: str, adapter, op: str) -> list:
        """The single call site for the real upstream GET /models request,
        so every caller (discover(), validate()) gets it logged exactly
        once, the same way an astra_gateway.*/ai.* call is (see
        astra.core.events.EVENT_KINDS)."""
        import time
        self._emit("provider.discovery.request", provider=provider, op=op)
        start = time.monotonic()
        try:
            ids = adapter.list_models()
        except Exception as e:
            self._emit("provider.discovery.error", provider=provider, op=op,
                       terminal=True, reason=f"{type(e).__name__}: {e}",
                       duration_ms=round((time.monotonic() - start) * 1000, 1))
            raise
        self._emit("provider.discovery.success", provider=provider, op=op,
                   terminal=True, model_count=len(ids or []),
                   duration_ms=round((time.monotonic() - start) * 1000, 1))
        return ids

    def discover(self, provider: str, *, force: bool = False) -> dict:
        """Discover models for one provider. Returns {provider, models, from_cache}."""
        with self._lock:
            cached = self._cache.get(provider)
            if not force and cached and (time.monotonic() - cached["at"]) < self.cache_ttl_s:
                return {"provider": provider, "models": cached["models"],
                        "from_cache": True}
        adapter = self._adapter_for(provider)
        if adapter is None or not hasattr(adapter, "list_models"):
            self._cache[provider] = {"at": time.monotonic(), "models": []}
            return {"provider": provider, "models": [], "from_cache": False,
                    "error": "no discovery adapter"}
        from astra.core.events import new_op_id
        try:
            ids = self._list_models(provider, adapter, new_op_id())
        except Exception:
            ids = []
        with self._lock:
            self._cache[provider] = {"at": time.monotonic(), "models": ids}
        return {"provider": provider, "models": ids, "from_cache": False}

    def refresh_all(self, *, force: bool = True) -> dict:
        """Refresh discovery for every registered provider (force available)."""
        return self.refresh(None, force=force)

    def refresh(self, providers: list[str] | None = None, *, force: bool = True) -> dict:
        """Refresh discovery for the given providers (all by default)."""
        targets = providers or self.registry.providers()
        report = {}
        for p in targets:
            report[p] = self.discover(p, force=force)
            for mid in report[p].get("models", []):
                if not self.registry.get(p, mid):
                    self.registry.add(p, mid, availability="discovered")
        return report

    def validate(self, provider: str, model_id: str) -> dict:
        """Cheap validation of one configured model (known id or listable)."""
        known = self.registry.get(provider, model_id) is not None
        if not known:
            adapter = self._adapter_for(provider)
            if adapter is not None and hasattr(adapter, "list_models"):
                from astra.core.events import new_op_id
                try:
                    known = model_id in self._list_models(
                        provider, adapter, new_op_id())
                except Exception:
                    known = False
        if known:
            m = self.registry.get(provider, model_id)
            if m is None:
                meta = metadata_for(model_id, provider)
                meta.pop("provider", None)   # `provider` positional is authoritative
                m = self.registry.add(provider, model_id, **meta)
            m.availability = "available"
        return {"provider": provider, "model": model_id, "valid": known,
                "status": "available" if known else "unavailable"}

    def summary(self) -> dict:
        return {p: {"models": len(v["models"]), "cached": True}
                for p, v in self._cache.items()}
