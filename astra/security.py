"""Security helpers shared across Astra: secret redaction, request ids,
per-IP rate limiting, structured API errors and SSRF-safe URL checks.

Applied globally by the web layer and by subsystems that touch external
inputs. The rule: secrets never reach logs, events, stats, API responses or
prompts; every API request/response carries a request_id; and URL fetches
never hit the loopback / private network unless an operator opts in.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import uuid

# -- secret redaction ---------------------------------------------------------

# key names that *always* hold secrets (matched case-insensitively anywhere
# in the key path)
# Key names that hold secrets. Matched on *segment* boundaries so legitimate
# fields like `authorized_by` (a tx audit id, not a secret) survive, while
# `api_key`, `secret`, `password`, `access_token`, `private_key` etc. match.
_SECRET_KEYS = re.compile(
    r"(?:^|[_\-\s.])(api[_-]?key|apikey|secret|passwd|password|pwd|token|"
    r"authorization|auth[_-]?header|private[_-]?key|seed|seed[_-]?phrase|"
    r"mnemonic|cookie|session[_-]?id|master[_-]?secret|keyfile|credential|"
    r"access[_-]?key|secret[_-]?key)(?=$|[_\-\s.]|\d)", re.I)

# well-known secret *value* patterns, masked wherever they appear, even if the
# surrounding key isn't secret-named (belt + braces)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}|glft-[A-Za-z0-9_\-]{10,}"
    r"|Bearer\s+\S+|x-api-key\s*[:=]\s*\S+|0x[a-fA-F0-9]{60,})")

REDACTED = "***redacted***"


def _mask(match: re.Match) -> str:
    return REDACTED


def redact(value, depth: int = 0):
    """Deep-redact secret-ish keys and secret-shaped values from arbitrary
    JSON-friendly structures. Keeps the key name (useful for debugging) but
    masks the *value* under any secret-named key. Returns a redacted copy;
    never mutates input."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEYS.search(k):
                out[k] = REDACTED
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(_mask, value) if _SECRET_VALUE.search(value) else value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def redact_text(text: str) -> str:
    """Redact secret-shaped strings inside arbitrary text (e.g. log lines)."""
    if not text:
        return text
    return _SECRET_VALUE.sub(_mask, str(text))


SECRET_LINE_PATTERN = _SECRET_VALUE


# -- request ids --------------------------------------------------------------

def make_request_id() -> str:
    """Short, correlation-friendly request identifier."""
    return uuid.uuid4().hex


# -- rate limiting ------------------------------------------------------------

class RateLimiter:
    """Fixed-window per-key limiter, thread-safe. 429s, never busy-loops."""

    def __init__(self, limit: int = 300, window_s: float = 60.0):
        self.limit = max(1, int(limit))
        self.window = float(window_s)
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def count(self, key: str) -> int:
        with self._lock:
            now = time.monotonic()
            return len([t for t in self._hits.get(key, []) if now - t < self.window])


# -- structured API error -----------------------------------------------------

class ApiError(Exception):
    """An error with a canonical machine code + HTTP status + safe message."""

    def __init__(self, code: str, message: str, status: int = 400,
                 request_id: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message,
                "error_code": self.code, "request_id": self.request_id}


# -- SSRF guard ---------------------------------------------------------------

_LOOPBACK_NETS = ("127.0.0.0/8", "::1/128", "0.0.0.0/8")
_PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10")
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def risky_url(url: str, resolve_dns: bool = True, allow_loopback: bool = False) -> bool:
    """True if `url` targets a risky address (non-http scheme, loopback,
    link-local, private network or internal metadata host) — callers must
    refuse such URLs unless the operator explicitly allows them."""
    if not url:
        return True
    low = url.strip().lower()
    if ":" in low[:8] and not low.startswith(("http://", "https://")):
        if not (low.startswith("http://") or low.startswith("https://")):
            return True
    parsed = _split_url(low)
    if parsed is None:
        return True
    scheme, host, port = parsed
    if scheme not in ("http", "https"):
        return True
    host = host.rstrip(".")
    if host in _LOCAL_HOSTNAMES:
        return not allow_loopback
    # literal IP forms
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback or ip.is_link_local or ip.is_private \
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return not allow_loopback
        return False
    # hostname without dots → metadata-ish internal name
    if "." not in host and ":" not in host:
        return not allow_loopback
    if "127.0.0.1" in host or "169.254.169.254" in host:
        return not allow_loopback
    if resolve_dns:
        try:
            for info in socket.getaddrinfo(host, port or 80,
                                           proto=socket.IPPROTO_TCP):
                try:
                    ip = ipaddress.ip_address(info[4][0])
                except ValueError:
                    continue
                if ip.is_loopback or ip.is_link_local or ip.is_private \
                        or ip.is_reserved or ip.is_unspecified or ip.is_multicast:
                    return not allow_loopback
        except (socket.gaierror, OSError):
            pass
    return False


def _split_url(url: str):
    """Minimal URL split: returns (scheme, host, port) or None."""
    m = re.match(r"^(https?)://([^/:?#]+)(?::(\d+))?", url)
    if not m:
        return None
    return m.group(1), m.group(2).lower(), m.group(3)


def allow_url(url: str, allow_private: bool = False) -> bool:
    """True when safe to fetch/give to the browser. SSRF guard call-site."""
    if allow_private:
        return bool(url)
    return not risky_url(url)