"""Persistent terminal session — one shell that remembers its cwd, its
environment and its processes across many commands.

This is the single, provider-agnostic terminal capability. Nothing here
knows about a Gateway, a Provider or a model: it is a plain, thread-safe
execution session that `astra.terminal.tools` exposes to the shared
`ToolRegistry`, so the AI Gateway and every Provider/model drive the
*identical* implementation.

Design
------
A session owns:

* ``session_id``   — stable identity, quoted back on every result
* ``shell``        — the auto-detected shell (bash/sh on POSIX, cmd or
                     PowerShell on Windows, Termux's bash on Termux)
* ``cwd``          — persisted between commands (``cd`` in one command is
                     still in effect for the next one)
* ``env``          — persisted (``export FOO=bar`` carries forward)
* ``history``      — bounded list of past commands + their key results
* ``processes``    — background processes started here, by process id

Each command runs as a real child process:

* POSIX: ``<shell> -c '<command>; ...probe cwd/rc/env...'`` in its own
  process group, so a timeout can kill the whole tree, not just the shell.
* Windows: ``cmd.exe /d /c`` or ``powershell -Command`` with the
  equivalent probes.

stdout and stderr are captured separately by reader threads, so a
structured result carries both streams plus exit code, status and
duration. Output is streamed through an optional callback as it arrives;
every stored buffer is deterministically capped so a chatty command can
never exhaust memory or blow up the Activity Log.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque

from astra.core.exceptions import ValidationError

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0
DEFAULT_MAX_OUTPUT_CHARS = 20000
DEFAULT_HISTORY_LIMIT = 50
MAX_STREAM_EVENTS = 8

# Status values a terminal result can carry.
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"
TIMEOUT = "timeout"


def detect_platform() -> str:
    """Human-readable platform tag, including a first-class Termux value."""
    if os.environ.get("TERMUX_VERSION") or "com.termux" in (
            os.environ.get("PREFIX") or ""):
        return "termux"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def detect_shell(platform: str | None = None) -> dict:
    """Pick the shell a session should use, honouring ``ASTRA_TERMINAL_SHELL``.

    Returns a descriptor: ``{"name", "path", "kind"}`` where ``kind`` is one
    of ``"posix"`` (sh-compatible), ``"cmd"`` or ``"powershell"``.
    """
    override = (os.environ.get("ASTRA_TERMINAL_SHELL") or "").strip()
    platform = platform or detect_platform()

    if override:
        low = override.lower()
        if "powershell" in low or low.endswith("pwsh") or low.endswith("pwsh.exe"):
            return {"name": os.path.basename(override), "path": override,
                    "kind": "powershell"}
        if low.endswith("cmd") or low.endswith("cmd.exe"):
            return {"name": os.path.basename(override), "path": override,
                    "kind": "cmd"}
        return {"name": os.path.basename(override), "path": override,
                "kind": "posix"}

    if platform == "windows":
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh:
            return {"name": "powershell", "path": pwsh, "kind": "powershell"}
        cmd = shutil.which("cmd") or os.environ.get("COMSPEC") or "cmd.exe"
        return {"name": "cmd", "path": cmd, "kind": "cmd"}

    if platform == "termux":
        prefix = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
        bash = os.path.join(prefix, "bin", "bash")
        if os.path.exists(bash):
            return {"name": "bash", "path": bash, "kind": "posix"}

    for candidate in ("bash", "sh"):
        path = shutil.which(candidate)
        if path:
            return {"name": candidate, "path": path, "kind": "posix"}
    return {"name": "sh", "path": "/bin/sh", "kind": "posix"}


def _parse_env_blob(blob: str) -> dict:
    out = {}
    for chunk in blob.split("\0"):
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        if not key or key.startswith("ASTRA_TERMINAL_"):
            continue
        out[key] = value
    return out


class _OutputBuffer:
    """A capped, thread-safe text buffer with an optional live callback.

    The cap bounds the in-memory hot copy only (protects RAM against a
    pathologically chatty command). When `on_raw` is given, EVERY appended
    chunk — capped or not — is also forwarded to it uncapped, so a caller
    can persist the complete stream (see TerminalSession's use of
    astra.core.blob_store) and nothing is silently lost even once the hot
    buffer stops growing.
    """

    def __init__(self, cap: int, on_chunk=None, on_raw=None):
        self._parts: list[str] = []
        self._size = 0
        self._cap = max(0, int(cap))
        self._on_chunk = on_chunk
        self._on_raw = on_raw
        self._lock = threading.Lock()
        self.truncated = False
        self.full_size = 0

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            if self._cap and self._size + len(text) > self._cap:
                remaining = max(0, self._cap - self._size)
                self._parts.append(text[:remaining])
                self._size += remaining
                self.truncated = True
            else:
                self._parts.append(text)
                self._size += len(text)
            self.full_size += len(text)
        if self._on_raw is not None:
            try:
                self._on_raw(text)
            except Exception:
                pass
        if self._on_chunk is not None:
            try:
                self._on_chunk(text)
            except Exception:
                pass

    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)


def _read_stream(pipe, buffer: _OutputBuffer) -> None:
    """Reader-thread body: read chunks until the pipe closes, then flush."""
    try:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            buffer.append(chunk.decode("utf-8", errors="replace")
                          if isinstance(chunk, bytes) else str(chunk))
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


class ProcessRecord:
    """One background process owned by a session."""

    def __init__(self, process_id: str, command: str, popen, cwd: str,
                 started_at: float, max_output_chars: int,
                 on_output=None, op: str = "", probes: dict | None = None,
                 blobs=None):
        self.process_id = process_id
        self.command = command
        self.popen = popen
        self.cwd = cwd
        self.started_at = started_at
        # This command's own probe files, removed once it has ended (a
        # session that starts many background jobs must not accumulate
        # them until the session itself closes).
        self.probes = probes or {}
        # Correlation id shared by this process's start/terminal events, so
        # the Activity Log resolves one row instead of leaving it running.
        self.op = op or f"term:{process_id}"
        self.exit_emitted = False
        self.status = RUNNING
        self.exit_code: int | None = None
        if blobs is None:
            from astra.core.blob_store import BlobStore
            blobs = BlobStore()
        # Full stdout/stderr for this background process, retrievable with
        # terminal_output_read regardless of the hot-buffer cap.
        self.stdout_blob_id = blobs.open(f"terminal_stdout_{process_id}")
        self.stderr_blob_id = blobs.open(f"terminal_stderr_{process_id}")
        self.stdout = _OutputBuffer(
            max_output_chars, on_output,
            on_raw=lambda t: blobs.append(self.stdout_blob_id, t))
        self.stderr = _OutputBuffer(
            max_output_chars, on_output,
            on_raw=lambda t: blobs.append(self.stderr_blob_id, t))
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def attach(self, out_thread: threading.Thread,
               err_thread: threading.Thread) -> None:
        self._threads = [out_thread, err_thread]

    def refresh(self) -> None:
        """Recompute status from the OS process state (idempotent)."""
        with self._lock:
            if self.status != RUNNING:
                return
            code = self.popen.poll()
            if code is None:
                return
            self.exit_code = code
            self.status = COMPLETED if code == 0 else FAILED

    def claim_exit(self) -> bool:
        """Return True exactly ONCE, when the process has ended and its
        terminal Activity-Log event has not been emitted yet. The poll +
        flag flip happen under the record lock, so two threads refreshing
        concurrently can never both emit a completion event."""
        with self._lock:
            if self.status == RUNNING:
                code = self.popen.poll()
                if code is None:
                    return False
                self.exit_code = code
                self.status = COMPLETED if code == 0 else FAILED
            if self.exit_emitted:
                return False
            self.exit_emitted = True
            return True

    def duration_ms(self) -> float:
        return round((time.monotonic() - self.started_at) * 1000.0, 2)

    def to_dict(self, include_output: bool = True) -> dict:
        self.refresh()
        out = {
            "process_id": self.process_id,
            "command": self.command,
            "cwd": self.cwd,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms(),
        }
        if include_output:
            out["stdout"] = self.stdout.text()
            out["stderr"] = self.stderr.text()
            out["truncated"] = bool(self.stdout.truncated or self.stderr.truncated)
            out["stdout_blob_id"] = self.stdout_blob_id
            out["stdout_total_chars"] = self.stdout.full_size
            out["stderr_blob_id"] = self.stderr_blob_id
            out["stderr_total_chars"] = self.stderr.full_size
        return out


class TerminalSession:
    """One persistent, thread-safe terminal session."""

    def __init__(self, session_id: str | None = None, *, cwd: str | None = None,
                 shell: dict | None = None, env: dict | None = None,
                 platform: str | None = None, events=None,
                 max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
                 history_limit: int = DEFAULT_HISTORY_LIMIT,
                 on_output=None, blobs=None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.platform = platform or detect_platform()
        self.shell = dict(shell or detect_shell(self.platform))
        self.cwd = os.path.abspath(os.path.expanduser(cwd or
                                                      os.path.expanduser("~")))
        base_env = dict(os.environ)
        if env:
            base_env.update(env)
        self.env = base_env
        self.events = events
        self.max_output_chars = int(max_output_chars)
        self.on_output = on_output
        self.created_at = time.time()
        self.last_used = self.created_at
        self._history: deque = deque(maxlen=max(1, int(history_limit)))
        self._processes: dict[str, ProcessRecord] = {}
        self._counter = 0
        self._closed = False
        # `_lock` guards session STATE (cwd/env/history/process table/closed)
        # and is only ever held for short mutations. `_exec_lock` serializes
        # foreground commands so a session runs one at a time, but it is NOT
        # held by close()/status()/stop() — an in-flight command must never
        # block shutdown or process control (that used to hang close() for
        # the full command duration).
        self._lock = threading.RLock()
        self._exec_lock = threading.Lock()
        self._active = None            # currently-running foreground Popen
        self._probe_dir = tempfile.mkdtemp(prefix="astra-term-")
        # Full stdout/stderr and full command history outlive the capped
        # hot buffer / the bounded deque below — see astra.core.blob_store.
        if blobs is None:
            from astra.core.blob_store import BlobStore
            blobs = BlobStore()
        self._blobs = blobs
        self._history_seq = 0
        # One append-only, newline-delimited log per session: every command
        # ever run here, regardless of the `history_limit` deque size. Used
        # by `terminal_history_read` to page back past what the hot deque
        # still holds.
        self._history_blob_id = self._blobs.open(
            f"terminal_history_{self.session_id}")

    def _probe_paths_for(self, tag: str) -> dict:
        """Probe files for ONE command.

        A single shared set per session was silently overwritten by a
        concurrently-running background command (its wrapper writes the same
        files), so a foreground command could read *another* command's cwd /
        exit code — or have its probes deleted by a background `start` while
        it ran. Unique files per command remove the race entirely.
        """
        safe = "".join(c for c in str(tag) if c.isalnum() or c in "-_")
        base = os.path.join(self._probe_dir, safe or "cmd")
        return {"rc": base + ".rc", "pwd": base + ".pwd",
                "env": base + ".env"}

    # -- identity / state ----------------------------------------------------
    def snapshot(self) -> dict:
        self._refresh_all()
        with self._lock:
            return {
                "session_id": self.session_id,
                "cwd": self.cwd,
                "shell": self.shell.get("name", ""),
                "shell_path": self.shell.get("path", ""),
                "platform": self.platform,
                "closed": self._closed,
                "created_at": self.created_at,
                "last_used": self.last_used,
                "history_count": len(self._history),
                "processes": [p.to_dict(include_output=False)
                              for p in self._processes.values()],
                "env_keys": len(self.env),
            }

    def history(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = list(self._history)
        if limit and limit > 0:
            rows = rows[-limit:]
        return rows

    def history_log_chunk(self, offset: int = 0, length: int = 6000) -> dict:
        """Raw newline-delimited-JSON chunk of the FULL, unbounded command
        history for this session (every command ever run here), for
        `terminal_history_read` to page through once a command has aged
        out of the bounded `history()` deque."""
        return self._blobs.read(self._history_blob_id, offset=offset,
                                length=length)

    def processes(self) -> list[dict]:
        self._refresh_all()
        with self._lock:
            return [p.to_dict(include_output=False)
                    for p in self._processes.values()]

    def set_cwd(self, cwd: str) -> str:
        target = os.path.expanduser(str(cwd or ""))
        if not target:
            raise ValidationError("cwd must not be empty")
        if not os.path.isabs(target):
            target = os.path.join(self.cwd, target)
        target = os.path.abspath(target)
        if not os.path.isdir(target):
            raise ValidationError(f"cwd does not exist: {target}")
        with self._lock:
            self.cwd = target
        return target

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active
            records = list(self._processes.values())
        # Terminate outside the lock: close() must never block on a slow
        # command or on process-control calls contending for the lock.
        if active is not None:
            self._terminate(active)
        for record in records:
            record.exit_emitted = True
            self._terminate(record.popen)
        shutil.rmtree(self._probe_dir, ignore_errors=True)

    def is_idle(self) -> bool:
        """True when no foreground command is running and no background
        process is still alive — safe for the manager to reap."""
        self._refresh_all()
        return self.can_reap()

    def can_reap(self) -> bool:
        """Like `is_idle` but with no events/refresh side effects, so the
        manager can call it while holding its own lock."""
        with self._lock:
            if self._closed:
                return True
            if self._active is not None:
                return False
            for proc in self._processes.values():
                if proc.status == RUNNING and proc.popen.poll() is None:
                    return False
            return True

    @property
    def closed(self) -> bool:
        return self._closed

    def idle_seconds(self) -> float:
        return time.time() - self.last_used

    # -- execution -----------------------------------------------------------
    def exec(self, command: str, *, timeout: float | None = DEFAULT_TIMEOUT,
             wait: bool = True, cwd: str | None = None,
             env: dict | None = None) -> dict:
        """Run `command`. `wait=False` starts it in the background."""
        command = str(command or "").strip()
        if not command:
            raise ValidationError("command required")
        if cwd:
            self.set_cwd(cwd)
        if timeout is not None:
            timeout = min(float(timeout), MAX_TIMEOUT)
            if timeout <= 0:
                timeout = None
        with self._lock:
            if self._closed:
                raise ValidationError(
                    f"terminal session {self.session_id} is closed")
            self.last_used = time.time()
        if wait:
            with self._exec_lock:
                with self._lock:
                    if self._closed:
                        raise ValidationError(
                            f"terminal session {self.session_id} is closed")
                return self._run_foreground(command, timeout, env)
        return self._run_background(command, env)

    def _new_process_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"{self.session_id}-{self._counter}"

    def _base_env(self, extra: dict | None, probes: dict) -> dict:
        env = dict(self.env)
        env["ASTRA_TERMINAL_RC"] = probes["rc"]
        env["ASTRA_TERMINAL_PWD"] = probes["pwd"]
        env["ASTRA_TERMINAL_ENV"] = probes["env"]
        env["ASTRA_TERMINAL_SESSION"] = self.session_id
        if extra:
            env.update({str(k): str(v) for k, v in extra.items()})
        return env

    @staticmethod
    def _discard_probes(probes: dict) -> None:
        for path in probes.values():
            try:
                os.remove(path)
            except OSError:
                pass

    def _wrap(self, command: str) -> str:
        kind = self.shell.get("kind", "posix")
        if kind == "cmd":
            return (f"{command}\r\n"
                    f"echo %errorlevel% > \"%ASTRA_TERMINAL_RC%\"\r\n"
                    f"cd > \"%ASTRA_TERMINAL_PWD%\"\r\n")
        if kind == "powershell":
            return (f"{command}\n"
                    f"if ($null -eq $LASTEXITCODE) {{ 0 }} else "
                    f"{{ $LASTEXITCODE }} | Out-File -Encoding ascii "
                    f"$env:ASTRA_TERMINAL_RC\n"
                    f"(Get-Location).Path | Out-File -Encoding ascii "
                    f"$env:ASTRA_TERMINAL_PWD\n")
        return (f"{command}\n"
                f"__astra_rc=$?\n"
                f"pwd > \"$ASTRA_TERMINAL_PWD\" 2>/dev/null\n"
                f"printf '%s' \"$__astra_rc\" > \"$ASTRA_TERMINAL_RC\" "
                f"2>/dev/null\n"
                f"command -v env >/dev/null 2>&1 && "
                f"env -0 > \"$ASTRA_TERMINAL_ENV\" 2>/dev/null\n"
                f"exit $__astra_rc\n")

    def _shell_argv(self, script: str) -> list[str]:
        kind = self.shell.get("kind", "posix")
        path = self.shell.get("path") or "sh"
        if kind == "cmd":
            return [path, "/d", "/c", script]
        if kind == "powershell":
            return [path, "-NoProfile", "-NonInteractive", "-Command", script]
        return [path, "-c", script]

    def _popen(self, argv, env, cwd):
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":       # pragma: no cover - Windows only
            kwargs["creationflags"] = getattr(subprocess,
                                              "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    def _run_foreground(self, command, timeout, env) -> dict:
        process_id = self._new_process_id()
        probes = self._probe_paths_for(process_id)
        op = f"term:{process_id}"
        started = time.monotonic()
        with self._lock:
            cwd = self.cwd
            base_env = self._base_env(env, probes)
        argv = self._shell_argv(self._wrap(command))
        popen = self._popen(argv, base_env, cwd)
        with self._lock:
            self._active = popen
        # Live streaming: forward chunks to the caller's callback AND publish
        # at most MAX_STREAM_EVENTS capped `terminal.output` events per
        # command, so a chatty process can never flood the Activity Log.
        stream_state = {"count": 0}

        def _on_chunk(text, stream_name):
            if self.on_output is not None:
                try:
                    self.on_output(text)
                except Exception:
                    pass
            if self.events is None or stream_state["count"] >= MAX_STREAM_EVENTS:
                return
            if not text.strip():
                return
            stream_state["count"] += 1
            self._emit("terminal.output", op=op, process_id=process_id,
                       stream=stream_name, chars=len(text),
                       snippet=(text if len(text) <= 500 else text[:500] + "…"),
                       status=RUNNING)

        # Full stdout/stderr are captured here regardless of the hot-buffer
        # cap, so a chatty command's output is never lost — only deferred
        # behind `terminal_output_read` once it exceeds max_output_chars.
        stdout_blob = self._blobs.open(f"terminal_stdout_{process_id}")
        stderr_blob = self._blobs.open(f"terminal_stderr_{process_id}")
        stdout = _OutputBuffer(self.max_output_chars,
                               lambda t: _on_chunk(t, "stdout"),
                               on_raw=lambda t: self._blobs.append(stdout_blob, t))
        stderr = _OutputBuffer(self.max_output_chars,
                               lambda t: _on_chunk(t, "stderr"),
                               on_raw=lambda t: self._blobs.append(stderr_blob, t))
        out_thread = threading.Thread(target=_read_stream,
                                      args=(popen.stdout, stdout), daemon=True)
        err_thread = threading.Thread(target=_read_stream,
                                      args=(popen.stderr, stderr), daemon=True)
        out_thread.start()
        err_thread.start()
        self._emit("terminal.started", op=op, process_id=process_id,
                   command=command, cwd=self.cwd,
                   shell=self.shell.get("name"), status=RUNNING,
                   stdout_blob_id=stdout_blob,
                   stderr_blob_id=stderr_blob)
        timed_out = False
        try:
            try:
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(popen)
        finally:
            with self._lock:
                if self._active is popen:
                    self._active = None
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
        rc = None if timed_out else popen.returncode
        if timed_out:
            status = TIMEOUT
        elif rc == 0:
            status = COMPLETED
        else:
            status = FAILED
        duration = round((time.monotonic() - started) * 1000.0, 2)
        if not timed_out:
            probe_rc = self._apply_probes(probes)
            # cmd/PowerShell return the wrapper's own status, so their real
            # command exit code comes from the probe file; POSIX shells
            # already `exit $?`, so popen.returncode is authoritative.
            if self.shell.get("kind") != "posix" and probe_rc is not None:
                rc = probe_rc
                status = COMPLETED if rc == 0 else FAILED
        self._discard_probes(probes)
        with self._lock:
            current_cwd = self.cwd
        result = {
            "command": command,
            "cwd": current_cwd,
            "session_id": self.session_id,
            "process_id": process_id,
            "shell": self.shell.get("name", ""),
            "platform": self.platform,
            "status": status,
            "exit_code": rc,
            "stdout": stdout.text(),
            "stderr": stderr.text(),
            "duration": duration,
            "duration_ms": duration,
            "truncated": bool(stdout.truncated or stderr.truncated),
            # Full stream is always retrievable by these ids, whether or
            # not this particular run was truncated — terminal_output_read
            # is the one code path for "give me more output".
            "stdout_blob_id": stdout_blob, "stdout_total_chars": stdout.full_size,
            "stderr_blob_id": stderr_blob, "stderr_total_chars": stderr.full_size,
        }
        self._record(result)
        self._emit_output(op, process_id, result)
        kind = {"completed": "terminal.completed", "failed": "terminal.failed",
                "timeout": "terminal.timeout"}.get(status, "terminal.failed")
        self._emit(kind, op=op, process_id=process_id, command=command,
                   cwd=result["cwd"], exit_code=rc, duration=duration,
                   status=status, terminal=True,
                   stdout_blob_id=result.get("stdout_blob_id", ""),
                   stderr_blob_id=result.get("stderr_blob_id", ""))
        return result

    def _run_background(self, command, env) -> dict:
        process_id = self._new_process_id()
        probes = self._probe_paths_for(process_id)
        started = time.monotonic()

        def on_chunk(text):
            if self.on_output is not None:
                self.on_output(text)

        with self._lock:
            cwd = self.cwd
            base_env = self._base_env(env, probes)
        argv = self._shell_argv(self._wrap(command))
        popen = self._popen(argv, base_env, cwd)
        record = ProcessRecord(process_id, command, popen, cwd, started,
                               self.max_output_chars, on_output=on_chunk,
                               op=f"term:{process_id}", probes=probes,
                               blobs=self._blobs)
        out_thread = threading.Thread(target=_read_stream,
                                      args=(popen.stdout, record.stdout),
                                      daemon=True)
        err_thread = threading.Thread(target=_read_stream,
                                      args=(popen.stderr, record.stderr),
                                      daemon=True)
        out_thread.start()
        err_thread.start()
        record.attach(out_thread, err_thread)
        with self._lock:
            self._processes[process_id] = record
        self._emit("terminal.started", op=record.op, process_id=process_id,
                   command=command, cwd=cwd,
                   shell=self.shell.get("name"), status=RUNNING,
                   background=True,
                   stdout_blob_id=record.stdout_blob_id,
                   stderr_blob_id=record.stderr_blob_id)
        result = {
            "command": command,
            "cwd": cwd,
            "session_id": self.session_id,
            "process_id": process_id,
            "shell": self.shell.get("name", ""),
            "platform": self.platform,
            "status": RUNNING,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "duration": 0.0,
            "duration_ms": 0.0,
            "background": True,
        }
        self._record(dict(result, status=RUNNING))
        return result

    # -- process control -----------------------------------------------------
    def status(self, process_id: str) -> dict:
        self._refresh_all()
        with self._lock:
            record = self._processes.get(process_id)
        if record is None:
            raise ValidationError(f"unknown process: {process_id}")
        return record.to_dict()

    def stop(self, process_id: str, *, force: bool = False) -> dict:
        record = self._get_process(process_id)
        with record._lock:
            if record.status == RUNNING:
                if force:
                    record.popen.kill()
                else:
                    self._terminate(record.popen)
                record.status = STOPPED
                try:
                    record.exit_code = record.popen.wait(timeout=5)
                except Exception:
                    record.exit_code = None
            # Marked under the record lock so a concurrent _refresh_all can
            # never also emit a completion event for the same process.
            record.exit_emitted = True
        self._discard_probes(record.probes)
        result = record.to_dict()
        result["session_id"] = self.session_id
        self._emit("terminal.stopped", op=record.op, process_id=process_id,
                   command=record.command, cwd=record.cwd,
                   status=STOPPED, terminal=True,
                   stdout_blob_id=record.stdout_blob_id,
                   stderr_blob_id=record.stderr_blob_id)
        return result

    def kill(self, process_id: str) -> dict:
        return self.stop(process_id, force=True)

    def _get_process(self, process_id: str) -> ProcessRecord:
        self._refresh_all()
        with self._lock:
            record = self._processes.get(process_id)
        if record is None:
            raise ValidationError(f"unknown process: {process_id}")
        return record

    def _refresh_all(self) -> None:
        with self._lock:
            records = list(self._processes.values())
        for record in records:
            # claim_exit() is atomic: exactly one refresh emits the terminal
            # event, so concurrent polls cannot duplicate it.
            if not record.claim_exit():
                continue
            self._discard_probes(record.probes)
            kind = ("terminal.completed" if record.status == COMPLETED
                    else "terminal.failed")
            self._emit(kind, op=record.op,
                       process_id=record.process_id,
                       command=record.command, cwd=record.cwd,
                       exit_code=record.exit_code,
                       duration=record.duration_ms(),
                       status=record.status, terminal=True,
                       stdout_blob_id=record.stdout_blob_id,
                       stderr_blob_id=record.stderr_blob_id)

    # -- results / probes ----------------------------------------------------
    def _apply_probes(self, probes: dict) -> int | None:
        """Apply the cwd/env side effects and return the probed exit code
        (None when the shell does not need/emit one)."""
        with self._lock:
            return self._apply_probes_locked(probes)

    def _apply_probes_locked(self, probes: dict) -> int | None:
        pwd_path = probes["pwd"]
        rc_path = probes["rc"]
        env_path = probes["env"]
        try:
            with open(pwd_path, "r", encoding="utf-8", errors="replace") as fh:
                new_cwd = fh.read().strip()
            if new_cwd and os.path.isdir(new_cwd):
                self.cwd = new_cwd
        except OSError:
            pass
        probe_rc = None
        try:
            with open(rc_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if text:
                probe_rc = int(text.strip().splitlines()[-1].strip())
        except (OSError, ValueError):
            probe_rc = None
        try:
            with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
                parsed = _parse_env_blob(fh.read())
            if parsed:
                self.env = parsed
        except OSError:
            pass
        self._discard_probes(probes)
        return probe_rc

    def _record(self, result: dict) -> None:
        with self._lock:
            self._history_seq += 1
            seq = self._history_seq
        entry = {
            "seq": seq,
            "process_id": result.get("process_id", ""),
            "command": result.get("command", ""),
            "cwd": result.get("cwd", self.cwd),
            "status": result.get("status", ""),
            "exit_code": result.get("exit_code"),
            "duration_ms": result.get("duration_ms", 0.0),
            # Bounded preview only — this is what the hot deque/context_text
            # carries. Full stdout/stderr for this command stay retrievable
            # via terminal_output_read using stdout_blob_id/stderr_blob_id
            # below (never dropped, unlike the 1000-char cap that used to
            # apply here with no way to get the rest back).
            "stdout": (result.get("stdout") or "")[:1000],
            "stderr": (result.get("stderr") or "")[:1000],
            "stdout_blob_id": result.get("stdout_blob_id"),
            "stderr_blob_id": result.get("stderr_blob_id"),
        }
        with self._lock:
            self._history.append(entry)
        # Append-only persistent log: EVERY command this session ever ran,
        # regardless of the deque's history_limit — terminal_history_read
        # pages through it by seq once a command has aged out of the deque.
        try:
            import json
            self._blobs.append(self._history_blob_id,
                               json.dumps(entry, ensure_ascii=False,
                                         default=str) + "\n")
        except Exception:
            pass

    # -- context for the AI --------------------------------------------------
    def context_text(self, *, max_commands: int = 8,
                     max_chars: int | None = None) -> str:
        self._refresh_all()
        with self._lock:
            history = list(self._history)[-max(1, max_commands):]
            procs = [p.to_dict(include_output=False)
                     for p in self._processes.values()]
        lines = [
            f"Terminal session {self.session_id} "
            f"(shell={self.shell.get('name', '')}, platform={self.platform}, "
            f"cwd={self.cwd})"
        ]
        if procs:
            running = [p for p in procs if p["status"] == RUNNING]
            if running:
                lines.append("Running processes:")
                for p in running:
                    lines.append(f"  [{p['process_id']}] {p['command']}")
        if history:
            lines.append("Recent commands (most recent last):")
            for h in history:
                head = f"  $ {h['command']}  -> status={h['status']}"
                if h["exit_code"] is not None:
                    head += f" exit={h['exit_code']}"
                lines.append(head)
                if h["status"] in (FAILED, TIMEOUT) and h.get("stderr"):
                    # No artificial cap here: this text is shown to the AI so
                    # it can reason about the failure. The command's full
                    # stdout/stderr is already returned by the tool itself.
                    snippet = " ".join((h["stderr"] or "").split())
                    if snippet:
                        lines.append(f"    stderr: {snippet}")
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "…"
        # This text is injected into model prompts (Gateway + Provider), so
        # a credential-shaped value that appeared in a command or a stderr
        # snippet must never leave the session unredacted.
        try:
            from astra.security import redact_text
            text = redact_text(text)
        except Exception:
            pass
        return text

    # -- internals -----------------------------------------------------------
    def _terminate(self, popen) -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
            else:               # pragma: no cover - Windows only
                popen.terminate()
        except Exception:
            try:
                popen.terminate()
            except Exception:
                pass
        try:
            popen.wait(timeout=3)
        except Exception:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
                else:           # pragma: no cover - Windows only
                    popen.kill()
            except Exception:
                pass

    def _emit(self, kind: str, **data) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="terminal",
                             session_id=self.session_id, **data)
        except Exception:
            pass

    def _emit_output(self, op: str, process_id: str, result: dict) -> None:
        """Publish a bounded, capped output event — never raw unlimited text."""
        if self.events is None:
            return
        for stream in ("stdout", "stderr"):
            text = result.get(stream) or ""
            if not text.strip():
                continue
            snippet = text if len(text) <= 500 else text[:500] + "…"
            self._emit("terminal.output", op=op, process_id=process_id,
                       stream=stream, chars=len(text), snippet=snippet,
                       status=result.get("status"))
            break
