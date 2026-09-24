"""Windows PTY transport: the Astra-process half of a WSL runtime session.

`astra/runtime/pty.py` owns a real PTY through `pty.fork()`, which only
exists on POSIX hosts. On Windows the PTY lives INSIDE Ubuntu (see
`astra/runtime/wsl_bridge.py`) and this class is only the transport:
`wsl.exe`'s pipes carry the framed stdin channel and the raw terminal stream,
and the guest bridge turns them back into a real Linux pseudo-terminal.

Everything the rest of Astra uses is inherited unchanged from `PtyProcess` -
the hot replay buffer, the BlobStore copy of the complete stream,
`on_output` (the SSE feed), byte offsets for reconnect, `snapshot`,
`interrupt()` (Ctrl+C as raw 0x03 into the line discipline) and `eof()`
(Ctrl+D). A Windows session is therefore the same object to the terminal
pane, the chat tools and the reconnect path as an Android one.

The only value that differs in meaning is `pid`: it is the Windows pid of the
`wsl.exe` transport, not the guest shell's pid (which lives in another
namespace and is not addressable from the host).
"""
from __future__ import annotations

import os
import signal
import struct
import subprocess
import threading
import time

from astra.runtime.pty import READ_CHUNK, RUNNING, STOPPED, PtyProcess

# The frame format is shared with astra/runtime/wsl_bridge.py.
MAGIC = b"\xa5Z"
FRAME_DATA = 0x01
FRAME_RESIZE = 0x02
FRAME_SIGNAL = 0x03

# Signals are named, not numbered, on this transport: the process group being
# signalled lives inside Ubuntu, where the bridge turns the name back into its
# own SIG*. That also keeps the module importable on Windows, whose `signal`
# module has no SIGHUP/SIGKILL at all (`SharedPtyProcess.kill` may hand us a
# number, so the Windows-visible numbers are mapped too).
_SIGNAL_NAMES = {1: "HUP", 2: "INT", 3: "QUIT", 9: "KILL", 15: "TERM",
                 10: "USR1", 12: "USR2"}
for _name in ("SIGHUP", "SIGINT", "SIGQUIT", "SIGKILL", "SIGTERM", "SIGUSR1",
              "SIGUSR2"):
    _value = getattr(signal, _name, None)
    if _value is not None:
        _SIGNAL_NAMES[int(_value)] = _name[3:]

# `signal.SIGKILL` does not exist on Windows; this is the guest's number.
SIGKILL = getattr(signal, "SIGKILL", 9)
SIGTERM = getattr(signal, "SIGTERM", 15)


class WslPtyProcess(PtyProcess):
    """One PTY-backed runtime session transported over `wsl.exe`.

    `argv` is the full host command line from
    `WslRuntimeBackend.pty_argv` (wsl.exe -> unshare -> session bootstrap ->
    PTY bridge -> bash). stdin/stdout of that process ARE the session: a
    framed control channel in, the raw terminal stream out.
    """

    def __init__(self, argv, **kwargs):
        super().__init__(argv, **kwargs)
        self._proc = None
        self._write_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        creationflags = 0
        if os.name == "nt":
            # Never flash a console window: this transport is plumbing.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, creationflags=creationflags)
        self.pid = self._proc.pid
        self.started_at = time.time()
        self.status = RUNNING
        self._reader = threading.Thread(
            target=self._read_loop, name="wslpty-read-%s" % self.process_id,
            daemon=True)
        self._reader.start()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="wslpty-reap-%s" % self.process_id,
            daemon=True)
        self._reaper.start()
        # The guest PTY starts at the size the caller asked for.
        self._send_frame(FRAME_RESIZE,
                         ("%d %d" % (self.rows, self.cols)).encode("ascii"))
        return self

    def _read_loop(self):
        stream = self._proc.stdout if self._proc is not None else None
        while stream is not None:
            if self._closed:
                break
            read = getattr(stream, "read1", None) or stream.read
            try:
                chunk = read(READ_CHUNK)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self._absorb(chunk)
        self._mark_ended()

    def _reap_loop(self):
        proc = self._proc
        if proc is None:
            return
        try:
            code = proc.wait()
        except Exception:                          # noqa: BLE001
            code = None
        with self._lock:
            self.exit_code = None if code is None else int(code)
        self._mark_ended()

    def _close_fd(self):
        """Closing the transport also ends the guest session: the bridge
        treats a closed stdin as "the host is gone" and hangs up its shell."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:                      # noqa: BLE001
                pass

    # -- interaction --------------------------------------------------------
    def _send_frame(self, kind: int, payload: bytes) -> bool:
        """Write one framed control message to the bridge over stdin."""
        proc = self._proc
        stream = proc.stdin if proc is not None else None
        if stream is None:
            return False
        frame = (MAGIC + bytes(bytearray([kind]))
                 + struct.pack(">I", len(payload)) + payload)
        with self._write_lock:
            try:
                stream.write(frame)
                stream.flush()
            except (OSError, ValueError):
                return False
        return True

    def write(self, data) -> int:
        """Send bytes to the guest PTY's stdin (what the user typed)."""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if not data:
            return 0
        if not self._send_frame(FRAME_DATA, bytes(data)):
            return 0
        return len(data)

    def _apply_winsize(self, rows: int, cols: int) -> bool:
        """Resize the REAL guest PTY (the bridge applies TIOCSWINSZ)."""
        return self._send_frame(
            FRAME_RESIZE,
            ("%d %d" % (int(rows), int(cols))).encode("ascii"))

    def send_signal(self, sig: int) -> bool:
        """Deliver a signal to the session's foreground process group, which
        is the guest's job - the Windows host has no handle on it."""
        name = _SIGNAL_NAMES.get(int(sig))
        if name is None:
            return False
        return self._send_frame(FRAME_SIGNAL, name.encode("ascii"))

    def kill(self, *, graceful: bool = True) -> bool:
        """Stop the session (TERM, then KILL) inside Ubuntu.

        Overrides the POSIX implementation, which is built on `os.killpg`
        and `signal.SIGKILL` - neither of which exists on Windows.
        """
        if self.status != RUNNING:
            return False
        if graceful and self.send_signal(SIGTERM):
            deadline = time.time() + 3.0
            while time.time() < deadline and self.status == RUNNING:
                time.sleep(0.1)
            if self.status != RUNNING:
                return True
        self.send_signal(SIGKILL)
        with self._lock:
            if self.status == RUNNING:
                self.status = STOPPED
            if self.ended_at is None:
                self.ended_at = time.time()
        return True
