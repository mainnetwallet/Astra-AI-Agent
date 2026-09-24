"""Windows backend: WSL2 + Ubuntu (the Astra Agent Runtime on a PC).

The Astra Agent Terminal is the terminal UI on every platform. The shell
behind it on Windows is NOT `cmd.exe` and NOT PowerShell:

    Astra Terminal  ->  Astra Runtime  ->  WSL2  ->  Ubuntu  ->  bash

`wsl.exe` is used purely as the transport/bootstrap mechanism; every command
the Agent runs is executed by Ubuntu's own bash, inside Ubuntu's own
filesystem, and the Agent only ever sees `astra:/workspace$`. Neither
`wsl.exe`, `cmd.exe`, `powershell.exe` nor a `C:\\` path is ever exposed to
the Agent as a capability.

Isolation
---------
Each session is started inside a PRIVATE mount namespace (`unshare -m`,
`--propagation private`) in which:

* every host-provided mount (`9p`/`drvfs`/`virtiofs`: `/mnt/c`, `/mnt/wsl`,
  `/mnt/wslg`, `/usr/lib/wsl/drivers`, ...) is unmounted, so the Windows
  filesystem, the Windows user profile and anything on `C:\\` are simply not
  reachable - and the unmount is invisible outside the namespace, so the
  user's own `wsl` shell is untouched;
* `/workspace`, `/root` and `/tmp` are bind-mounted from this runtime's own
  directories, so two Astra runtimes never share state;
* the guest PATH is the Linux-only `GUEST_PATH` (never WSL's Windows PATH
  additions), so `powershell.exe`/`cmd.exe` are not on PATH;
* the environment is rebuilt from scratch (`env -i`, shared with the proot
  backend) so no Windows environment variable reaches the guest.

Host fallback is untouched: it stays a separate, explicitly approved
capability. A missing WSL2/Ubuntu does NOT trigger it - the runtime reports
itself unavailable and execution fails closed.

Layout
------
    /var/lib/astra/runtime/<runtime-id>/workspace  ->  /workspace
    /var/lib/astra/runtime/<runtime-id>/root       ->  /root
    /var/lib/astra/runtime/<runtime-id>/tmp        ->  /tmp

`RUNTIME_WSL_ROOT` relocates that tree. The Astra process reaches the same
files through `\\\\wsl.localhost\\<distro>\\...` for the file tools, and the
runtime's directories are created `0777` with the session `umask 000` so the
host-side file manager can write what the (root) guest created.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys

from astra.runtime.backends.base import (GUEST_HOME, GUEST_PATH, GUEST_SHELL,
                                         GUEST_TMP, GUEST_WORKSPACE,
                                         PROBE_TIMEOUT, AstraRuntimeUnavailable,
                                         RuntimeBackend, detect_platform,
                                         guest_env)

DEFAULT_DISTRO = "Ubuntu"
DEFAULT_ROOT = "/var/lib/astra/runtime"
DEFAULT_UMASK = "000"

# Safety valve for a wedged wsl.exe: a probe may never hang Astra's startup,
# and a one-shot command gets its own (larger) bound from the caller.
PROBE_GRACE = 20.0

# The machine-readable line the in-guest bootstrap prints when it has applied
# every isolation step, so the host can verify (rather than assume) them.
CHECK_PREFIX = "ASTRA_WSL_CHECK"


class WslResult:
    """Outcome of one `wsl.exe` invocation (the adapter's return type)."""

    __slots__ = ("rc", "out", "err", "timed_out")

    def __init__(self, rc, out=b"", err=b"", timed_out=False):
        self.rc = rc
        self.out = out
        self.err = err
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def text(self) -> str:
        """Decode guest output. Guest programs speak UTF-8; `wsl.exe`'s own
        management output (--list/--status) is UTF-16LE."""
        data = self.out or b""
        if not data:
            return ""
        if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:64]:
            try:
                return (data.decode("utf-16-le", "replace")
                        .replace("\ufeff", "").replace("\x00", ""))
            except Exception:
                pass
        return data.decode("utf-8", "replace")

    def error_text(self) -> str:
        return (self.err or b"").decode("utf-8", "replace").strip()


