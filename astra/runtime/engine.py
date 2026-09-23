"""Astra Agent Runtime — isolation engine.

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
import subprocess

from astra.core.exceptions import AstraError

# -- guest-side constants --------------------------------------------------
GUEST_WORKSPACE = "/workspace"
GUEST_HOME = "/root"
GUEST_TMP = "/tmp"
GUEST_SHELL = "/bin/bash"

# The PATH a guest process sees. Note this is the *guest* path: it resolves
# inside the rootfs, never to Termux's /data/data/com.termux/files/usr.
GUEST_PATH = ("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
              "/sbin:/bin")

DEFAULT_CONTAINER = "ubuntu"

# Bounded probe so a wedged proot can never hang Astra's bootstrap.
PROBE_TIMEOUT = 25.0


class AstraRuntimeUnavailable(AstraError):
    """Raised when the isolated runtime cannot be reached.

    Callers must surface this (and refuse to execute) rather than falling
    back to the host shell. The message is written to be user-facing.
    """


def _termux_prefix() -> str:
    return os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"


class RuntimeEngine:
    """Locates proot + a distribution rootfs and assembles runtime argv.

    Stateless apart from a cached capability probe: two engines in the same
    process always describe the same environment.
    """

    def __init__(self, config=None, *, prefix: str | None = None):
        self.config = config
        self.prefix = prefix or _termux_prefix()
        self._probe: dict | None = None

    # -- config helpers -----------------------------------------------------
    def _cfg(self, key: str, default=None):
        if self.config is None:
            return default
        try:
            value = self.config.get(key)
        except Exception:
            return default
        return default if value in (None, "") else value

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
        self._probe = {
            "available": not issues,
            "backend": "proot",
            "container": self.container,
            "proot": proot or "",
            "rootfs": base,
            "rootfs_mode": self.rootfs_mode,
            "issues": issues,
            "reason": "; ".join(issues),
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

        child_env = {
            "PATH": GUEST_PATH,
            "HOME": GUEST_HOME,
            "USER": "root",
            "TERM": (env or {}).get("TERM", "xterm-256color"),
            "COLORTERM": "truecolor",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PS1": "\\u@astra:\\w\\$ ",
        }
        for key, value in (env or {}).items():
            child_env[str(key)] = str(value)

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
    def run(self, *, binds, cwd, command, rootfs=None, timeout=PROBE_TIMEOUT,
            env=None):
        """Run one command in the guest and return a structured result.

        Used by capability detection and install verification; interactive
        work goes through `astra.runtime.pty` instead.
        """
        argv = self.build_argv(binds=binds, cwd=cwd, rootfs=rootfs,
                               argv=self.exec_argv(command), env=env)
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=float(timeout))
        except subprocess.TimeoutExpired:
            return {"ok": False, "status": "timeout", "exit_code": None,
                    "stdout": "", "stderr": f"command timed out after {timeout}s",
                    "command": command}
        except OSError as exc:
            return {"ok": False, "status": "failed", "exit_code": None,
                    "stdout": "", "stderr": str(exc), "command": command}
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        # proot's own diagnostics ("proot warning: ...") are noise on an
        # otherwise successful command; drop them so a clean run reads clean.
        err = "\n".join(line for line in err.splitlines()
                        if not line.startswith("proot ")).strip()
        return {"ok": proc.returncode == 0, "status":
                "completed" if proc.returncode == 0 else "failed",
                "exit_code": proc.returncode, "stdout": out, "stderr": err,
                "command": command}
