"""Astra Agent Runtime - isolation engine (backend facade).

The runtime is the *only* place Agent work is allowed to execute, and it is
always a real, separate Linux userland that belongs to the runtime and never
to the host. Which mechanism provides that userland is the backend's job:

    RuntimeEngine
        |
        +-- ProotRuntimeBackend   Android/Termux  -> proot + proot-distro
        +-- WslRuntimeBackend     Windows         -> WSL2 Ubuntu

`RuntimeEngine` is the single entry point every other module talks to
(`RuntimeManager`, the runtime tools, the Gateway, the Providers, the web
API, the Astra Agent Terminal). It selects a backend and delegates, so no
caller needs to know whether a session is running under proot on Android or
inside WSL2 Ubuntu on Windows.

There is deliberately **no host fallback**. If the selected backend cannot be
reached - proot missing, WSL2/Ubuntu missing, the namespace unusable - then
`available()` is False, `require()` raises `AstraRuntimeUnavailable`, and
every caller must surface "Agent Runtime unavailable" instead of running the
command on the host (`cmd.exe`/PowerShell are NOT runtime backends).

See `astra/runtime/backends/base.py` for the shared guest contract (paths,
guest PATH, the `astra:<cwd>$` prompt and the minimal environment) and
`docs/AGENT_RUNTIME.md` for the full model.
"""
from __future__ import annotations

from astra.runtime.backends import (BACKEND_AUTO, BACKEND_PROOT, BACKEND_WSL2,
                                    InvalidBackend, ProotRuntimeBackend,
                                    RuntimeBackend, WslRuntimeBackend,
                                    detect_platform, select_backend)
from astra.runtime.backends.base import (ASTRA_PS1, GUEST_HOME, GUEST_PATH,
                                         GUEST_SHELL, GUEST_TMP,
                                         GUEST_WORKSPACE, PROBE_TIMEOUT,
                                         AstraRuntimeUnavailable, guest_env)
from astra.runtime.backends.base import _prompt_env, _termux_prefix
from astra.runtime.backends.base import _user_scope_env
from astra.runtime.backends.proot import DEFAULT_CONTAINER

__all__ = [
    "RuntimeEngine", "AstraRuntimeUnavailable", "RuntimeBackend",
    "ProotRuntimeBackend", "WslRuntimeBackend", "InvalidBackend",
    "select_backend", "detect_platform", "guest_env",
    "GUEST_WORKSPACE", "GUEST_HOME", "GUEST_TMP", "GUEST_SHELL", "GUEST_PATH",
    "ASTRA_PS1", "PROBE_TIMEOUT", "DEFAULT_CONTAINER",
    "BACKEND_AUTO", "BACKEND_PROOT", "BACKEND_WSL2",
]


class RuntimeEngine:
    """Backend-agnostic access to the isolated Agent Runtime.

    `RuntimeEngine(config)` selects the backend for this host automatically
    (`RUNTIME_BACKEND=auto`); `backend=` overrides it explicitly and
    `prefix=` selects the proot backend with a non-default Termux prefix
    (diagnostics and tests).
    """

    def __init__(self, config=None, *, prefix: str | None = None,
                 backend=None):
        self.config = config
        self.prefix = prefix or _termux_prefix()
        self.backend = select_backend(config, prefix=prefix, name=backend)
        self._proot_view: ProotRuntimeBackend | None = None

    # -- identity -----------------------------------------------------------
    @property
    def name(self) -> str:
        return str(getattr(self.backend, "name", "") or "")

    @property
    def platform(self) -> str:
        """The HOST platform ("windows", "android", "linux", "macos")."""
        return detect_platform()

    @property
    def container(self) -> str:
        return str(getattr(self.backend, "container", "")
                   or getattr(self.backend, "distro", "") or "")

    @property
    def rootfs_mode(self) -> str:
        return str(getattr(self.backend, "rootfs_mode", "shared")
                   or "shared")

    # -- discovery ----------------------------------------------------------
    def probe(self, *, refresh: bool = False) -> dict:
        return self.backend.probe(refresh=refresh)

    def available(self) -> bool:
        return self.backend.available()

    def require(self) -> dict:
        return self.backend.require()

    # -- launch -------------------------------------------------------------
    def build_argv(self, **kwargs) -> list:
        return self.backend.build_argv(**kwargs)

    def pty_argv(self, **kwargs) -> list:
        return self.backend.pty_argv(**kwargs)

    def shell_argv(self, **kwargs) -> list:
        return self.backend.shell_argv(**kwargs)

    def exec_argv(self, command: str) -> list:
        return self.backend.exec_argv(command)

    def probe_argv(self, command: str) -> list:
        return self.backend.probe_argv(command)

    def run(self, **kwargs) -> dict:
        return self.backend.run(**kwargs)

    # -- layout -------------------------------------------------------------
    def host_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """Host paths backing /workspace, /root and /tmp for one runtime."""
        return self.backend.host_dirs(runtime_id, base_dir)

    def guest_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """The same three directories as the GUEST sees them."""
        return self.backend.guest_dirs(runtime_id, base_dir)

    def guest_source_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """Where those directories really live inside the guest.

        Identical to `guest_dirs` on proot; on Windows it is the runtime's
        own tree inside Ubuntu, which the session binds onto /workspace,
        /root and /tmp.
        """
        return self.backend.guest_source_dirs(runtime_id, base_dir)

    def prepare(self, runtime_id: str, base_dir: str) -> None:
        """Create/refresh the runtime's directories inside the guest."""
        return self.backend.prepare(runtime_id, base_dir)

    def pty_class(self):
        """PTY implementation for this backend (POSIX PTY, or the WSL
        transport that drives a PTY inside Ubuntu)."""
        return self.backend.pty_class()

    # -- proot rootfs discovery (documented proot API, kept for callers) ----
    def _proot(self) -> ProotRuntimeBackend:
        if isinstance(self.backend, ProotRuntimeBackend):
            return self.backend
        if self._proot_view is None:
            self._proot_view = ProotRuntimeBackend(self.config,
                                                   prefix=self.prefix)
        return self._proot_view

    def base_rootfs(self) -> str:
        return self._proot().base_rootfs()

    def sysdata_dir(self) -> str:
        return self._proot().sysdata_dir()

    def shm_dir(self) -> str:
        return self._proot().shm_dir()