class WslRunner:
    """The narrow adapter seam over `wsl.exe`.

    Everything this backend knows about Windows is funnelled through
    `run()`, so tests can drive the whole backend (detection, layout, argv,
    probe, error paths) without WSL installed - see
    `tests/test_runtime_backends.py`.
    """

    def __init__(self, exe: str):
        self.exe = exe

    def run(self, args, *, stdin=None, timeout: float = PROBE_TIMEOUT,
            cwd: str | None = None) -> WslResult:
        argv = [self.exe] + [str(a) for a in args]
        try:
            proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=float(timeout),
                                  cwd=cwd)
        except subprocess.TimeoutExpired:
            return WslResult(None, b"", b"wsl.exe timed out", timed_out=True)
        except OSError as exc:
            return WslResult(None, b"", str(exc).encode("utf-8", "replace"))
        return WslResult(proc.returncode, proc.stdout, proc.stderr)


# -- in-guest scripts ------------------------------------------------------
# Small, POSIX-sh scripts (plus the bash session bootstrap) sent to Ubuntu as
# argv elements - never as files on the Windows filesystem, and never via
# /mnt/c, because /mnt/c is unmounted inside the session namespace.

# One round trip: what the distribution has, who we are, which paths to use.
TOOL_PROBE = r"""printf 'ASTRA_TOOLS=1\n'
printf 'os=%s\n' "$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"')"
printf 'uid=%s\n' "$(id -u)"
for t in unshare mount umount awk bash python3 timeout; do
  printf '%s=%s\n' "$t" "$(command -v "$t" 2>/dev/null || true)"
done
"""


# The session bootstrap: host isolation + this runtime's own /workspace,
# /root and /tmp, then the requested command. Positional arguments:
#   $1 workspace  $2 home  $3 tmp  $4 cwd  $5 isolate(1|0)  $6 umask
BOOTSTRAP = r"""ws="$1"; home="$2"; tmp="$3"; cwd="$4"; isolate="$5"; umask="$6"
shift 6

mkdir -p "$ws" "$home" "$tmp" 2>/dev/null || true

# 1. Host isolation. Every host-provided mount (drvfs/9p/virtiofs) is
#    unmounted: C:\, the Windows user profile, /mnt/c and the WSL system
#    mounts all stop being reachable. The unmount happens inside a PRIVATE
#    mount namespace, so the user's own wsl.exe shell keeps its mounts.
if [ "$isolate" = "1" ]; then
  for target in $(awk '$3 ~ /^(9p|drvfs|virtiofs|v9fs)$/ {print $2}' /proc/mounts 2>/dev/null | sort -r); do
    [ "$target" = "/" ] && continue
    umount -l "$target" >/dev/null 2>&1 || true
  done
fi

# 2. This runtime's OWN directories become /workspace, /root and /tmp.
for pair in "$ws:/workspace" "$home:/root" "$tmp:/tmp"; do
  src=${pair%%:*}; dst=${pair##*:}
  [ -d "$dst" ] || mkdir -p "$dst" 2>/dev/null || true
  mount --bind "$src" "$dst" >/dev/null 2>&1 || true
done

# 3. Files the root guest creates stay writable from the Astra process on
#    Windows, which reaches them through the WSL file server.
umask "$umask"

cd "$cwd" 2>/dev/null || cd /workspace 2>/dev/null || true
exec "$@"
"""


