"""Shared harness for Astra tests. Import as `from helpers import ...`
(discover -s tests puts this directory on sys.path).
The plugin system has been removed — `plugins/` is an empty placeholder
(see plugins/README.md), so the legacy `plugins` list is always empty."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.store import Store              # noqa: E402
from astra.agent import Agent              # noqa: E402
from astra.terminal import detect_shell    # noqa: E402


# ── environment gates ────────────────────────────────────────────────────────
#
# Two host runtimes the suite can genuinely not provide on every machine, so
# the tests that need them skip instead of failing:
#
# * POSIX host shell - `LocalRuntimeStub` (the stand-in for the Agent Runtime,
#   which is a Linux sandbox on every platform) shells out to `/bin/sh`. On
#   Windows the real runtime is WSL2 (see docs/AGENT_RUNTIME.md) and the host
#   has no `/bin/sh`, so the stub cannot run there.
# * POSIX terminal shell - the persistent host TerminalSession auto-detects
#   bash/sh on POSIX but cmd/PowerShell on Windows (see
#   astra/terminal/session.py::detect_shell). A test that asserts POSIX shell
#   semantics (`export FOO=bar`, `1>&2`, `printf`, `seq`, ...) is only
#   meaningful against a real POSIX shell.
POSIX_HOST_SHELL = os.path.exists("/bin/sh")
_SHELL = detect_shell()
POSIX_TERMINAL_SHELL = _SHELL.get("kind") == "posix"

requires_posix_host = unittest.skipUnless(
    POSIX_HOST_SHELL,
    "requires a POSIX host shell (/bin/sh) for the runtime test double")
requires_posix_terminal = unittest.skipUnless(
    POSIX_TERMINAL_SHELL,
    "requires a POSIX terminal shell (this host auto-detects %r)"
    % _SHELL.get("name"))


def runtime_python_has_pytest() -> bool:
    """Can the shell's own `python3` import pytest?

    The end-to-end "fix the failing tests" flow runs a REAL pytest inside the
    runtime; if that python has no pytest there is nothing to fix, so the
    tests built on it must skip rather than fail."""
    import subprocess
    try:
        return subprocess.run(["python3", "-m", "pytest", "--version"],
                              capture_output=True, timeout=120).returncode == 0
    except Exception:
        return False


def make_agent():
    """Returns (store, plugins, agent) wired exactly like the real app."""
    store = Store(":memory:")
    plugins = []
    agent = Agent()
    return store, plugins, agent


def make_stack(**kw):
    """Full stack (tools, memory, workflows, scheduler, router, …) on a fresh
    in-memory store — the same wiring run.py uses."""
    from astra.bootstrap import build
    return build(store=Store(":memory:"), **kw)


class LiveServer:
    """The real web server, in this process, on an ephemeral port.

    uvicorn runs in a background thread over a pre-bound socket, so two tests
    can never race for the same port and no sleep-and-hope is needed to know
    it is up.

    `site` is the AstraSite the running app serves from, so a test can flip the
    security knobs exactly where it used to poke the old server object:

        srv = LiveServer(stack=stack)
        srv.site.operator_token = "sekrit"
        srv.site.env = "production"
        srv.site.max_body_bytes = 10 * 1024 * 1024
    """

    def __init__(self, stack=None, store=None, agent=None):
        import socket
        import threading

        import uvicorn

        from astra.web import AstraSite
        from astra.web_fastapi import make_app

        if stack is not None:
            store = store or stack["store"]
            agent = agent or stack["agent"]
        self.stack = stack
        self.site = AstraSite(("127.0.0.1", 0), store, agent, stack=stack)
        # site= -> the caller owns the stack, so nothing is closed for us
        self.app = make_app(site=self.site)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]},
            daemon=True)
        self._thread.start()
        self._wait_ready()

    def _wait_ready(self, timeout=20.0):
        import time
        import urllib.error
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=1).read()
                return self
            except urllib.error.HTTPError as e:
                e.close()  # release the response body
                return self  # any HTTP status at all means it is serving
            except Exception:
                time.sleep(0.05)
        raise AssertionError("the ASGI server did not become ready")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


class ScriptedBrain:
    """A deterministic stand-in for a model inside the agent tool loop.

    Returns the scripted replies in order, recording every message list it
    was shown. It speaks the same JSON tool protocol a real model would, so
    the surrounding loop, ToolRegistry and Terminal are all the real thing —
    only the model call itself is scripted (there is no live model offline).
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, max_tokens=1500, trace=""):
        self.calls.append(list(messages))
        if not self.replies:
            return "done"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class LocalRuntimeStub:
    """A stand-in for one `AgentRuntime` whose isolation backend is the
    local shell instead of proot.

    Unit tests that drive the AGENT TOOL LOOP use this so they stay fast and
    hermetic: the loop, the `ToolRegistry`, every runtime tool schema and the
    runtime TOOL functions under test are all the real production ones — only
    the process backend behind them is local. It implements the same surface
    the runtime tools resolve against (`exec_command`, `status`, `get`,
    lifecycle no-ops) and emits the same `terminal.*` lifecycle events, so
    the Activity Log / op-correlation paths are exercised for real.

    Genuine proot isolation is covered by `tests/test_runtime.py`; this stub
    must never be used to claim isolation.
    """

    def __init__(self, workspace=None, events=None):
        import os
        import tempfile

        self.runtime_id = "test"
        self.title = "test"
        self.events = events
        self.workspace = workspace or tempfile.mkdtemp(prefix="astra-stub-")
        self.home_dir = os.path.join(self.workspace, ".home")
        self.tmp_dir = os.path.join(self.workspace, ".tmp")
        self.uploads_dir = os.path.join(self.workspace, ".uploads")
        for d in (self.home_dir, self.tmp_dir, self.uploads_dir):
            os.makedirs(d, exist_ok=True)
        self.paths = _StubPaths()
        self._cwd: dict[str, str] = {}
        self._started: set[str] = set()
        self._history: dict[str, list] = {}
        self.commands: list[tuple[str, str]] = []

    # -- lifecycle (no-ops the runtime tools call) --------------------------
    def default(self):
        return self

    def get(self, runtime_id=None, **_kw):
        return self

    def create(self, **_kw):
        return self.status()

    def start(self):
        return self.status()

    def stop(self):
        return self.status()

    def restart(self):
        return self.status()

    def reset(self):
        return self.status()

    def status(self, **_kw):
        return {"available": True, "state": "running",
                "runtime_id": self.runtime_id, "backend": "local-stub",
                "container": "stub", "workspace": self.workspace,
                "terminals": [], "tools": {}}

    def capabilities(self, refresh=False):
        return self.status()

    def open_terminal(self, session_id=None, **_kw):
        return None

    def close_terminal(self, session_id):
        key = str(session_id or self.runtime_id)
        self._cwd.pop(key, None)
        self._started.discard(key)
        return True

    def terminals(self):
        return []

    def close_all(self):
        self._cwd.clear()
        self._started.clear()
        return 0

    # -- the execution surface the runtime tools use ------------------------
    def exec_command(self, command, *, session_id="", timeout=None,
                     rows=24, cols=80):
        import os
        import shlex
        import subprocess
        import time

        text = str(command or "").strip()
        if not text:
            raise ValueError("command required")
        from astra.core.events import new_op_id

        key = str(session_id or self.runtime_id)
        cwd = self._cwd.get(key, self.workspace)
        op = new_op_id()
        exec_id = "exec-" + op
        # per-command lifecycle, exactly like the real runtime (one
        # persistent shell, so the per-command handle is the execution id)
        self._started.add(key)
        self._emit("terminal.started", session_id=key, shell="sh", op=op,
                   process_id=exec_id, shell_pid=0, command=text[:400],
                   cwd=cwd)
        rc_mark = "__ASTRA_STUB_RC__"
        pwd_mark = "__ASTRA_STUB_PWD__"
        # Capture the command's OWN exit status before the metadata printf
        # (which would otherwise become the script's status), so a failing
        # command is reported as failed exactly like the real runtime does.
        script = ("cd {cwd} || exit 1\n{cmd}\n__astra_rc=$?\n"
                  "printf '\\n{r}%s\\n{p}%s\\n' \"$__astra_rc\" \"$PWD\"\n"
                  ).format(cwd=shlex.quote(cwd), cmd=text, r=rc_mark, p=pwd_mark)
        started = time.time()
        proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True,
                              text=True)
        out = proc.stdout
        exit_code = proc.returncode
        idx = out.rfind(rc_mark)
        if idx >= 0:
            meta = out[idx + len(rc_mark):].splitlines()
            out = out[:idx]
            try:
                exit_code = int(meta[0].strip())
            except (IndexError, ValueError):
                pass
            for line in meta[1:]:
                if line.startswith(pwd_mark):
                    tail = line[len(pwd_mark):].strip()
                    if tail and os.path.isdir(tail):
                        self._cwd[key] = tail
                    break
        self.commands.append((key, text))
        self._history.setdefault(key, []).append({"command": text})
        status = "completed" if exit_code == 0 else "failed"
        blob = "stub-blob-" + exec_id if out else ""
        self._emit("terminal.output", session_id=key, op=op,
                   process_id=exec_id, stream="stdout", chars=len(out),
                   snippet=out[-500:], status=status)
        self._emit("terminal.completed" if exit_code == 0 else "terminal.failed",
                   session_id=key, op=op, process_id=exec_id,
                   exit_code=exit_code, status=status, terminal=True,
                   stdout_blob_id=blob, stderr_blob_id="")
        return {"ok": exit_code == 0, "status": status, "session_id": key,
                "runtime": self.runtime_id, "command": text,
                "cwd": self._cwd.get(key, cwd), "exit_code": exit_code,
                "stdout": out, "stderr": proc.stderr,
                "duration_ms": int((time.time() - started) * 1000),
                "truncated": False, "blob_id": ""}

    def context_text(self, session_id=None, *, max_commands=8, max_chars=None):
        key = str(session_id or self.runtime_id)
        hist = self.history(key, limit=max_commands)
        if not hist:
            return ""
        lines = [f"Agent Runtime session {key} (runtime={self.runtime_id}, "
                 f"shell=sh, cwd={self._cwd.get(key, self.workspace)})"]
        for h in hist:
            lines.append(f"  $ {h['command']}")
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            text = text[-max_chars:]
        return text

    # -- helpers the tests read back ----------------------------------------
    def session_ids(self):
        return list(self._cwd.keys())

    def cwd(self, session_id=""):
        key = str(session_id or self.runtime_id)
        return self._cwd.get(key, self.workspace)

    def history(self, session_id="", limit=20):
        return list(self._history.get(str(session_id or self.runtime_id), []))

    def session(self, session_id=""):
        key = str(session_id or self.runtime_id)
        stub = self

        class _Session:
            session_id = key
            cwd = stub._cwd.get(key, stub.workspace)
            shell = {"name": "sh"}

            @staticmethod
            def history(limit=20):
                return stub.history(key, limit)
        return _Session()

    def _emit(self, kind, **data):
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="runtime", **data)
        except Exception:
            pass


class _StubPaths:
    max_file_mb = 16
    max_members = 4096
    max_extract_mb = 128
    max_file_bytes = 16 * 1024 * 1024
