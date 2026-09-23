"""AgentRuntime + RuntimeManager — lifecycle, sessions and shared execution.

One `AgentRuntime` is one isolated development environment: its own
workspace, home, temp, package state and process tree, reached through the
proot engine in `astra.runtime.engine`. `RuntimeManager` owns the runtime
instances and is created once by `astra.bootstrap.build`, exactly like
`TerminalManager`/`BrowserManager`, so the Gateway, every Provider, the
agent tool loop and the HTTP API all drive the SAME runtime.

Chat ↔ Terminal sharing
-----------------------
A session id is the join key. The chat pipeline derives
`conv-<conversation_id>` for a conversation (the same scheme
`astra.terminal.manager.default_session_id_for` already uses) and the
Astra Agent Terminal opens `conv-<conversation_id>` too. When the ids match
they resolve to the *same* `PtyProcess` — the same shell, the same cwd, the
same filesystem. That is what makes "run `git clone` in chat, then `ls` in
the terminal and see the clone" true rather than aspirational, and it is
verified end-to-end by `tests/test_runtime_shared_session.py`.

`runtime_command` therefore does not spawn a throwaway shell: it types the
command into the session's live PTY, waits for a unique end-of-command
sentinel, and returns the captured slice. cwd and exported variables
persist exactly as they do in an interactive terminal, because it *is* the
interactive terminal.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid

from astra.core.events import new_op_id
from astra.core.exceptions import ValidationError
from astra.runtime import files as runtime_files
from astra.runtime import packages as runtime_packages
from astra.runtime.engine import (GUEST_HOME, GUEST_TMP, GUEST_WORKSPACE,
                                  AstraRuntimeUnavailable, RuntimeEngine)
from astra.runtime.pty import PtyProcess

DEFAULT_RUNTIME_ID = "default"
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0
SENTINEL_TIMEOUT = 1800.0

STATE_CREATED = "created"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"

# ANSI/CSI/OSC stripper for tool results. The live terminal keeps every
# escape sequence (that is what a terminal renderer needs); a tool result
# is plain text a model reads, so the control bytes come out.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"                  # other escapes
    r"|\r"
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", str(text or ""))


class AgentRuntime:
    """One isolated runtime instance."""

    def __init__(self, runtime_id: str, engine: RuntimeEngine, base_dir: str,
                 *, events=None, config=None, blobs=None,
                 title: str = ""):
        self.runtime_id = runtime_id
        self.engine = engine
        self.base_dir = os.path.abspath(base_dir)
        self.events = events
        self.config = config
        self.title = title or runtime_id
        self._lock = threading.RLock()
        self._sessions: dict[str, PtyProcess] = {}
        self._state = STATE_CREATED
        self._created_at = time.time()
        self._last_error = ""
        self._capabilities: dict | None = None
        self._capabilities_at = 0.0

        self.workspace_dir = os.path.join(self.base_dir, "workspace")
        self.home_dir = os.path.join(self.base_dir, "root")
        self.tmp_dir = os.path.join(self.base_dir, "tmp")
        self.uploads_dir = os.path.join(self.base_dir, "uploads")
        self.state_path = os.path.join(self.base_dir, "runtime.json")

        max_mb = self._cfg_int("RUNTIME_MAX_FILE_MB",
                               runtime_files.DEFAULT_MAX_FILE_MB)
        max_members = self._cfg_int("RUNTIME_MAX_MEMBERS",
                                    runtime_files.DEFAULT_MAX_MEMBERS)
        max_extract = self._cfg_int("RUNTIME_MAX_EXTRACT_MB",
                                    runtime_files.DEFAULT_MAX_EXTRACT_MB)
        self.paths = runtime_files.RuntimePaths(
            self.workspace_dir, self.home_dir, self.tmp_dir,
            max_file_mb=max_mb, max_members=max_members,
            max_extract_mb=max_extract)

        self.blobs = blobs
        if self.blobs is None:
            from astra.core.blob_store import BlobStore
            self.blobs = BlobStore()
        # Bounded per-session log of the commands run through this runtime.
        # It is what the Gateway/Provider are shown as "Live runtime context"
        # (see context_text) so both the chat agent and the terminal see the
        # same execution history, and it never grows without limit.
        self._command_log: dict[str, list[dict]] = {}
        self._command_log_max = self._cfg_int("RUNTIME_COMMAND_LOG_MAX", 50)

    # -- config --------------------------------------------------------------
    def _cfg_int(self, key: str, default: int) -> int:
        if self.config is None:
            return default
        try:
            value = self.config.get(key)
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    # -- events --------------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="runtime", runtime=self.runtime_id,
                             **data)
        except Exception:
            pass

    # -- binds ---------------------------------------------------------------
    @property
    def binds(self) -> list[tuple[str, str]]:
        return [(self.workspace_dir, GUEST_WORKSPACE),
                (self.home_dir, GUEST_HOME),
                (self.tmp_dir, GUEST_TMP)]

    # -- persistence ---------------------------------------------------------
    def _persist(self) -> None:
        payload = {"runtime_id": self.runtime_id, "title": self.title,
                   "state": self._state, "created_at": self._created_at,
                   "updated_at": time.time(), "last_error": self._last_error}
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _restore(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh) or {}
        except (OSError, ValueError):
            return
        self._created_at = float(payload.get("created_at") or self._created_at)
        self.title = payload.get("title") or self.title
        # A runtime is never "running" at process start: the previous
        # process's PTYs are gone. Persisted data (files, packages) is what
        # survives — exactly the spec's requirement that chat completion
        # must not destroy the runtime.
        if payload.get("state") in (STATE_RUNNING, STATE_FAILED):
            self._state = STATE_STOPPED

    # -- lifecycle -----------------------------------------------------------
    def create(self, *, reset: bool = False) -> dict:
        with self._lock:
            existed = os.path.isdir(self.base_dir)
            if reset and existed:
                shutil.rmtree(self.base_dir, ignore_errors=True)
                existed = False
            for path in (self.workspace_dir, self.home_dir, self.tmp_dir,
                         self.uploads_dir):
                os.makedirs(path, exist_ok=True)
            if not os.path.exists(self.state_path):
                self._created_at = time.time()
            if not existed:
                self._state = STATE_CREATED
                self._persist()
                self._emit("runtime.created", path=self.base_dir)
            return self.status(refresh_capabilities=False)

    def start(self) -> dict:
        info = self.engine.probe()
        if not info["available"]:
            self._state = STATE_FAILED
            self._last_error = info["reason"]
            self._persist()
            self._emit("runtime.failed", reason=info["reason"])
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: " + info["reason"])
        self.create()
        with self._lock:
            self._state = STATE_RUNNING
            self._last_error = ""
            self._persist()
        capabilities = self.capabilities(refresh=True)
        self._emit("runtime.started", container=info["container"],
                   rootfs=info["rootfs"], tools=len(capabilities.get("tools", {})))
        status = self.status()
        status["capabilities"] = capabilities
        return status

    def stop(self) -> dict:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        with self._lock:
            if self._state != STATE_FAILED:
                self._state = STATE_STOPPED
            self._persist()
        self._emit("runtime.stopped", sessions=len(sessions))
        return self.status(refresh_capabilities=False)

    def restart(self) -> dict:
        self.stop()
        return self.start()

    def reset(self) -> dict:
        """Wipe the runtime's user data, keeping the installed rootfs.

        Explicit and destructive by design — normal chat completion never
        calls this (spec §17)."""
        self.stop()
        for path in (self.workspace_dir, self.uploads_dir, self.tmp_dir):
            shutil.rmtree(path, ignore_errors=True)
        self.create()
        self._emit("runtime.reset", path=self.base_dir)
        return self.status(refresh_capabilities=False)

    def destroy(self) -> dict:
        """Delete the runtime and everything in it."""
        self.stop()
        with self._lock:
            self._state = STATE_CREATED
        shutil.rmtree(self.base_dir, ignore_errors=True)
        self._emit("runtime.destroyed", path=self.base_dir)
        return {"runtime_id": self.runtime_id, "destroyed": True,
                "path": self.base_dir}

    # -- status --------------------------------------------------------------
    def status(self, *, refresh_capabilities: bool = True) -> dict:
        info = self.engine.probe()
        with self._lock:
            sessions = [s.snapshot() for s in self._sessions.values()]
            state = self._state
            last_error = self._last_error
            created_at = self._created_at
        if info["available"] and state == STATE_FAILED:
            state = STATE_STOPPED
        return {
            "runtime_id": self.runtime_id,
            "title": self.title,
            "state": state,
            "available": bool(info["available"]),
            "reason": info.get("reason", ""),
            "backend": info.get("backend", ""),
            "container": info.get("container", ""),
            "rootfs": info.get("rootfs", ""),
            "rootfs_mode": info.get("rootfs_mode", "shared"),
            "base_dir": self.base_dir,
            "workspace": GUEST_WORKSPACE,
            "created_at": created_at,
            "last_error": last_error,
            "sessions": sessions,
            "session_count": len(sessions),
            "capabilities": (self.capabilities()
                             if refresh_capabilities else self._capabilities),
        }

    def capabilities(self, *, refresh: bool = False, ttl: float = 60.0) -> dict:
        with self._lock:
            cached, at = self._capabilities, self._capabilities_at
        if cached is not None and not refresh and (time.time() - at) < ttl:
            return dict(cached)
        if not self.engine.available():
            return {"available": False, "tools": {}, "managers": [],
                    "reason": self.engine.probe()["reason"]}
        self._emit("runtime.package.detect.started")
        result = runtime_packages.detect(self.engine, self.binds)
        with self._lock:
            self._capabilities = result
            self._capabilities_at = time.time()
        self._emit("runtime.package.detect.completed",
                   managers=result.get("managers", []))
        return dict(result)

    # -- PTY sessions --------------------------------------------------------
    def open_terminal(self, session_id: str, *, rows: int = 24, cols: int = 80,
                      on_output=None, title: str = "",
                      cwd: str = GUEST_WORKSPACE,
                      shell: str = "") -> PtyProcess:
        """Get or create the PTY for `session_id` (real interactive shell)."""
        if not self.engine.available():
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: " + self.engine.probe()["reason"])
        key = str(session_id or self.runtime_id)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                if on_output is not None:
                    existing.on_output = on_output
                # Deliberately NO resize here: rows/cols are the caller's
                # *desired initial* size, and a chat tool call must never
                # shrink a terminal the user has already sized. Explicit
                # resizes go through the resize endpoint / process.resize().
                return existing
            if existing is not None:
                self._sessions.pop(key, None)

        argv = self.engine.build_argv(
            binds=self.binds, cwd=cwd,
            argv=([shell, "-l"] if shell else self.engine.shell_argv()))
        process = PtyProcess(
            argv, process_id=key, cwd=cwd, rows=rows, cols=cols,
            on_output=on_output, blobs=self.blobs, blob_prefix="runtime_pty",
            title=title or key, command="interactive shell",
            session_id=self.runtime_id)
        process.env = {"TERM": "xterm-256color"}
        process.start()
        with self._lock:
            self._sessions[key] = process
        self._emit("terminal.started", session_id=key, pid=process.pid,
                   rows=rows, cols=cols, shell="bash",
                   # §18: a runtime PTY session is ALWAYS tagged, exactly
                   # like the command lifecycle below.
                   environment="agent_runtime")
        return process

    def get_terminal(self, session_id: str) -> PtyProcess | None:
        with self._lock:
            return self._sessions.get(str(session_id or self.runtime_id))

    def close_terminal(self, session_id: str) -> bool:
        key = str(session_id or self.runtime_id)
        with self._lock:
            process = self._sessions.pop(key, None)
        if process is None:
            return False
        process.close()
        self._emit("terminal.stopped", session_id=key)
        return True

    def terminals(self) -> list[dict]:
        with self._lock:
            return [s.snapshot() for s in self._sessions.values()]

    def close_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        return len(sessions)

    # -- command execution (shared with chat) -------------------------------
    def exec_command(self, command: str, *, session_id: str = "",
                     timeout: float | None = None,
                     rows: int = 24, cols: int = 80) -> dict:
        """Run `command` in the session's live PTY and capture its output.

        cwd/exported state persist because this types into the real shell
        rather than starting a new process — which is also why the chat and
        the terminal panel observe each other's work.
        """
        text = str(command or "").strip()
        if not text:
            raise ValidationError("command required")
        if timeout is not None and float(timeout) > 0:
            timeout = min(float(timeout), MAX_TIMEOUT)
        else:
            timeout = DEFAULT_TIMEOUT
        key = str(session_id or self.runtime_id)
        process = self.open_terminal(key, rows=rows, cols=cols)

        # The command's output is redirected to a file INSIDE the runtime
        # (which the host sees through the /tmp bind) instead of being
        # scraped out of the interactive stream. Scraping would mean
        # untangling the shell's own echo, prompts and ANSI redraws from the
        # real output — fragile, and wrong for anything that repaints the
        # screen. With the redirect the terminal stream only carries the
        # completion marker, and the result is read from the file verbatim.
        #
        # The marker is emitted in two literal pieces so the shell's echo of
        # the typed line can never contain the assembled marker.
        token = uuid.uuid4().hex[:12]
        # Registers (byte counts of the live stream) that a client can use
        # to jump straight to this command in the terminal scrollback.
        started = process.total_bytes()
        started_at = time.time()
        out_name = f".astra_cmd_{token}.out"
        guest_out = f"{GUEST_TMP}/{out_name}"
        host_out = os.path.join(self.tmp_dir, out_name)
        marker = f"__ASTRA_DONE_{token}__"
        # Per-command lifecycle on the SAME event contract the legacy host
        # terminal uses (`terminal.started` -> `terminal.output` ->
        # `terminal.completed`/`terminal.failed`, all sharing one `op`), so
        # the Activity Log, the chat execution cards and the status model
        # treat runtime work exactly like any other real execution.
        op = new_op_id()
        # Per-execution identity. The runtime keeps ONE persistent shell per
        # session (that is what makes cwd/export persist and the Astra Agent
        # Terminal show the same state), so the shell's pid is the same for
        # every command. The per-command handle every consumer pairs on is
        # therefore this execution id (`process_id`, matching the terminal
        # event contract); the real PTY pid rides along as `shell_pid`.
        exec_id = token
        prior = self.session_history(key, limit=1)
        self._emit("terminal.started", session_id=key, op=op,
                   process_id=exec_id, shell_pid=process.pid,
                   command=text[:400],
                   cwd=prior[-1]["cwd"] if prior else GUEST_WORKSPACE,
                   # NOTE: `runtime` is injected by `_emit` itself — passing
                   # it here too would be a duplicate keyword, raise inside
                   # `_emit` and silently DROP this event, so the per-command
                   # `terminal.started` must not repeat it.
                   shell="bash", status="running",
                   # §18: runtime execution is ALWAYS tagged, so the
                   # Activity Log can never confuse it with a host command.
                   environment="agent_runtime")
        # A brace group runs in the CURRENT shell (so `cd`/`export` persist)
        # while redirecting only the command's own output.
        script = (
            f"{{ {text}\n}} > {guest_out} 2>&1\n"
            f"printf '\\n%s%s%s\\n' '__ASTRA_DONE_' '{token}__' \"$?\"\n"
        )
        process.write(script.encode("utf-8"))

        deadline = started_at + float(timeout)
        payload: dict = {}
        while time.time() < deadline:
            payload = process.text_since(started)
            if marker in payload.get("data", ""):
                break
            if not process.alive:
                break
            time.sleep(0.05)

        raw = payload.get("data", "")
        idx = raw.find(marker)
        exit_code = None
        if idx >= 0:
            tail = raw[idx + len(marker):].lstrip()
            digits = ""
            for ch in tail:
                if ch.isdigit() or (ch == "-" and not digits):
                    digits += ch
                else:
                    break
            try:
                exit_code = int(digits)
            except ValueError:
                exit_code = None

        stdout = _read_results_file(host_out, self.paths.max_file_bytes)
        try:
            os.remove(host_out)
        except OSError:
            pass
        # Keep this execution's full stdout retrievable (BlobStore), exactly
        # like the host terminal's `terminal_stdout_<pid>` blob, so the card's
        # "full output" always resolves through the ONE retrieval path.
        stdout_blob = self._store_output(exec_id, stdout)

        duration_ms = int((time.time() - started_at) * 1000)
        timed_out = exit_code is None and process.alive
        status = ("timeout" if timed_out
                  else "completed" if exit_code == 0
                  else "failed" if exit_code is not None else "running")
        result = {
            "ok": exit_code == 0,
            "status": status,
            "session_id": key,
            "runtime": self.runtime_id,
            "command": text,
            "cwd": GUEST_WORKSPACE,
            "exit_code": exit_code,
            "stdout": stdout[-20000:],
            "stderr": "",
            "duration_ms": duration_ms,
            "truncated": len(stdout) > 20000,
            "stream_offset_end": payload.get("next_offset"),
            "blob_id": process.blob_id,
            "stdout_blob_id": stdout_blob,
            "execution_id": exec_id,
        }
        # Bounded output snippet for the Activity Log — the full text stays
        # in the result / BlobStore, never in the event table.
        if stdout:
            self._emit("terminal.output", session_id=key, op=op,
                       process_id=exec_id, stream="stdout",
                       chars=len(stdout),
                       snippet=(stdout[-500:] if len(stdout) > 500
                                else stdout), status=status,
                       environment="agent_runtime")
        status_event = ("terminal.completed" if exit_code == 0
                        else "terminal.timeout" if status == "timeout"
                        else "terminal.failed")
        self._emit(status_event, session_id=key, op=op, process_id=exec_id,
                   command=text[:200], cwd=result["cwd"], exit_code=exit_code,
                   status=status, terminal=True, duration=duration_ms,
                   stdout_blob_id=stdout_blob, stderr_blob_id="",
                   environment="agent_runtime")
        self._remember(key, {"command": text, "status": status,
                             "exit_code": exit_code, "cwd": result["cwd"],
                             "duration_ms": duration_ms,
                             # Bounded: this log is prompt context, not an
                             # output store — the full text is in the result
                             # and the BlobStore.
                             "stderr": str(result.get("stderr") or "")[:2000]})
        return result

    def _store_output(self, exec_id: str, text: str) -> str:
        """Persist one execution's full stdout in the shared BlobStore.
        Best-effort: retrieval is a convenience, never a failure mode."""
        if not self.blobs or not text:
            return ""
        try:
            blob = self.blobs.open(f"runtime_stdout_{exec_id}")
            self.blobs.append(blob, text)
            return blob
        except Exception:
            return ""

    # -- shared execution context (chat <-> terminal) -----------------------
    def _remember(self, session_id: str, entry: dict) -> None:
        with self._lock:
            log = self._command_log.setdefault(session_id, [])
            # Also cap how many SESSIONS are remembered, so a long-lived
            # process with many conversations cannot grow this unbounded.
            if len(self._command_log) > 64 and session_id not in self._command_log:
                for stale in list(self._command_log)[:-32]:
                    if stale != session_id:
                        self._command_log.pop(stale, None)
            log.append(entry)
            del log[:-max(1, self._command_log_max)]

    def session_history(self, session_id: str, *, limit: int = 20) -> list[dict]:
        """Recent commands run on `session_id`, most recent last."""
        with self._lock:
            return list(self._command_log.get(
                str(session_id or self.runtime_id), []))[-max(1, limit):]

    def context_text(self, session_id: str | None = None, *,
                     max_commands: int = 8,
                     max_chars: int | None = None) -> str:
        """Bounded, deterministic view of one runtime session's state —
        cwd, live processes and recent commands. Empty when the session has
        never been used, so a fresh conversation adds nothing to the prompt."""
        key = str(session_id or self.runtime_id)
        history = self.session_history(key, limit=max(1, max_commands))
        process = self.get_terminal(key)
        if not history and process is None:
            return ""
        lines = [f"Agent Runtime session {key} "
                 f"(runtime={self.runtime_id}, shell=bash, "
                 f"cwd={history[-1]['cwd'] if history else GUEST_WORKSPACE})"]
        if process is not None:
            lines.append(f"Live PTY: pid={process.pid} "
                         f"alive={'yes' if process.alive else 'no'} "
                         f"bytes={process.total_bytes()}")
        if history:
            lines.append("Recent commands (most recent last):")
            for h in history:
                head = f"  $ {h['command']}  -> status={h['status']}"
                if h.get("exit_code") is not None:
                    head += f" exit={h['exit_code']}"
                lines.append(head)
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            text = text[-max_chars:]
        return text

    def one_shot(self, command: str, *, cwd: str = GUEST_WORKSPACE,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Run a command in a fresh guest shell (no session state).

        Used by detection/verification style work where isolation matters
        but interactivity does not.
        """
        if not self.engine.available():
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: " + self.engine.probe()["reason"])
        return self.engine.run(binds=self.binds, cwd=cwd, command=command,
                               timeout=timeout)

    # -- packages ------------------------------------------------------------
    def package_managers(self, *, refresh: bool = False) -> list[str]:
        return list(self.capabilities(refresh=refresh).get("managers", []))

    def package_install(self, *, ecosystem: str, packages: list[str],
                        cwd: str = GUEST_WORKSPACE, global_scope: bool = False,
                        timeout: float = runtime_packages.INSTALL_TIMEOUT) -> dict:
        if not self.engine.available():
            raise AstraRuntimeUnavailable(
                "Agent Runtime unavailable: " + self.engine.probe()["reason"])
        managers = self.package_managers()
        return runtime_packages.install(
            self.engine, self.binds, ecosystem=ecosystem, packages=packages,
            managers=managers, cwd=cwd, global_scope=global_scope,
            timeout=timeout, on_event=self._emit)

    # -- files ---------------------------------------------------------------
    def list_directory(self, path: str = GUEST_WORKSPACE, *, limit: int = 500):
        return runtime_files.directory_list(self.paths, path, limit=limit)

    def file_info(self, path: str):
        return runtime_files.file_info(self.paths, path)

    def read_file(self, path: str, *, max_bytes: int = 200_000):
        return runtime_files.read_text(self.paths, path, max_bytes=max_bytes)

    def write_file(self, path: str, text: str, *, overwrite: bool = True):
        return runtime_files.write_text(self.paths, path, text,
                                        overwrite=overwrite)

    def make_directory(self, path: str, *, parents: bool = True):
        return runtime_files.make_directory(self.paths, path, parents=parents)

    def copy(self, source: str, destination: str):
        return runtime_files.copy_path(self.paths, source, destination)

    def move(self, source: str, destination: str):
        return runtime_files.move_path(self.paths, source, destination)

    def remove(self, path: str, *, recursive: bool = False):
        return runtime_files.remove_path(self.paths, path, recursive=recursive)

    def extract_archive(self, archive: str, destination: str = "",
                        *, strip_components: int = 0):
        self._emit("runtime.archive.extract.started", archive=archive)
        try:
            result = runtime_files.extract_archive(
                self.paths, archive, destination,
                strip_components=strip_components)
        except Exception as exc:
            self._emit("runtime.archive.extract.failed", archive=archive,
                       error=str(exc)[:200])
            raise
        self._emit("runtime.archive.extract.completed", archive=archive,
                   count=result.get("count", 0))
        return result

    def import_file(self, source_host_path: str, destination: str = "",
                    *, overwrite: bool = True):
        self._emit("runtime.file.import.started",
                   name=os.path.basename(source_host_path))
        try:
            result = runtime_files.import_file(
                self.paths, source_host_path, destination,
                overwrite=overwrite)
        except Exception as exc:
            self._emit("runtime.file.import.failed", error=str(exc)[:200])
            raise
        self._emit("runtime.file.import.completed", path=result.get("path"))
        return result

    def import_and_extract(self, source_host_path: str, destination: str = "",
                           *, strip_components: int = 0) -> dict:
        """Upload a file and, when it is an archive, extract it in place."""
        name = os.path.basename(source_host_path)
        target_dir = destination or GUEST_WORKSPACE
        # Staging lives in the runtime's uploads dir (never bound into the
        # guest), so the archive itself is not left lying in the workspace.
        staged_guest = f"{GUEST_TMP}/uploads/{name}"
        staged_host = os.path.join(self.uploads_dir, name)
        os.makedirs(self.uploads_dir, exist_ok=True)
        shutil.copyfile(source_host_path, staged_host)
        imported = self.import_file(staged_host, f"{GUEST_TMP}/{name}",
                                    overwrite=True)
        if runtime_files.is_archive(name):
            extracted = self.extract_archive(
                f"{GUEST_TMP}/{name}", target_dir,
                strip_components=strip_components)
            return {"imported": imported, "extracted": extracted,
                    "archive": True}
        moved = self.move(f"{GUEST_TMP}/{name}",
                          f"{target_dir.rstrip('/')}/{name}")
        return {"imported": imported, "extracted": None, "archive": False,
                "path": moved["destination"]}


