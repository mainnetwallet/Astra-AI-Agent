"""Real host telemetry (CPU / memory / disk / network) for ``GET /api/metrics``.

Aggregate numbers only: no usernames, environment, MAC/IP addresses, process
command lines or filesystem paths ever leave this module.

``psutil`` is optional. When it is missing (or a platform refuses a given
counter, e.g. ``/proc`` restrictions on newer Android/Termux) the affected
section is reported as ``None`` and ``available`` reflects the truth — Astra
itself never depends on telemetry being present, and nothing is estimated.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

try:
    import psutil
except ImportError:          # pragma: no cover - exercised via monkeypatch in tests
    psutil = None

# Rates are only meaningful between two reasonably close samples. A baseline
# older than this is discarded and re-established rather than reporting an
# average over an idle stretch as if it were the current throughput.
_MAX_RATE_WINDOW_S = 120.0
# Two samples closer than this share the previous rate (avoids jitter/div-by-0
# when several clients hit /api/metrics at once).
_MIN_RATE_WINDOW_S = 0.2
# psutil.cpu_percent(None) measures since the previous call; below ~0.1s the
# reading is dominated by scheduler noise, so closer calls (several tabs at
# once) reuse the last real reading instead of taking a meaningless one.
_MIN_CPU_WINDOW_S = 0.1


_LOOPBACK = re.compile(r"^(lo\d*|loopback.*)$", re.I)   # lo, lo0, "Loopback Pseudo-Interface 1"


def _is_loopback(name) -> bool:
    return bool(_LOOPBACK.match(str(name).strip()))


def _pct(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:               # NaN
        return None
    return round(max(0.0, min(100.0, v)), 1)


def _disk_candidates() -> list[str]:
    """Filesystems to try, most-representative first. Never hard-codes '/' on Windows."""
    out: list[str] = []
    if os.name == "nt":
        drive = os.environ.get("SystemDrive") or os.path.splitdrive(sys.executable or "")[0]
        if drive:
            out.append(drive.rstrip("\\/") + "\\")
    else:
        out.append(os.sep)
    try:
        cwd_anchor = os.path.splitdrive(os.getcwd())[0]
        if cwd_anchor:
            out.append(cwd_anchor + "\\")
    except OSError:
        pass
    out.append(os.path.expanduser("~"))       # Termux: '/' is often unreadable
    try:
        out.append(os.getcwd())
    except OSError:
        pass
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


class HostMetrics:
    """Thread-safe sampler. One instance per process; cheap to call."""

    def __init__(self, psutil_module="__default__") -> None:
        self._ps = psutil if psutil_module == "__default__" else psutil_module
        self._lock = threading.Lock()
        self._net_prev = None          # (monotonic_ts, bytes_sent, bytes_recv)
        self._net_rates = (None, None)  # (upload_bps, download_bps)
        self._cpu_last = None          # (monotonic_ts, percent) of the last real reading
        if self._ps is not None:
            # Prime both baselines so the FIRST real request is already a
            # meaningful delta (psutil's first cpu_percent() call is always 0.0).
            try:
                self._ps.cpu_percent(interval=None)     # warm-up; value discarded
            except Exception:
                pass
            try:
                self._net_prev = self._net_sample()
            except Exception:
                pass

    # ----------------------------------------------------------- sections
    def _cpu(self) -> dict | None:
        # Non-blocking: interval=None compares against the previous call, so no
        # request ever sleeps. The baseline is primed in __init__.
        now = time.monotonic()
        last = self._cpu_last
        if last is not None and now - last[0] < _MIN_CPU_WINDOW_S:
            return {"percent": last[1]}
        try:
            p = _pct(self._ps.cpu_percent(interval=None))
        except Exception:
            return None
        if p is None:
            return None
        self._cpu_last = (now, p)
        return {"percent": p}

    def _memory(self) -> dict | None:
        try:
            vm = self._ps.virtual_memory()
            p = _pct(vm.percent)
            if p is None:
                return None
            return {"percent": p, "used_bytes": int(vm.total - vm.available),
                    "total_bytes": int(vm.total), "available_bytes": int(vm.available)}
        except Exception:
            return None

    def _disk(self) -> dict | None:
        for path in _disk_candidates():
            try:
                du = self._ps.disk_usage(path)
                p = _pct(du.percent)
                if p is None:
                    continue
                return {"percent": p, "used_bytes": int(du.used),
                        "total_bytes": int(du.total), "free_bytes": int(du.free)}
            except Exception:
                continue
        return None

    def _net_sample(self):
        """Host network counters EXCLUDING loopback: local traffic (including this
        panel's own polling) is not network activity, and counting it would keep
        an idle machine from ever measuring 0 B/s. Falls back to psutil's
        aggregate counter where per-interface data is unavailable (e.g. Android)."""
        try:
            per = self._ps.net_io_counters(pernic=True)
        except Exception:
            per = None
        if isinstance(per, dict):
            real = [c for name, c in per.items() if not _is_loopback(name)]
            return (time.monotonic(), sum(int(c.bytes_sent) for c in real),
                    sum(int(c.bytes_recv) for c in real))
        c = self._ps.net_io_counters()
        if c is None:
            return None
        return time.monotonic(), int(c.bytes_sent), int(c.bytes_recv)

    def _network(self) -> dict | None:
        try:
            cur = self._net_sample()
        except Exception:
            return None
        if cur is None:
            return None
        now, sent, recv = cur
        prev = self._net_prev
        if prev is None or now - prev[0] > _MAX_RATE_WINDOW_S or sent < prev[1] or recv < prev[2]:
            # first sample, stale baseline, or counter reset/wrap: re-baseline
            self._net_prev = cur
            self._net_rates = (None, None)
        elif now - prev[0] >= _MIN_RATE_WINDOW_S:
            dt = now - prev[0]
            self._net_rates = (round((sent - prev[1]) / dt), round((recv - prev[2]) / dt))
            self._net_prev = cur
        up, down = self._net_rates
        return {"bytes_sent": sent, "bytes_recv": recv,
                "upload_bps": up, "download_bps": down}

    # ------------------------------------------------------------- public
    def snapshot(self) -> dict:
        if self._ps is None:
            return {"available": False, "reason": "psutil not installed",
                    "cpu": None, "memory": None, "disk": None, "network": None}
        with self._lock:
            out = {"cpu": self._cpu(), "memory": self._memory(),
                   "disk": self._disk(), "network": self._network()}
        out["available"] = any(v is not None for v in out.values())
        if not out["available"]:
            out["reason"] = "host telemetry not readable on this platform"
        out["sampled_at"] = round(time.time(), 3)
        return out


_INSTANCE: HostMetrics | None = None
_INSTANCE_LOCK = threading.Lock()


def get_host_metrics() -> HostMetrics:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = HostMetrics()
        return _INSTANCE


def resources_snapshot() -> dict:
    """Never raises: telemetry must not be able to break /api/metrics."""
    try:
        return get_host_metrics().snapshot()
    except Exception:
        return {"available": False, "reason": "host telemetry error",
                "cpu": None, "memory": None, "disk": None, "network": None}


try:                          # prime the CPU/network baselines at startup so the
    get_host_metrics()        # first /api/metrics call is already a real delta
except Exception:             # pragma: no cover - telemetry must never block startup
    pass