# Verification run inside a real session: it proves the isolation and the
# per-runtime binds instead of assuming them.
SESSION_CHECK = r"""printf 'ASTRA_WSL_CHECK=1\n'
printf 'whoami=%s\n' "$(id -un 2>/dev/null)"
printf 'uid=%s\n' "$(id -u 2>/dev/null)"
printf 'cwd=%s\n' "$PWD"
printf 'mnt_host_mounts=%s\n' "$(awk '$3 ~ /^(9p|drvfs|virtiofs|v9fs)$/' /proc/mounts 2>/dev/null | wc -l)"
printf 'mnt_c_entries=%s\n' "$(ls -A /mnt/c 2>/dev/null | wc -l)"
printf 'workspace_bind=%s\n' "$(touch /workspace/.astra_probe_$$ 2>/dev/null && test -e @WS@/.astra_probe_$$ && echo yes || echo no)"
printf 'home_bind=%s\n' "$(touch /root/.astra_probe_$$ 2>/dev/null && test -e @HOME@/.astra_probe_$$ && echo yes || echo no)"
rm -f /workspace/.astra_probe_$$ /root/.astra_probe_$$ 2>/dev/null
printf 'path=%s\n' "$PATH"
printf 'python_ok=%s\n' "$(python3 -c 'print(1)' 2>&1 | head -1)"
printf 'windows_bin=%s\n' "$(command -v powershell.exe >/dev/null 2>&1 && echo on_path || echo absent)"
printf 'cmd_bin=%s\n' "$(command -v cmd.exe >/dev/null 2>&1 && echo on_path || echo absent)"
printf 'interop=%s\n' "$(ls /proc/sys/fs/binfmt_misc/WSLInterop >/dev/null 2>&1 && echo registered || echo absent)"
printf 'c_drive=%s\n' "$(awk '$2 == "/mnt/c" {found=1} END {print (found ? "visible" : "hidden")}' /proc/mounts 2>/dev/null)"
"""


PREPARE = r"""set -u
for d in @WS@ @HOME@ @TMP@ @BINDIR@; do
  mkdir -p "$d" 2>/dev/null || true
  chmod 0777 "$d" 2>/dev/null || true
done
cat > @BRIDGE@ <<'@DELIM@'
@SOURCE@
@DELIM@
chmod 0755 @BRIDGE@ 2>/dev/null || true
printf 'ASTRA_WSL_PREPARED=1\n'
"""


def _parse_kv(text: str) -> dict:
    """Parse the `KEY=value` lines the in-guest probe scripts print."""
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip("\r")
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


