"""Real PTY processes for Astra Agent Runtime.

This is a genuine pseudo-terminal, not a pipe: `pty.fork()` allocates a
master/slave pair, makes the slave the child's controlling terminal and
execs the runtime command in a new session. Everything a desktop terminal
does therefore works — a shell prompt, line editing, job control, ANSI
colour, `Ctrl+C` (the kernel's line discipline turns the raw 0x03 byte into
SIGINT for the foreground process group, we never fake it), full-screen
TUI programs, and `TIOCSWINSZ` resize.

Output is read off the master fd by a dedicated thread and fanned out three
ways:

* a capped in-memory replay buffer (what a reconnecting browser re-draws),
* the shared `BlobStore`, so the COMPLETE stream stays retrievable no
  matter how chatty the command was (a 500 MB build log never lands in
  RAM or the browser),
* an optional `on_output` callback (the SSE feed).

stdin is written straight back to the master fd, so what the browser sends
is what the PTY's line discipline sees.
"""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
import time

# Hot replay buffer per stream. Only this much is kept in RAM; the full
# stream always lives in the BlobStore.
DEFAULT_HOT_BYTES = 256 * 1024
READ_CHUNK = 65536

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"


class PtyProcess:
    """One real PTY running a command inside the Agent Runtime."""

    def __init__(self, argv, *, process_id: str, cwd: str = "/workspace",
                 env: dict | None = None, rows: int = 24, cols: int = 80,
                 on_output=None, blobs=None, blob_prefix: str = "pty",
                 hot_bytes: int = DEFAULT_HOT_BYTES, title: str = "",
                 command: str = "", session_id: str = ""):
        self.argv = list(argv)
        self.process_id = process_id
        self.cwd = cwd
        self.env = dict(env or {})
        self.rows = max(1, int(rows or 24))
        self.cols = max(2, int(cols or 80))
        self.title = title or process_id
        self.command = command
        self.session_id = session_id
        self.on_output = on_output
        self.blob_prefix = blob_prefix
        self.hot_bytes = max(4096, int(hot_bytes or DEFAULT_HOT_BYTES))

        self.pid: int | None = None
        self.fd: int | None = None
        self.started_at: float = 0.0
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.status = "created"
        self.error = ""

        self._lock = threading.RLock()
        self._parts: list[bytes] = []
        self._hot_size = 0
        self._total = 0          # bytes ever produced (monotonic offset)
        self._closed = False
        self._reader: threading.Thread | None = None
        self._reaper: threading.Thread | None = None

        self.blobs = blobs
        self.blob_id = ""
        if blobs is not None:
            self.blob_id = blobs.open(f"{blob_prefix}_{process_id}")

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> "PtyProcess":
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        pid, fd = pty.fork()
        if pid == 0:                                    # pragma: no cover
            # Child: pty.fork already made us a session leader with the
            # slave as controlling terminal.
            try:
                os.chdir(self.cwd or "/")
            except OSError:
                os.chdir("/")
            try:
                os.execvpe(self.argv[0], self.argv, env)
            except Exception as exc:                     # noqa: BLE001
                os.write(2, f"astra runtime: exec failed: {exc}\n".encode())
            finally:
                os._exit(127)

        self.pid = pid
        self.fd = fd
        self.started_at = time.time()
        self.status = RUNNING
        self._apply_winsize(self.rows, self.cols)
        self._reader = threading.Thread(
            target=self._read_loop, name=f"pty-read-{self.process_id}",
            daemon=True)
        self._reader.start()
        self._reaper = threading.Thread(
            target=self._reap_loop, name=f"pty-reap-{self.process_id}",
            daemon=True)
        self._reaper.start()
        return self

    def _apply_winsize(self, rows: int, cols: int) -> bool:
        if self.fd is None:
            return False
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(rows), int(cols), 0, 0))
            return True
        except OSError:
            return False

    def _read_loop(self) -> None:
        """Drain the master fd until the guest closes it."""
        fd = self.fd
        while True:
            if self._closed or fd is None:
                break
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(fd, READ_CHUNK)
            except OSError as exc:
                # EIO is how the kernel reports "the slave side is gone".
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                continue
            if not chunk:
                break
            self._absorb(chunk)
        self._mark_ended()

    def _absorb(self, chunk: bytes) -> None:
        with self._lock:
            self._parts.append(chunk)
            self._hot_size += len(chunk)
            self._total += len(chunk)
            while self._hot_size > self.hot_bytes and len(self._parts) > 1:
                dropped = self._parts.pop(0)
                self._hot_size -= len(dropped)
        if self.blobs is not None and self.blob_id:
            try:
                self.blobs.append(self.blob_id,
                                  chunk.decode("utf-8", "replace"))
            except Exception:
                pass
        if self.on_output is not None:
            try:
                self.on_output(self.process_id, chunk)
            except Exception:
                pass

    def _reap_loop(self) -> None:
        if self.pid is None:
            return
        try:
            _, status = os.waitpid(self.pid, 0)
        except (ChildProcessError, OSError):
            self._mark_ended()
            return
        with self._lock:
            if os.WIFEXITED(status):
                self.exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                self.exit_code = -os.WTERMSIG(status)
        self._mark_ended()

    def _mark_ended(self) -> None:
        with self._lock:
            if self.status not in (RUNNING, "created"):
                return
            if self.exit_code is None:
                self.status = STOPPED
            else:
                self.status = COMPLETED if self.exit_code == 0 else FAILED
            self.ended_at = time.time()
        self._close_fd()

    def _close_fd(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    # -- interaction --------------------------------------------------------
    def write(self, data) -> int:
        """Send bytes to the PTY's stdin (what the user typed)."""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if not data:
            return 0
        with self._lock:
            fd = self.fd
        if fd is None:
            return 0
        try:
            return os.write(fd, data)
        except OSError:
            return 0

    def resize(self, rows: int, cols: int) -> bool:
        """Apply a browser resize to the REAL PTY (TIOCSWINSZ)."""
        rows = max(1, int(rows or 24))
        cols = max(2, int(cols or 80))
        with self._lock:
            self.rows, self.cols = rows, cols
        return self._apply_winsize(rows, cols)

    def send_signal(self, sig: int) -> bool:
        """Deliver a signal to the PTY's foreground process group."""
        if self.pid is None:
            return False
        try:
            os.killpg(os.getpgid(self.pid), sig)
            return True
        except OSError:
            try:
                os.kill(self.pid, sig)
                return True
            except OSError:
                return False

    def interrupt(self) -> bool:
        """Ctrl+C, exactly as a real terminal: raw 0x03 to the line discipline."""
        return bool(self.write(b"\x03"))

    def eof(self) -> bool:
        """Ctrl+D."""
        return bool(self.write(b"\x04"))

    def kill(self, *, graceful: bool = True) -> bool:
        if self.status != RUNNING:
            return False
        if graceful and self.send_signal(signal.SIGTERM):
            deadline = time.time() + 3.0
            while time.time() < deadline and self.status == RUNNING:
                time.sleep(0.1)
            if self.status != RUNNING:
                return True
        self.send_signal(signal.SIGKILL)
        with self._lock:
            if self.status == RUNNING:
                self.status = STOPPED
            if self.ended_at is None:
                self.ended_at = time.time()
        return True

    def wait(self, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.time() + float(timeout)
        while self.status == RUNNING:
            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(0.05)
        return self.status

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if self.status == RUNNING:
            self.kill()
        self._close_fd()

    # -- observation --------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.status == RUNNING

    def duration(self) -> float:
        end = self.ended_at or time.time()
        return max(0.0, end - (self.started_at or end))

    def total_bytes(self) -> int:
        with self._lock:
            return self._total

    def replay(self) -> bytes:
        with self._lock:
            return b"".join(self._parts)

    def text_since(self, offset: int = 0) -> dict:
        """Replay from a byte offset — how a reconnecting client catches up.

        `offset` is the client's last seen position in the stream's total
        byte count. When the request predates the hot buffer the server says
        so (`truncated`) and the client re-draws from the buffer start; the
        complete output remains retrievable through the BlobStore.
        """
        with self._lock:
            total = self._total
            buffered = b"".join(self._parts)
            buffer_start = total - len(buffered)
        offset = max(0, int(offset or 0))
        truncated = offset < buffer_start
        if truncated:
            offset = buffer_start
        slice_ = buffered[offset - buffer_start:]
        return {"offset": offset, "next_offset": total, "total": total,
                "truncated": truncated, "data": slice_.decode("utf-8", "replace")}

    def snapshot(self, *, include_output: bool = True,
                 max_chars: int = 20000) -> dict:
        out = {
            "process_id": self.process_id,
            "session_id": self.session_id,
            "title": self.title,
            "command": self.command,
            "pid": self.pid,
            "status": self.status,
            "exit_code": self.exit_code,
            "rows": self.rows,
            "cols": self.cols,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": int(self.duration() * 1000),
            "total_bytes": self.total_bytes(),
            "blob_id": self.blob_id,
            "error": self.error,
        }
        if include_output:
            text = self.replay().decode("utf-8", "replace")
            out["output"] = text[-max_chars:] if max_chars else text
            out["truncated"] = len(text) > (max_chars or 0)
        return out
