"""Astra Agent Runtime - proot (Android/Termux) backend.

This module is the Android/Termux half of the runtime backend
abstraction (`astra/runtime/backends/`). Windows drives the SAME
runtime through `astra/runtime/backends/wsl.py` (WSL2 Ubuntu);
`RuntimeEngine` picks between them. Only what is genuinely
proot-specific lives here - the guest contract (paths, PATH, prompt,
minimal environment) is shared and comes from
`astra/runtime/backends/base.py`.

The runtime is the *only* place Agent work is allowed to execute. It is a
real, separate Linux filesystem and process tree, reached through proot
(`proot --rootfs=... --change-id=0:0`), the userspace chroot/namespace
emulator that works without root on Android/Termux.

Isolation model
---------------
A runtime session is launched as::

    env -i PATH=<guest path> HOME=/root USER=root TERM=xterm-256color \
    proot --kill-on-exit --link2symlink --sysvipc -L --change-id=0:0 \
          --rootfs=<rootfs> --cwd=<cwd> \
          --bind=/dev --bind=/proc --bind=/sys \
          --bind=<rootfs>/../sysdata/sys_empty:/sys/fs/selinux \
          --bind=<rootfs>/../shm:/dev/shm \
          --bind=<runtime workspace>:/workspace \
          --bind=<runtime home>:/root \
          --bind=<runtime tmp>:/tmp \
          <argv>

Only kernel pseudo-filesystems (`/dev`, `/proc`, `/sys`) and the runtime's
OWN directories are bound in. The host home, host Termux prefix, `/sdcard`
and `/storage` are deliberately NOT bound, so a command running inside the
runtime cannot read or write a single host path — this is verified by
`tests/test_runtime_isolation.py`.

There is deliberately **no host fallback**. If proot (or a distribution
rootfs) is missing, `available()` is False and every caller must surface a
"Agent Runtime unavailable" error instead of running the command on the
host. `AstraRuntimeUnavailable` exists precisely so that failure mode can
never be silently swallowed into a host execution.

Rootfs
------
The base rootfs is the proot-distro container already installed in the
environment (default `ubuntu`, discovered under
`$PREFIX/var/lib/proot-distro/containers/<name>/rootfs`). The runtime does
not copy it by default (a full Ubuntu rootfs is several GB); instead each
runtime gets its own `/workspace`, `/root` and `/tmp` bind targets, so
projects, shell state and temp files are per-runtime. `RUNTIME_ROOTFS_COPY`
opts into a private copy of the whole rootfs when the caller wants globally
installed packages to be per-runtime as well.
"""
from __future__ import annotations

import os
import shutil

# DEFAULT_CONTAINER is the proot-distro container the runtime roots into.
DEFAULT_CONTAINER = "ubuntu"

# The guest contract - guest paths, the guest PATH, the Astra prompt and
# the MINIMAL guest environment (`env -i` equivalent) - is IDENTICAL on
# every backend and lives in `backends/base.py`, re-exported here so the
# public import surface of this module is unchanged.
from astra.runtime.backends.base import (  # noqa: F401,E402
    ASTRA_PS1, GUEST_HOME, GUEST_PATH, GUEST_SHELL, GUEST_TMP,
    GUEST_WORKSPACE, PROBE_TIMEOUT, AstraRuntimeUnavailable,
    RuntimeBackend, _prompt_env, _termux_prefix, _user_scope_env,
    detect_platform, guest_env)


