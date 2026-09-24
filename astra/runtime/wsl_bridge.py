#!/usr/bin/env python3
"""Astra WSL PTY bridge - the guest half of the Agent Terminal on Windows.

This program runs INSIDE the WSL2 Ubuntu distribution (as root, inside the
runtime's private mount namespace) and gives the Astra Agent Terminal a REAL
pseudo-terminal there. The Astra process on Windows talks to it over
`wsl.exe`'s stdin/stdout pipes, which are byte-transparent but are not a
terminal; the PTY itself - line discipline, job control, Ctrl+C as SIGINT,
Ctrl+D as EOF, TIOCSWINSZ resize, full-screen programs - is created here, in
Linux, by `pty.fork()`. Nothing about the terminal is emulated on Windows.

The host never sees `wsl.exe`, `cmd.exe` or a `C:\\` path as a capability:
this bridge is internal plumbing between Astra and Ubuntu's bash.

Protocol
--------
stdin (host -> guest) carries length-prefixed control frames::

    b"\\xa5Z" | type(1) | length(4, big endian) | payload

    0x01 DATA     payload is raw bytes for the PTY (what the user typed)
    0x02 RESIZE   payload is "rows cols"
    0x03 SIGNAL   payload is a signal name ("TERM", "KILL", "INT", ...)

stdout (guest -> host) is the raw terminal stream, unframed. stderr carries
diagnostics only. Closing stdin means the host process is gone, so the
session is hung up instead of leaving a stray shell inside Ubuntu.

The bridge exits with the shell's own exit status, and `wsl.exe` propagates
that to the host as the process exit code.
"""
import base64
import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

MAGIC = b"\xa5Z"
FRAME_DATA = 0x01
FRAME_RESIZE = 0x02
FRAME_SIGNAL = 0x03
FRAME_HEADER = 7                      # magic(2) + type(1) + length(4)
READ_CHUNK = 65536
DRAIN_DEADLINE = 2.0
EXIT_GRACE = 3.0

SIGNALS = {
    "HUP": signal.SIGHUP, "INT": signal.SIGINT, "QUIT": signal.SIGQUIT,
    "KILL": signal.SIGKILL, "TERM": signal.SIGTERM, "USR1": signal.SIGUSR1,
    "USR2": signal.SIGUSR2, "STOP": signal.SIGSTOP, "CONT": signal.SIGCONT,
}


def _write_all(fd, data):
    """Write every byte, or report that the far end is gone."""
    view = memoryview(data)
    while len(view):
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except OSError:
            return False
        if written <= 0:
            return False
        view = view[written:]
    return True


def _set_winsize(fd, rows, cols):
    try:
        packed = struct.pack("HHHH", int(rows), int(cols), 0, 0)
    except (TypeError, ValueError, struct.error):
        return False
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
        return True
    except OSError:
        return False


def _signal_group(pid, name):
    """Deliver a signal to the session's process group (its foreground
    program included), exactly as a terminal would on hangup/termination."""
    number = SIGNALS.get(str(name or "").upper())
    if number is None:
        return False
    try:
        os.killpg(os.getpgid(pid), number)
        return True
    except OSError:
        try:
            os.kill(pid, number)
            return True
        except OSError:
            return False


def _consume_frames(buffer, fd, pid):
    """Apply every COMPLETE frame in `buffer`, leaving any partial tail."""
    while True:
        start = buffer.find(MAGIC)
        if start < 0:
            # Not a frame: drop everything but a possible partial magic byte.
            if len(buffer) > 1:
                del buffer[:-1]
            return
        if start:
            del buffer[:start]
        if len(buffer) < FRAME_HEADER:
            return
        kind = buffer[2]
        length = struct.unpack(">I", bytes(buffer[3:FRAME_HEADER]))[0]
        if len(buffer) < FRAME_HEADER + length:
            return
        payload = bytes(buffer[FRAME_HEADER:FRAME_HEADER + length])
        del buffer[:FRAME_HEADER + length]
        if kind == FRAME_DATA:
            if payload and not _write_all(fd, payload):
                return
        elif kind == FRAME_RESIZE:
            parts = payload.decode("utf-8", "replace").split()
            if len(parts) == 2:
                _set_winsize(fd, parts[0], parts[1])
        elif kind == FRAME_SIGNAL:
            _signal_group(pid, payload.decode("utf-8", "replace"))


