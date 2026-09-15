"""Credential pools for Astra AI providers.

Every provider reads a *list* of API keys:
  GEMINI_API_KEYS=<k1>,<k2>,...
Each comma/space-separated entry is a Credential with its own health, cooldown
and statistics. A pool hands out the healthiest not-in-cooldown key first
(round-robin among equally healthy), records usage/failures, and — on an auth
error — marks only *that* credential unhealthy so sibling keys keep working.

Secrets are never logged, never serialised into events/API, and never exposed.
Only non-secret metadata (id, healthy, calls, errors, cooldown_until) leaves.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Credential:
    """One API key with health + usage metadata. The secret stays private."""

    def __init__(self, provider: str, secret: str):
        self.provider = provider
        self._secret = secret
        self.healthy = bool(secret)
        self.calls = 0
        self.errors = 0
        self.previous_success = True
        self.cooldown_until = 0.0          # perf_counter timestamp
        self.cooled_for_rate_limit = False
        self.last_used_at = 0.0
        self.last_error = ""
        self.created_at = _now()

    # -- lifecycle ------------------------------------------------------------
    def mark_success(self) -> None:
        self.calls += 1
        self.previous_success = True
        self.last_used_at = time.perf_counter()
        self.cooldown_until = 0.0

    def mark_failure(self, reason: str = "", *, rate_limited: bool = False,
                     auth_failure: bool = False, block_s: float = 0.0) -> None:
        self.calls += 1
        self.errors += 1
        self.previous_success = False
        self.last_error = reason[:200]
        self.last_used_at = time.perf_counter()
        if block_s > 0:
            self.cooldown_until = time.perf_counter() + block_s
        if rate_limited:
            self.cooled_for_rate_limit = True
        if auth_failure:
            # an invalid/revoked key is useless until re-validated by the user
            self.healthy = False

    @property
    def in_cooldown(self) -> bool:
        return time.perf_counter() < self.cooldown_until

    def to_metadata(self) -> dict:
        """Public, secret-free metadata for dashboards / health / events."""
        return {
            "id": id(self), "provider": self.provider,
            "healthy": bool(self.healthy) and not self.in_cooldown,
            "in_cooldown": self.in_cooldown,
            "calls": self.calls, "errors": self.errors,
            "success_rate": round(self.calls / (self.calls + self.errors), 2)
                            if (self.calls + self.errors) else None,
            "last_error": self.last_error or "",
        }


class CredentialPool:
    """Round-robin / least-recently-used selection across healthy keys."""

    def __init__(self, provider: str, secrets: list[str] | None = None):
        self.provider = provider
        self._lock = threading.Lock()
        self._creds = [Credential(provider, s) for s in (secrets or [])]
        self._cursor = 0

    # -- config ---------------------------------------------------------------
    @classmethod
    def from_env(cls, config, env_name: str, provider: str | None = None) -> "CredentialPool":
        fn = getattr(config, "getlist", None)
        values = fn(env_name) if fn else []
        return cls(provider or env_name.replace("_API_KEYS", "").lower(), values)

    def add(self, secret: str) -> Credential:
        with self._lock:
            c = Credential(self.provider, secret)
            self._creds.append(c)
            return c

    def clear(self) -> None:
        with self._lock:
            self._creds = []

    @property
    def count(self) -> int:
        return len(self._creds)

    @property
    def healthy_count(self) -> int:
        return sum(1 for c in self._creds if c.healthy and not c.in_cooldown)

    def __bool__(self) -> bool:
        return self.healthy_count > 0

    # -- selection ------------------------------------------------------------
    def pick(self) -> Credential | None:
        """Best key: healthy + out-of-cooldown, by (recently used, round robin)."""
        with self._lock:
            cands = [c for c in self._creds if c.healthy and not c.in_cooldown]
            if not cands:
                return None
            cands.sort(key=lambda c: c.last_used_at)   # least-recently-used
            chosen = cands[self._cursor % len(cands)]
            self._cursor += 1
            return chosen

    def get_secret(self) -> str | None:
        c = self.pick()
        return c._secret if c else None

    def get_secret_for(self, cred: Credential) -> str:
        return cred._secret

    def report_success(self, cred: Credential | None) -> None:
        if cred:
            cred.mark_success()

    def report_failure(self, cred: Credential | None, *, reason: str = "",
                       rate_limited: bool = False,
                       auth_failure: bool = False,
                       cooldown_s: float = 30.0) -> None:
        if cred:
            cred.mark_failure(reason, rate_limited=rate_limited,
                              auth_failure=auth_failure, block_s=cooldown_s)

    # -- introspection --------------------------------------------------------
    def metadata(self) -> list[dict]:
        with self._lock:
            return [c.to_metadata() for c in self._creds]

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "credentials": self.healthy_count,
            "total_credentials": self.count,
            "in_cooldown": sum(1 for c in self._creds if c.in_cooldown),
            "calls": sum(c.calls for c in self._creds),
            "errors": sum(c.errors for c in self._creds),
            "healthy": bool(self),
        }