class WslRuntimeBackend(RuntimeBackend):
    """Windows/WSL2 Ubuntu backend (see the module docstring)."""

    name = "wsl2"
    platform = "windows"
    shell = "bash"

    def __init__(self, config=None, *, runner=None):
        super().__init__(config)
        # Injectable adapter (see WslRunner) - the ONLY seam tests need.
        self._runner = runner
        self._unc: str = ""

    # -- configuration ------------------------------------------------------
    @property
    def distro(self) -> str:
        name = str(self._cfg("RUNTIME_WSL_DISTRO", DEFAULT_DISTRO)
                   or DEFAULT_DISTRO).strip()
        return name or DEFAULT_DISTRO

    @property
    def root(self) -> str:
        """Guest directory that holds every runtime's workspace/home/tmp."""
        value = str(self._cfg("RUNTIME_WSL_ROOT", DEFAULT_ROOT)
                    or DEFAULT_ROOT).strip().rstrip("/")
        return value or DEFAULT_ROOT

    def isolate(self) -> bool:
        return self._cfg_flag("RUNTIME_WSL_ISOLATE", True)

    def umask(self) -> str:
        return str(self._cfg("RUNTIME_WSL_UMASK", DEFAULT_UMASK)
                   or DEFAULT_UMASK)

    # -- discovery ----------------------------------------------------------
    def wsl_exe(self) -> str:
        explicit = self._cfg("RUNTIME_WSL_EXE")
        if explicit:
            return str(explicit)
        found = shutil.which("wsl.exe") or shutil.which("wsl")
        if found:
            return found
        candidate = os.path.join(os.environ.get("SystemRoot")
                                 or r"C:\Windows", "System32", "wsl.exe")
        return candidate if os.path.exists(candidate) else ""

    def runner(self) -> WslRunner:
        if self._runner is None:
            self._runner = WslRunner(self.wsl_exe() or "wsl.exe")
        return self._runner

    def _wsl(self, args, *, stdin=None, timeout: float = PROBE_TIMEOUT):
        return self.runner().run(args, stdin=stdin, timeout=timeout)

    def spawn(self, argv, *, timeout: float) -> tuple:
        """One-shot execution through the `wsl.exe` adapter.

        The RUNNER is the single place that talks to Windows, so routing
        `run()` through it (instead of spawning `wsl.exe` directly) keeps
        the entire backend - discovery, preparation and command execution -
        drivable through one injectable seam, and keeps a guest command's
        exit code and output exactly as the adapter read them.
        """
        argv = [str(a) for a in (argv or [])]
        args = argv[1:] if argv else []
        res = self.runner().run(args, stdin=b"", timeout=float(timeout))
        if res.timed_out:
            return None, b"", b"", True
        if res.rc is None:
            return None, res.out or b"", res.err or b"", False
        return int(res.rc), res.out or b"", res.err or b"", False

    def _guest(self, script: str, *, timeout: float = PROBE_TIMEOUT,
               shell_path: str = "/bin/sh"):
        """Run `script` in the distribution as root, OUTSIDE any namespace.

        `-e` (--exec) is essential: without it `wsl.exe` would first hand the
        whole command line to the distribution's shell, which would expand
        `$variables` and break every probe that reads one.
        """
        return self._wsl(["-d", self.distro, "-u", "root", "-e",
                          shell_path, "-c", script], timeout=timeout)

    def list_distros(self):
        """(names, error). `wsl.exe -l -q` writes UTF-16LE."""
        res = self._wsl(["-l", "-q"])
        if res.timed_out:
            return [], "wsl.exe did not answer while listing distributions"
        if res.rc not in (0, None) and not (res.out or b"").strip():
            return [], ("wsl.exe could not list distributions: "
                        + (res.error_text() or "unknown error"))
        names = []
        for line in res.text().splitlines():
            name = line.replace("\x00", "").strip().lstrip("*").strip()
            if name:
                names.append(name)
        return names, ""

    def distro_version(self, distro: str) -> str:
        res = self._wsl(["-l", "-v"])
        for line in res.text().splitlines():
            # `-l -v` marks the default distribution with a leading "*".
            parts = line.replace("\x00", "").replace("*", " ").split()
            if len(parts) >= 3 and parts[0] == distro:
                return parts[2]
        return ""

    # -- probe --------------------------------------------------------------
    def probe(self, *, refresh: bool = False) -> dict:
        if self._probe is not None and not refresh:
            return dict(self._probe)
        platform = detect_platform()
        info = {
            "backend": self.name,
            "platform": platform,
            "container": self.distro,
            "distro": self.distro,
            "shell": self.shell,
            "workspace": GUEST_WORKSPACE,
            "home": GUEST_HOME,
            "rootfs": "",
            "rootfs_mode": "shared",
            "root": self.root,
            "wsl_exe": "",
            "distro_version": "",
            "guest_os": "",
            "unshare": "",
            "mount": "",
            "bash": "",
            "python3": "",
            "timeout_bin": "",
            "isolate": self.isolate(),
            "host_isolation": False,
            "details": {},
        }
        install_hint = ("Install WSL2 + Ubuntu once from an elevated Windows "
                        "terminal (`wsl --install -d Ubuntu`), then reopen "
                        "Astra. Astra never installs WSL for you and never "
                        "falls back to CMD or PowerShell.")
        if platform != "windows":
            return self._finish(info, ["the wsl2 runtime backend requires "
                                       "Windows (this host is %s)" % platform],
                                "")
        exe = self.wsl_exe()
        info["wsl_exe"] = exe
        if not exe or (os.path.isabs(exe) and not os.path.exists(exe)):
            return self._finish(
                info, ["wsl.exe was not found: WSL2 Ubuntu is not installed"],
                install_hint)
        names, err = self.list_distros()
        if err:
            return self._finish(info, [err], install_hint)
        if not names:
            return self._finish(
                info, ["no WSL2 distribution is installed: WSL2 Ubuntu is not "
                       "installed"], install_hint)
        if self.distro not in names:
            return self._finish(
                info, ["WSL2 %s distribution is not installed (installed: %s)"
                       % (self.distro, ", ".join(names))],
                "Set RUNTIME_WSL_DISTRO to one of the installed distributions, "
                "or install Ubuntu: `wsl --install -d Ubuntu`.")
        version = self.distro_version(self.distro)
        info["distro_version"] = version
        if version and version != "2":
            return self._finish(
                info, ["distribution '%s' is WSL%s; the Astra runtime requires "
                       "WSL2" % (self.distro, version)],
                "Convert it with `wsl --set-version %s 2`." % self.distro)

        tools = self._guest(TOOL_PROBE)
        if "ASTRA_TOOLS" not in tools.text():
            return self._finish(
                info, ["the WSL2 %s runtime could not be started: %s"
                       % (self.distro, (tools.error_text() or tools.text()
                                        or "no answer").strip()[-300:])],
                install_hint)
        meta = _parse_kv(tools.text())
        info["guest_os"] = meta.get("os", "")
        for key in ("unshare", "mount", "bash", "timeout"):
            info[key] = meta.get(key, "")
        info["timeout_bin"] = meta.get("timeout", "")
        info["python3"] = meta.get("python3", "")
        issues = []
        if meta.get("uid") != "0":
            issues.append("Astra could not run as root inside %s (uid=%s)"
                          % (self.distro, meta.get("uid")))
        for tool, why in (("bash", "the runtime shell"),
                          ("unshare", "mount-namespace isolation"),
                          ("mount", "mounting the runtime directories"),
                          ("python3", "the Astra runtime PTY bridge")):
            if not meta.get(tool):
                issues.append("%s is not installed inside %s (required for %s)"
                              % (tool, self.distro, why))
        if issues:
            packages = []
            if not meta.get("unshare") or not meta.get("mount"):
                packages.append("util-linux")
            if not meta.get("bash"):
                packages.append("bash")
            if not meta.get("python3"):
                packages.append("python3")
            detail = ("Install them inside %s: `sudo apt update && sudo apt "
                      "install %s`." % (self.distro, " ".join(packages))
                      if packages else "")
            return self._finish(info, issues, detail)
        return self._session_probe(info, install_hint)

    def _session_probe(self, info: dict, hint: str) -> dict:
        """Verify, inside a REAL session, that isolation and the per-runtime
        binds actually work - rather than trusting the bootstrap."""
        ws, home, tmp = self.guest_source_dirs("default",
                                              self._default_host_base())
        script = (SESSION_CHECK.replace("@WS@", ws).replace("@HOME@", home))
        # Wrapped exactly like a real session (`env -i` + the guest contract),
        # so what is verified here is what an Agent command actually gets.
        inner = self._env_wrap(guest_env(), ["/bin/bash", "-c", script])
        args = self._session_args(ws, home, tmp, ws, inner,
                                  unshare_path=info.get("unshare") or "")
        res = self.runner().run(args, timeout=PROBE_TIMEOUT)
        text = res.text()
        if "ASTRA_WSL_CHECK" not in text:
            return self._finish(
                info, ["the Astra runtime session could not be started inside "
                       "%s: %s" % (self.distro, (res.error_text() or text
                                                 or "no answer").strip()[-300:])],
                hint)
        detail = _parse_kv(text)
        info["details"] = detail
        issues = []
        if detail.get("uid") != "0":
            issues.append("the runtime session is not running as root "
                          "(uid=%s)" % detail.get("uid"))
        if detail.get("python_ok", "").strip() != "1":
            issues.append("python3 inside %s cannot run the Astra PTY bridge: "
                          "%s" % (self.distro, detail.get("python_ok", "")))
        if detail.get("workspace_bind") != "yes":
            issues.append("the runtime workspace could not be mounted at "
                          "/workspace")
        if detail.get("home_bind") != "yes":
            issues.append("the runtime home could not be mounted at /root")
        # §19/§22: the Windows host must not be reachable from the runtime -
        # not by path, not by a Windows binary on PATH, not by interop.
        if "/mnt/" in detail.get("path", ""):
            issues.append("the runtime PATH still contains Windows entries")
        if detail.get("windows_bin") == "on_path":
            issues.append("powershell.exe is reachable from the runtime")
        if detail.get("cmd_bin") == "on_path":
            issues.append("cmd.exe is reachable from the runtime")
        if detail.get("c_drive") == "visible":
            issues.append("the Windows C: drive is still visible at /mnt/c")
        # §19/§22: host isolation is mandatory, so it is VERIFIED, not
        # assumed - and a failure is reported instead of silently accepted.
        if self.isolate():
            hidden = (detail.get("mnt_host_mounts") == "0"
                      and detail.get("mnt_c_entries") == "0")
            if not hidden:
                issues.append("the runtime could not be isolated from the "
                              "Windows filesystem (host mounts are still "
                              "visible)")
            info["host_isolation"] = bool(hidden)
        else:
            info["host_isolation"] = False
        return self._finish(info, issues, hint)

    def _finish(self, info: dict, issues, hint: str) -> dict:
        info["issues"] = list(issues)
        info["hint"] = hint or ""
        info["available"] = not issues
        info["reason"] = "; ".join(issues)
        if info["available"]:
            info["rootfs"] = self.unc_root()
        self._probe = dict(info)
        return dict(self._probe)

    # -- layout -------------------------------------------------------------
    def _default_host_base(self) -> str:
        """The host-side runtime root RuntimeManager uses by default."""
        configured = self._cfg("RUNTIME_DIR")
        if configured:
            return os.path.abspath(str(configured))
        return os.path.abspath(os.path.join(os.path.expanduser("~"), ".astra",
                                            "runtime"))

    def _builtin_host_base(self) -> str:
        """The documented default root, ignoring any RUNTIME_DIR override.

        This is the ONE host root whose guest side is the clean, documented
        `/var/lib/astra/runtime/<runtime-id>`: it belongs to this Astra
        installation alone, so its runtimes can own those directories.
        """
        return os.path.abspath(os.path.join(os.path.expanduser("~"), ".astra",
                                            "runtime"))

    def guest_base(self, base_dir: str) -> str:
        """Guest directory that holds this runtime root's instances.

        A runtime directory under the DEFAULT host runtime root keeps the
        clean, documented guest path (`<RUNTIME_WSL_ROOT>/<runtime-id>`).
        Any other host-side root - a second Astra installation, an explicit
        RUNTIME_DIR, a test run - gets its own tag directory instead, so two
        of them can never share (or destroy) each other's workspace, and a
        test can never write into the real runtime's directories.
        """
        base = os.path.abspath(base_dir)
        home = os.path.normcase(self._builtin_host_base())
        key = os.path.normcase(base)
        if key == home or key.startswith(home + os.sep):
            return self.root
        tag = hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:12]
        return "%s/h-%s" % (self.root, tag)

    def guest_source_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        """The runtime's directories INSIDE Ubuntu (bind SOURCES).

        These are not the Agent's view - the session bootstrap mounts them
        onto /workspace, /root and /tmp (the shared `guest_dirs` targets),
        which is what the Agent sees on every platform.
        """
        rid = str(runtime_id or "default")
        base = "%s/%s" % (self.guest_base(base_dir), rid)
        override = self._cfg("RUNTIME_WSL_WORKSPACE")
        workspace = (str(override).rstrip("/") if override
                     else base + "/workspace")
        return (workspace, base + "/root", base + "/tmp")

    def unc_root(self) -> str:
        """The Windows-side view of the distribution's filesystem."""
        if not self._unc:
            canonical = "\\\\wsl.localhost\\" + self.distro + "\\"
            self._unc = canonical
            try:
                if not os.path.isdir(canonical):
                    legacy = "\\\\wsl$\\" + self.distro + "\\"
                    if os.path.isdir(legacy):
                        self._unc = legacy
            except OSError:
                pass
        return self._unc

    def unc(self, guest_path: str) -> str:
        """Map a guest path to the Windows path that reaches the same file."""
        return (self.unc_root()
                + str(guest_path).lstrip("/").replace("/", "\\"))

    def unc_to_guest(self, host_path: str) -> str:
        """Inverse of `unc` - "" when the path is not inside this distro."""
        text = str(host_path or "")
        low = text.lower()
        for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
            root = prefix + self.distro.lower() + "\\"
            if low.startswith(root):
                rel = text[len(root):].replace("\\", "/").strip("/")
                return "/" + rel if rel else "/"
        return ""

    def host_dirs(self, runtime_id: str, base_dir: str) -> tuple:
        ws, home, tmp = self.guest_source_dirs(runtime_id, base_dir)
        return (self.unc(ws), self.unc(home), self.unc(tmp))

    def bridge_path(self) -> str:
        return self.root + "/.bin/astra_wsl_bridge.py"

    def bridge_source(self) -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "wsl_bridge.py")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    # -- launch -------------------------------------------------------------
    def _guest_dirs_from_binds(self, pairs) -> tuple:
        """The guest-side /workspace, /root and /tmp for a session.

        The manager hands us (host_path, guest_path) pairs, and on this
        backend the host side is the Windows UNC view of the SAME directory
        the guest sees - so the pair already carries the whole mapping.
        """
        found = {}
        for host_path, guest_path in pairs:
            local = self.unc_to_guest(host_path)
            if local:
                found[guest_path] = local
        fallback = self.guest_source_dirs("default", self._default_host_base())
        return (found.get(GUEST_WORKSPACE, fallback[0]),
                found.get(GUEST_HOME, fallback[1]),
                found.get(GUEST_TMP, fallback[2]))

    def _session_args(self, ws: str, home: str, tmp: str, cwd: str, inner,
                      *, unshare_path: str = "", isolate=None,
                      umask: str | None = None) -> list:
        """`wsl.exe` arguments for one runtime session (one-shot or PTY).

        `inner` is what runs as the guest command, after the bootstrap has
        isolated the session and mounted the runtime's own directories.
        """
        isolate = self.isolate() if isolate is None else bool(isolate)
        # `-e` execs the command directly (no extra shell pass over the
        # arguments - see `_guest`).
        args = ["-d", self.distro, "-u", "root", "-e"]
        if isolate:
            args += [unshare_path or "/usr/bin/unshare", "-m",
                     "--propagation", "private", "--"]
        args += ["/bin/bash", "-c", BOOTSTRAP, "astra-session", ws, home, tmp,
                 str(cwd or ws), "1" if isolate else "0",
                 self.umask() if umask is None else umask]
        return args + list(inner)

    def build_argv(self, *, binds, cwd: str, rootfs: str | None = None,
                   argv: list | None = None, env: dict | None = None,
                   hostname: str = "astra-runtime") -> list:
        info = self.require()
        pairs = self._bind_pairs(binds)
        ws, home, tmp = self._guest_dirs_from_binds(pairs)
        inner = self._env_wrap(guest_env(env), list(argv or self.shell_argv()))
        args = self._session_args(ws, home, tmp, cwd or ws, inner,
                                  unshare_path=info.get("unshare") or "")
        return [self.wsl_exe() or "wsl.exe"] + args

    @staticmethod
    def _env_wrap(child_env: dict, argv: list) -> list:
        """`env -i` + explicit assignments: the Windows environment is never
        inherited, and the same guest environment builder serves the proot
        backend, so the two platforms cannot drift apart."""
        return (["/usr/bin/env", "-i"]
                + ["%s=%s" % (key, value) for key, value in child_env.items()]
                + list(argv))

    def pty_argv(self, *, binds, cwd: str, argv: list | None = None,
                 env: dict | None = None, rows: int = 24,
                 cols: int = 80) -> list:
        """The interactive session, with the in-guest PTY bridge as the
        guest command: wsl.exe -> unshare -> bootstrap -> bridge -> bash.

        The bridge configuration travels as one base64 argv element, so the
        guest needs no file from the Windows filesystem to start a session
        (and `/mnt/c` can stay unmounted).
        """
        info = self.require()
        pairs = self._bind_pairs(binds)
        ws, home, tmp = self._guest_dirs_from_binds(pairs)
        config = {
            "argv": [str(a) for a in (argv or self.shell_argv())],
            "cwd": str(cwd or ws),
            "env": guest_env(env),
            "rows": max(1, int(rows or 24)),
            "cols": max(2, int(cols or 80)),
            "umask": self.umask(),
        }
        encoded = base64.b64encode(
            json.dumps(config).encode("utf-8")).decode("ascii")
        inner = [info.get("python3") or "python3", self.bridge_path(),
                 encoded]
        args = self._session_args(ws, home, tmp, cwd or ws, inner,
                                  unshare_path=info.get("unshare") or "")
        return [self.wsl_exe() or "wsl.exe"] + args

    def run(self, *, binds, cwd: str = GUEST_WORKSPACE, command: str,
            rootfs: str | None = None, timeout: float = PROBE_TIMEOUT,
            env: dict | None = None) -> dict:
        info = self.require()
        limit = float(timeout) if timeout else PROBE_TIMEOUT
        # A guest-side `timeout` is the primary bound (it kills the process
        # INSIDE Ubuntu, so nothing is left running); the host-side bound in
        # `run_argv` is only a backstop for a wedged wsl.exe.
        guard = info.get("timeout_bin") or ""
        guarded = str(command)
        if guard:
            guarded = ("%s --signal=TERM --kill-after=5 %ds /bin/bash -c %s"
                       % (guard, max(1, int(limit)),
                          shlex.quote(str(command))))
        result = super().run(binds=binds, cwd=cwd, rootfs=rootfs,
                             command=guarded, timeout=limit + PROBE_GRACE,
                             env=env)
        result["command"] = str(command)
        if guard and result.get("exit_code") in (124, 137):
            result["ok"] = False
            result["status"] = "timeout"
            message = ("the command exceeded %ss and was stopped inside the "
                       "runtime" % int(limit))
            result["stderr"] = ((result.get("stderr") or "") + "\n"
                                + message).strip()
        return result

    # -- preparation --------------------------------------------------------
    def prepare(self, runtime_id: str, base_dir: str) -> None:
        """Create this runtime's directories inside Ubuntu and (re)install
        the PTY bridge. Idempotent - safe to call on every create/reset."""
        info = self.probe()
        if not info.get("available"):
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: " + (info.get("reason")
                                                 or "unknown"))
        ws, home, tmp = self.guest_source_dirs(runtime_id, base_dir)
        source = self.bridge_source()
        delim = "__ASTRA_BRIDGE_%s__" % hashlib.sha1(
            source.encode("utf-8", "replace")).hexdigest()[:12]
        script = (PREPARE
                  .replace("@WS@", ws)
                  .replace("@HOME@", home)
                  .replace("@TMP@", tmp)
                  .replace("@BINDIR@", self.root + "/.bin")
                  .replace("@BRIDGE@", self.bridge_path())
                  .replace("@DELIM@", delim)
                  .replace("@SOURCE@", source.rstrip("\n")))
        res = self._guest(script, shell_path="/bin/bash")
        if "ASTRA_WSL_PREPARED" not in res.text():
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: could not prepare the WSL2 runtime "
                "directories: "
                + (res.error_text() or res.text() or "no answer").strip()[-300:])

    def pty_class(self):
        from astra.runtime.pty_wsl import WslPtyProcess
        return WslPtyProcess