def _drain(fd, deadline):
    """Forward whatever the shell wrote before it exited."""
    while time.time() < deadline:
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
        except (OSError, ValueError):
            return
        if not ready:
            continue
        try:
            chunk = os.read(fd, READ_CHUNK)
        except OSError:
            return
        if not chunk:
            return
        if not _write_all(1, chunk):
            return


def _exit_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0


def _reap(pid, status, host_gone):
    """Wait for the shell, terminating it if the host has already gone."""
    if host_gone:
        _signal_group(pid, "HUP")
        _signal_group(pid, "TERM")
    deadline = time.time() + EXIT_GRACE
    while status is None and time.time() < deadline:
        try:
            done, state = os.waitpid(pid, os.WNOHANG)
        except OSError:
            return 0
        if done == pid:
            status = state
            break
        time.sleep(0.05)
    if status is None:
        _signal_group(pid, "KILL")
        try:
            _, status = os.waitpid(pid, 0)
        except OSError:
            return 0
    return _exit_code(status)


def _spawn(config):
    """Start the guest shell (or program) on a brand new PTY."""
    rows = int(config.get("rows") or 24)
    cols = int(config.get("cols") or 80)
    guest_argv = [str(a) for a in (config.get("argv") or ["/bin/bash"])]
    cwd = str(config.get("cwd") or "/workspace")
    umask = config.get("umask")
    env = dict((str(k), str(v))
               for k, v in (config.get("env") or {}).items())

    pid, fd = pty.fork()
    if pid == 0:                                  # child: the guest program
        # pty.fork() already made this a session leader with the slave side
        # as its controlling terminal, so job control and Ctrl+C work.
        if umask is not None:
            try:
                os.umask(int(str(umask), 8))
            except (TypeError, ValueError):
                pass
        try:
            os.chdir(cwd)
        except OSError:
            try:
                os.chdir("/")
            except OSError:
                pass
        try:
            os.execvpe(guest_argv[0], guest_argv, env)
        except Exception as exc:                  # noqa: BLE001
            try:
                os.write(2, ("astra runtime: cannot start %s: %s\r\n"
                             % (guest_argv[0], exc)).encode("utf-8", "replace"))
            except Exception:                     # noqa: BLE001
                pass
        os._exit(127)
    return pid, fd, (rows, cols)


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("astra wsl bridge: missing configuration\n")
        return 2
    try:
        raw = base64.b64decode(argv[1])
        config = json.loads(raw.decode("utf-8"))
    except Exception as exc:                      # noqa: BLE001
        sys.stderr.write("astra wsl bridge: bad configuration: %s\n" % exc)
        return 2

    pid, fd, (rows, cols) = _spawn(config)
    _set_winsize(fd, rows, cols)
    try:
        os.set_blocking(0, False)
    except (AttributeError, OSError):
        pass

    buffer = bytearray()
    stdin_open = True
    host_gone = False
    status = None
    while True:
        watched = [fd, 0] if stdin_open else [fd]
        try:
            ready, _, _ = select.select(watched, [], [], 0.25)
        except (OSError, ValueError):
            break
        if fd in ready:
            try:
                chunk = os.read(fd, READ_CHUNK)
            except InterruptedError:
                chunk = b""
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    chunk = b""
                else:
                    break
            if chunk and not _write_all(1, chunk):
                host_gone = True
                break
        if stdin_open and 0 in ready:
            try:
                data = os.read(0, READ_CHUNK)
            except InterruptedError:
                data = None
            except OSError as exc:
                data = (None if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                        else b"")
            if data == b"":
                # Host gone: hang this session up rather than leave a stray
                # shell running inside Ubuntu.
                stdin_open = False
                host_gone = True
                _signal_group(pid, "HUP")
                _signal_group(pid, "TERM")
            elif data:
                buffer.extend(data)
                _consume_frames(buffer, fd, pid)
        try:
            done, state = os.waitpid(pid, os.WNOHANG)
        except OSError:
            break
        if done == pid:
            status = state
            break

    _drain(fd, time.time() + DRAIN_DEADLINE)
    return _reap(pid, status, host_gone)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