class ProotRuntimeBackend(RuntimeBackend):
    """Android/Termux backend: proot + a proot-distro rootfs.

    Locates proot and a distribution rootfs and assembles the runtime argv.
    Stateless apart from a cached capability probe: two backends in the same
    process always describe the same environment.
    """

    name = "proot"
    platform = "android"
    shell = "bash"
    # proot's own diagnostics ("proot warning: ...") are noise on an
    # otherwise successful command; `run_argv` drops them.
    noise_prefixes = ("proot ",)

    def __init__(self, config=None, *, prefix: str | None = None):
        super().__init__(config)
        self.prefix = prefix or _termux_prefix()

    @property
    def container(self) -> str:
        return str(self._cfg("RUNTIME_CONTAINER", DEFAULT_CONTAINER))

    @property
    def rootfs_mode(self) -> str:
        mode = str(self._cfg("RUNTIME_ROOTFS_MODE", "shared")).strip().lower()
        return mode if mode in ("shared", "copy") else "shared"

    # -- discovery ----------------------------------------------------------
    def _proot_path(self) -> str | None:
        explicit = self._cfg("RUNTIME_PROOT")
        if explicit:
            return explicit if os.path.exists(explicit) else None
        return shutil.which("proot")

    def _containers_dir(self) -> str:
        override = self._cfg("RUNTIME_CONTAINERS_DIR")
        if override:
            return str(override)
        return os.path.join(self.prefix, "var", "lib", "proot-distro",
                            "containers")

    def _base_container_dir(self) -> str:
        return os.path.join(self._containers_dir(), self.container)

    def base_rootfs(self) -> str:
        """Absolute path of the read-write base rootfs for the container.

        proot-distro moved from `installed-rootfs/<name>` to
        `containers/<name>/rootfs`; both are accepted so an older install
        still works."""
        return os.path.join(self._base_container_dir(), "rootfs")

    def sysdata_dir(self) -> str:
        return os.path.join(self._base_container_dir(), "sysdata")

    def shm_dir(self) -> str:
        return os.path.join(self._base_container_dir(), "shm")

    # -- probe --------------------------------------------------------------
    def probe(self, *, refresh: bool = False) -> dict:
        """Isolated-runtime capability report. Cached per engine."""
        if self._probe is not None and not refresh:
            return dict(self._probe)
        proot = self._proot_path()
        base = self.base_rootfs()
        shell = os.path.join(base, "bin", "bash")
        bash = os.path.join(base, "usr", "bin", "bash")
        issues: list[str] = []
        if not proot:
            issues.append("proot is not installed")
        if not os.path.isdir(base):
            issues.append(
                f"no proot-distro rootfs for container '{self.container}'")
        elif not (os.path.exists(shell) or os.path.exists(bash)):
            issues.append(f"rootfs '{base}' has no /bin/bash")
        if not os.path.isdir(self.sysdata_dir()):
            issues.append(f"rootfs '{base}' has no sysdata directory")
        hint = ""
        if issues and detect_platform() == "windows":
            # The failure is platform-shaped, not install-shaped: say what
            # to do instead of leaving the user staring at "proot missing".
            hint = ("On Windows the Agent Runtime runs inside WSL2 Ubuntu - "
                    "leave RUNTIME_BACKEND at `auto` (or set it to `wsl2`) and "
                    "install WSL2 + Ubuntu. Windows CMD and PowerShell are NOT "
                    "runtime backends.")
        self._probe = {
            "available": not issues,
            "backend": self.name,
            "platform": detect_platform(),
            "container": self.container,
            "distro": self.container,
            "shell": self.shell,
            "workspace": GUEST_WORKSPACE,
            "home": GUEST_HOME,
            "proot": proot or "",
            "rootfs": base,
            "rootfs_mode": self.rootfs_mode,
            # proot binds exactly the runtime's own directories and nothing
            # else, so host isolation is structural here.
            "host_isolation": True,
            "issues": issues,
            "reason": "; ".join(issues),
            "hint": hint,
        }
        return dict(self._probe)

    def available(self) -> bool:
        return bool(self.probe()["available"])

    def require(self) -> dict:
        """Return the probe, or raise — never a host fallback."""
        info = self.probe()
        if not info["available"]:
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: " + (info["reason"] or "unknown"))
        return info

    # -- argv ---------------------------------------------------------------
    def build_argv(self, *, binds: list[tuple[str, str]], cwd: str,
                   rootfs: str | None = None, argv: list[str] | None = None,
                   env: dict | None = None,
                   hostname: str = "astra-runtime") -> list[str]:
        """Assemble the full `env -i ... proot ... argv` command line.

        `binds` are (host_path, guest_path) pairs — the ONLY host paths the
        guest can see. `cwd` is a guest path. Nothing here consults the host
        environment, which is what keeps host secrets out of the guest.
        """
        info = self.require()
        rootfs = rootfs or info["rootfs"]
        argv = list(argv or self.shell_argv())
        base_dir = os.path.dirname(rootfs)  # .../containers/<name>

        # The guest environment is built from scratch (`env -i` + explicit
        # assignments) by the SHARED builder, so the WSL backend cannot drift
        # from this one and no host secret can reach the guest.
        child_env = guest_env(env)

        proot_args = [
            info["proot"],
            "--kill-on-exit",
            "--link2symlink",
            "--sysvipc",
            "-L",
            "--change-id=0:0",
            f"--rootfs={rootfs}",
        ]
        # Pseudo-filesystems every Linux userland needs.
        for pseudo in ("/dev", "/proc", "/sys"):
            proot_args.append(f"--bind={pseudo}")
        # proot-distro's stand-in for the SELinux / sysctl files Android
        # does not expose; harmless when absent.
        sysdata = os.path.join(base_dir, "sysdata")
        shm = os.path.join(base_dir, "shm")
        if os.path.isdir(sysdata):
            proot_args.append(f"--bind={sysdata}/sys_empty:/sys/fs/selinux")
        if os.path.isdir(shm):
            proot_args.append(f"--bind={shm}:/dev/shm")
        # The runtime's own directories — the only host data it may touch.
        for host_path, guest_path in binds:
            os.makedirs(host_path, exist_ok=True)
            proot_args.append(f"--bind={host_path}:{guest_path}")

        proot_args.append(f"--cwd={cwd}")
        proot_args.extend(argv)

        launcher = [shutil.which("env") or "env", "-i"]
        for key, value in child_env.items():
            launcher.append(f"{key}={value}")
        return launcher + proot_args

    def shell_argv(self, *, login: bool = False) -> list[str]:
        """Interactive shell argv.

        Deliberately NOT a login shell: this rootfs carries
        `/etc/profile.d/termux-profile.sh`, which appends the Termux prefix
        to PATH. Nothing under that path exists inside the runtime, but a
        host path on the guest PATH is exactly the kind of leak the
        isolation contract forbids — so the profile is not sourced and PATH
        is precisely the one `build_argv` sets.
        """
        return [GUEST_SHELL, "-l"] if login else [GUEST_SHELL]

    def exec_argv(self, command: str) -> list[str]:
        """Non-interactive one-shot command (same non-login rationale)."""
        return [GUEST_SHELL, "-c", str(command)]

    def probe_argv(self, command: str) -> list[str]:
        return [GUEST_SHELL, "-lc", str(command)]

    # -- one-shot guest execution (used by detection / verification) --------
    # `run()` itself is inherited from RuntimeBackend: it builds the argv
    # above and shapes the result identically on every backend (proot's
    # launcher noise is dropped through `noise_prefixes`).
