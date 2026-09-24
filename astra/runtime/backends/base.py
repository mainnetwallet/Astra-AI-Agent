"""Shared contract for Astra Agent Runtime backends.

The Agent Runtime is the *only* place Agent work is allowed to execute, and
it is always a real, separate Linux userland: a shell, a filesystem, a
process tree and a PTY that belong to the runtime and never to the host.
*Which* mechanism provides that userland is the backend's business:

    RuntimeEngine
        |
        +-- ProotRuntimeBackend   Android/Termux  -> proot + proot-distro
        +-- WslRuntimeBackend     Windows         -> WSL2 Ubuntu

Everything the two platforms share lives here and in the modules above the
backends (`manager.py`, `pty.py`, `files.py`, `packages.py`, `tools.py`):
runtime sessions, the workspace, file operations, package management, the
guest environment, the Astra prompt, execution history, BlobStore, SSE,
reconnect, terminal tabs and the AgentToolLoop integration. A backend only
implements what genuinely differs: discovery/probe, how the host launches a
guest process, and where the runtime's guest directories live.

Two rules hold for every backend:

1. **No host fallback.** If the backend cannot be reached, `require()`
   raises `AstraRuntimeUnavailable` and callers must surface it. Nothing
   ever downgrades to `cmd.exe`, PowerShell or the host shell on its own.
2. **Minimal environment.** The guest environment is constructed explicitly
   (`guest_env`), never inherited, so host secrets (PATH, credentials,
   tokens, SSH keys, user profile) cannot leak into the runtime.
"""
from __future__ import annotations

import os
import sys

from astra.core.exceptions import AstraError

# -- guest-side constants --------------------------------------------------
# These are the paths the AGENT sees, on every platform. `/root` and `/tmp`
# are per-runtime directories (see `host_dirs`/`guest_dirs` on each backend),
# so a command inside runtime A cannot see runtime B's shell state either.
GUEST_WORKSPACE = "/workspace"
GUEST_HOME = "/root"
GUEST_TMP = "/tmp"
GUEST_SHELL = "/bin/bash"

# The PATH a guest process sees. Note this is the *guest* path: it resolves
# inside the runtime, never to Termux's /data/data/com.termux/files/usr and
# never to WSL's /mnt/c/... Windows PATH entries.
#
# The two trailing entries are the per-runtime USER-SCOPE install targets
# (`HOME` maps to this runtime's own directory), which is what makes a
# `pip install --user` / `npm install -g` in runtime A invisible to runtime
# B while the multi-GB distro image stays shared - see `_user_scope_env`.
GUEST_PATH = ("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
              "/sbin:/bin:/root/.local/bin:/root/.npm-global/bin")


# The prompt the Agent Terminal shows: `astra:/workspace$` (short enough for a
# phone screen). Colours are wrapped in \[ \] so bash measures line width right.
ASTRA_PS1 = ("\\[\\e[1;35m\\]astra\\[\\e[0m\\]:"
             "\\[\\e[1;34m\\]\\w\\[\\e[0m\\]$ ")

# Bounded probe so a wedged backend can never hang Astra's bootstrap.
PROBE_TIMEOUT = 25.0


def _prompt_env() -> dict:
    """Env that makes the interactive prompt `astra:<cwd>$`.

    A bare `PS1=` in the environment is NOT enough: the guest image's
    `~/.bashrc` or `/etc/profile` can overwrite it with the default
    user@host:cwd prompt (which is where `root@localhost:/workspace#` came
    from). `PROMPT_COMMAND` runs just before the first prompt - i.e. after
    those files - so it applies ours once, then removes itself so any PS1
    the user sets later sticks.
    """
    return {
        "PS1": ASTRA_PS1,
        "PROMPT_COMMAND": f"PS1='{ASTRA_PS1}'; unset PROMPT_COMMAND",
    }


def _user_scope_env() -> dict:
    """Environment that redirects every user-scope package manager into
    THIS runtime's private `$HOME`.

    `HOME` (`/root`) maps to `<runtime dir>/root`, a directory only this
    runtime owns. Pointing pip / npm / cargo / go / gem / XDG at paths under
    it means a package installed inside one runtime lands in that runtime's
    own writable state and is invisible to every other runtime - even though
    the distro image itself is shared. System-level installs (`apt`, `apk`)
    still write to the shared image; set `RUNTIME_ROOTFS_MODE=copy` (proot)
    for a fully private rootfs.

    `PIP_USER` is deliberately NOT exported. It would force `--user` on every
    pip invocation, and pip REFUSES that inside a virtualenv ("user
    site-packages are not visible in this virtualenv") - which would break
    the ordinary `python3 -m venv && pip install` workflow the runtime must
    support. Privacy is delivered explicitly instead: the runtime's own
    installer passes `--user` (`astra/runtime/packages.py::plan_install`),
    and `PYTHONUSERBASE` decides WHERE that lands, so a user-scope install
    is still per-runtime private.
    """
    return {
        "PYTHONUSERBASE": f"{GUEST_HOME}/.local",
        "PIP_CACHE_DIR": f"{GUEST_HOME}/.cache/pip",
        "NPM_CONFIG_PREFIX": f"{GUEST_HOME}/.npm-global",
        "npm_config_prefix": f"{GUEST_HOME}/.npm-global",
        "NPM_CONFIG_CACHE": f"{GUEST_HOME}/.npm",
        "npm_config_cache": f"{GUEST_HOME}/.npm",
        "NODE_PATH": f"{GUEST_HOME}/.npm-global/lib/node_modules",
        "CARGO_HOME": f"{GUEST_HOME}/.cargo",
        "GOPATH": f"{GUEST_HOME}/go",
        "GEM_HOME": f"{GUEST_HOME}/.gem",
        "XDG_DATA_HOME": f"{GUEST_HOME}/.local/share",
        "XDG_CONFIG_HOME": f"{GUEST_HOME}/.config",
        "XDG_CACHE_HOME": f"{GUEST_HOME}/.cache",
    }