def _read_results_file(host_path: str, max_bytes: int) -> str:
    """Read a command's captured output back from inside the runtime."""
    try:
        with open(host_path, "rb") as fh:
            data = fh.read(max(4096, min(int(max_bytes or 0) or 4096,
                                         64 * 1024 * 1024)))
    except OSError:
        return ""
    return strip_ansi(data.decode("utf-8", "replace"))


def _strip_echo(body: str, command: str) -> str:
    """Remove the shell's echo of the typed line (and its bracketed-paste
    wrappers) from the start of the captured output."""
    text = body
    # Bracketed paste / cursor markers the shell emits around the echo.
    for seq in ("\x1b[?2004h", "\x1b[?2004l", "\x1b[K"):
        text = text.replace(seq, "")
    lines = text.split("\n")
    cleaned: list[str] = []
    dropped_echo = False
    for line in lines:
        stripped = line.strip()
        if not dropped_echo:
            if not stripped:
                continue
            if command and (stripped == command.strip()
                            or command.strip() in stripped):
                dropped_echo = True
                continue
            dropped_echo = True
        cleaned.append(line)
    return "\n".join(cleaned)


class RuntimeManager:
    """Process-wide registry of `AgentRuntime` instances."""

    def __init__(self, engine: RuntimeEngine | None = None, *, base_dir=None,
                 events=None, config=None, store=None):
        self.engine = engine or RuntimeEngine(config=config)
        self.events = events
        self.config = config
        self.base_dir = os.path.abspath(base_dir or (
            (config.get("RUNTIME_DIR") if config else None)
            or os.path.join(os.path.expanduser("~"), ".astra", "runtime")
        ))
        from astra.core.blob_store import BlobStore
        self.blobs = BlobStore(store)
        self._runtimes: dict[str, AgentRuntime] = {}
        self._lock = threading.RLock()

    def get(self, runtime_id: str | None = None, *,
            create: bool = True) -> AgentRuntime | None:
        key = str(runtime_id or DEFAULT_RUNTIME_ID)
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing
            if not create:
                return None
            runtime = AgentRuntime(
                key, self.engine, os.path.join(self.base_dir, key),
                events=self.events, config=self.config, blobs=self.blobs)
            runtime._restore()
            runtime.create()
            self._runtimes[key] = runtime
            return runtime

    def default(self) -> AgentRuntime:
        return self.get(DEFAULT_RUNTIME_ID)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._runtimes)

    def status(self, runtime_id: str | None = None) -> dict:
        runtime = self.get(runtime_id)
        return runtime.status()

    def close_all(self) -> int:
        with self._lock:
            runtimes = list(self._runtimes.values())
        return sum(r.close_all() for r in runtimes)
