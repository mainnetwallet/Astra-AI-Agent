"""Runtime backends and how one gets selected.

    RuntimeEngine
        |
        +-- ProotRuntimeBackend   Android/Termux  -> proot + proot-distro
        +-- WslRuntimeBackend     Windows         -> WSL2 Ubuntu

`select_backend()` implements the `RUNTIME_BACKEND=auto` rule:

    Android/Termux/Linux  -> proot
    Windows               -> wsl2  (WSL2 + Ubuntu)

and honours an explicit `RUNTIME_BACKEND=proot|wsl2`. An incompatible or
unknown selection produces a backend that reports itself UNAVAILABLE with a
clear reason instead of silently running on the wrong thing (or on the host).
"""
from __future__ import annotations

from astra.runtime.backends.base import (AstraRuntimeUnavailable,
                                         RuntimeBackend, detect_platform)
from astra.runtime.backends.proot import ProotRuntimeBackend
from astra.runtime.backends.wsl import DEFAULT_DISTRO, WslRuntimeBackend

BACKEND_AUTO = "auto"
BACKEND_PROOT = "proot"
BACKEND_WSL2 = "wsl2"

# `wsl` is accepted as a friendly alias for diagnostics.
_ALIASES = {
    "": BACKEND_AUTO,
    "auto": BACKEND_AUTO,
    "proot": BACKEND_PROOT,
    "wsl": BACKEND_WSL2,
    "wsl2": BACKEND_WSL2,
}


class InvalidBackend(RuntimeBackend):
    """A selection nothing can satisfy: always unavailable, always explains."""

    def __init__(self, config=None, *, requested: str = "", platform: str = ""):
        super().__init__(config)
        self.name = str(requested or "unknown")
        self.platform = platform or detect_platform()
        self._reason = ("RUNTIME_BACKEND=%s is not a known runtime backend "
                        "(use `auto`, `proot` or `wsl2`)" % (requested or "?"))

    def probe(self, *, refresh: bool = False) -> dict:
        if self._probe is None:
            self._probe = {
                "available": False, "backend": self.name,
                "platform": self.platform, "container": "", "distro": "",
                "shell": "", "workspace": "/workspace", "rootfs": "",
                "host_isolation": False, "issues": [self._reason],
                "reason": self._reason, "hint": "", "details": {},
            }
        return dict(self._probe)


def select_backend(config=None, *, prefix=None, name=None,
                   runner=None) -> RuntimeBackend:
    """Pick the runtime backend for this host (see the module docstring).

    `runner` is handed to the WSL2 backend as its `wsl.exe` adapter. The
    probe, the launch and one-shot execution all go through that single seam,
    so a test can drive the whole backend without a real WSL - and in
    production it is None, so the only thing that ever spawns `wsl.exe` is the
    one adapter that is supposed to.
    """
    platform = detect_platform()
    if name is not None:
        requested = str(name)
    else:
        requested = str(_cfg(config, "RUNTIME_BACKEND", BACKEND_AUTO)
                        or BACKEND_AUTO)
        if prefix is not None:
            # An explicit Termux prefix is a proot-only argument (diagnostics
            # and tests use it to point at a different rootfs tree).
            requested = BACKEND_PROOT
    key = _ALIASES.get(requested.strip().lower())
    if key is None:
        return InvalidBackend(config, requested=requested, platform=platform)
    if key == BACKEND_PROOT:
        return ProotRuntimeBackend(config, prefix=prefix)
    if key == BACKEND_WSL2:
        return WslRuntimeBackend(config, runner=runner)
    if platform == "windows":
        return WslRuntimeBackend(config, runner=runner)
    return ProotRuntimeBackend(config, prefix=prefix)


def _cfg(config, key: str, default=None):
    if config is None:
        return default
    try:
        value = config.get(key)
    except Exception:
        return default
    return default if value in (None, "") else value


__all__ = ["AstraRuntimeUnavailable", "RuntimeBackend",
           "ProotRuntimeBackend", "WslRuntimeBackend", "InvalidBackend",
           "select_backend", "detect_platform", "DEFAULT_DISTRO",
           "BACKEND_AUTO", "BACKEND_PROOT", "BACKEND_WSL2"]