def guest_env(env: dict | None = None) -> dict:
    """The complete, minimal environment a guest process receives.

    Built from scratch - never from `os.environ` - which is what keeps the
    host's PATH, credentials and profile out of the runtime. `env` overrides
    individual keys (TERM, and anything a caller needs to pass through).
    """
    child = {
        "PATH": GUEST_PATH,
        "HOME": GUEST_HOME,
        "USER": "root",
        "LOGNAME": "root",
        "SHELL": GUEST_SHELL,
        "TERM": (env or {}).get("TERM", "xterm-256color"),
        "COLORTERM": "truecolor",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    child.update(_prompt_env())
    # Per-runtime writable package state (see `_user_scope_env`).
    child.update(_user_scope_env())
    for key, value in (env or {}).items():
        child[str(key)] = str(value)
    return child


def _termux_prefix() -> str:
    return os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"


def detect_platform() -> str:
    """Which host Astra is running on: `windows`, `android`, `macos`, `linux`."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if (os.environ.get("ANDROID_ROOT") or os.environ.get("TERMUX_VERSION")
            or "com.termux" in _termux_prefix()):
        return "android"
    return "linux"


class AstraRuntimeUnavailable(AstraError):
    """Raised when the isolated runtime cannot be reached.

    Callers must surface this (and refuse to execute) rather than falling
    back to the host shell. The message is written to be user-facing.
    """


class RuntimeBackend:
    """One way of providing the isolated Linux runtime.

    A backend owns exactly three things:

    * **discovery** - `probe()`, i.e. is this backend usable right now, and
      the reason why not when it is not;
    * **launch** - `build_argv()`/`run()`, i.e. how the host starts a process
      INSIDE the guest;
    * **layout** - `host_dirs()`/`guest_dirs()`/`prepare()`, i.e. where this
      runtime's own directories live, seen from both sides.

    Everything else (sessions, buffers, blobs, history, events, the PTY
    object itself) is shared and lives outside the backend.
    """

    #: Backend id reported to the UI/Gateway/Provider ("proot", "wsl2").
    name = "base"
    #: Host platform this backend runs on ("android", "windows", ...).
    platform = ""
    #: The guest shell a session starts.
    shell = "bash"
    #: Launcher diagnostics lines that must not be shown to the model or the
    #: user on an otherwise successful command (see `run_argv`).
    noise_prefixes: tuple = ()

    def __init__(self, config=None):
        self.config = config
        self._probe: dict | None = None

    # -- config --------------------------------------------------------------
    def _cfg(self, key: str, default=None):
        if self.config is None:
            return default
        try:
            value = self.config.get(key)
        except Exception:
            return default
        return default if value in (None, "") else value

    def _cfg_flag(self, key: str, default: bool = True) -> bool:
        value = self._cfg(key, None)
        if value is None:
            return default
        return str(value).strip().lower() not in ("0", "false", "no", "off")

    # -- discovery -----------------------------------------------------------
    def probe(self, *, refresh: bool = False) -> dict:
        """Capability report. Cached per backend instance."""
        raise NotImplementedError

    def available(self) -> bool:
        return bool(self.probe()["available"])

    def require(self) -> dict:
        """Return the probe, or raise - never a host fallback."""
        info = self.probe()
        if not info["available"]:
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: "
                + (info.get("reason") or "unknown"))
        return info

    # -- launch --------------------------------------------------------------
    def shell_argv(self, *, login: bool = False) -> list:
        """Interactive shell argv (guest side). NOT a login shell by default:
        a login shell would source the image's profile scripts, which is
        exactly where a host PATH can sneak back onto the guest PATH."""
        return [GUEST_SHELL, "-l"] if login else [GUEST_SHELL]

    def exec_argv(self, command: str) -> list:
        """Non-interactive one-shot command (same non-login rationale)."""
        return [GUEST_SHELL, "-c", str(command)]

    def probe_argv(self, command: str) -> list:
        return [GUEST_SHELL, "-lc", str(command)]

    def build_argv(self, *, binds, cwd: str, rootfs: str | None = None,
                   argv: list | None = None, env: dict | None = None,
                   hostname: str = "astra-runtime") -> list:
        """Full host command line that runs `argv` inside the runtime.

        `binds` are (host_path, guest_path) pairs. Only the runtime's OWN
        directories may appear in them; nothing else is exposed to the guest.
        """
        raise NotImplementedError

    def run(self, *, binds, cwd: str = GUEST_WORKSPACE, command: str,
            rootfs: str | None = None, timeout: float = PROBE_TIMEOUT,
            env: dict | None = None) -> dict:
        """Run one command in the guest and return a structured result.

        Used by capability detection, package verification and any other
        non-interactive work; interactive work goes through the PTY.
        """
        argv = self.build_argv(binds=binds, cwd=cwd, rootfs=rootfs,
                               argv=self.exec_argv(command), env=env)
        return self.run_argv(argv, command=command, timeout=timeout)

    def pty_argv(self, *, binds, cwd: str, argv: list | None = None,
                 env: dict | None = None, rows: int = 24,
                 cols: int = 80) -> list:
        """Host command line for an INTERACTIVE (PTY) session.

        Backends that can give the host a real PTY directly (proot on
        POSIX) only need `build_argv`. Backends where the PTY lives in
        another world (Windows/WSL2) override this to insert their bridge.
        """
        return self.build_argv(binds=binds, cwd=cwd, argv=argv, env=env)

    def run_argv(self, argv, *, command: str, timeout: float) -> dict:
        """Spawn `argv`, capture it and shape the result - shared by every
        backend, because only argv construction differs between them."""
        code, raw_out, raw_err, timed_out = self.spawn(argv, timeout=timeout)
        if timed_out:
            return {"ok": False, "status": "timeout", "exit_code": None,
                    "stdout": "", "stderr":
                    f"command timed out after {timeout}s", "command": command}
        out = (raw_out or b"").decode("utf-8", "replace")
        err = (raw_err or b"").decode("utf-8", "replace")
        if code is None:
            return {"ok": False, "status": "failed", "exit_code": None,
                    "stdout": "", "stderr": err or "the runtime did not "
                    "answer", "command": command}
        # Launcher diagnostics (e.g. "proot warning: ...") are noise on an
        # otherwise successful command; drop them so a clean run reads clean.
        if self.noise_prefixes:
            err = "\n".join(
                line for line in err.splitlines()
                if not line.startswith(self.noise_prefixes)).strip()
        return {"ok": code == 0,
                "status": "completed" if code == 0 else "failed",
                "exit_code": code, "stdout": out, "stderr": err,
                "command": command}

    def spawn(self, argv, *, timeout: float) -> tuple:
        """Run `argv` to completion: (exit_code, stdout, stderr, timed_out).

        The ONE genuinely host-specific step in one-shot execution. proot is
        an ordinary process spawn, so it uses this default; the WSL2 backend
        overrides it to go through its `wsl.exe` adapter, which keeps the
        whole backend drivable through a single seam in tests.

        `exit_code` is None when the command never produced one (timeout, or
        the launcher itself refused to start).
        """
        import subprocess

        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=float(timeout))
        except subprocess.TimeoutExpired:
            return None, b"", b"", True
        except OSError as exc:
            return None, b"", str(exc).encode("utf-8", "replace"), False
        return proc.returncode, proc.stdout, proc.stderr, False

    # -- layout --------------------------------------------------------------
    def host_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """Host-side paths backing /workspace, /root and /tmp.

        These are what `astra.runtime.files` reads and writes, so they must
        be reachable from the Astra process itself.
        """
        base = os.path.abspath(base_dir)
        return (os.path.join(base, "workspace"), os.path.join(base, "root"),
                os.path.join(base, "tmp"))

    def guest_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """The guest-side TARGETS the runtime's directories are bound onto.

        Every backend binds this runtime's OWN host-side directories onto
        exactly these three paths, so the contract the Agent sees
        (`/workspace`, `/root`, `/tmp`) is identical on every platform -
        and `AgentRuntime.binds` can always use these as the guest side.
        """
        return (GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP)

    def guest_source_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """Where those directories really live INSIDE the guest.

        The same three paths as `guest_dirs` when the guest-side storage IS
        the bind target (proot: the runtime's directories are host paths that
        proot binds onto /workspace, /root and /tmp). The WSL2 backend
        overrides this: its directories live under `/var/lib/astra/runtime/
        <runtime-id>` inside Ubuntu and are bind-mounted onto those targets.
        """
        return self.guest_dirs(runtime_id, base_dir)

    def prepare(self, runtime_id: str, base_dir: str) -> None:
        """Create/refresh whatever the runtime needs before first use.

        Best-effort and idempotent: a failure must leave the backend
        reporting itself unavailable (via `probe`), never silently degrade.
        """
        return None

    def pty_class(self):
        """The PTY implementation that drives this backend's sessions."""
        from astra.runtime.pty import PtyProcess
        return PtyProcess

    # -- helpers -------------------------------------------------------------
    def _bind_pairs(self, binds) -> list:
        """Normalise the (host_path, guest_path) pairs a caller supplied."""
        pairs = []
        for item in binds or []:
            try:
                host_path, guest_path = item
            except (TypeError, ValueError):
                continue
            pairs.append((str(host_path), str(guest_path)))
        return pairs
