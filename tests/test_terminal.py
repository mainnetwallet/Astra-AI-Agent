"""Persistent terminal session + manager: the shared capability itself.

These pin the behaviour both the Gateway and every Provider rely on:
structured results, cwd/env persistence, streaming buffers, background
process control, timeout, history and session isolation.
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.terminal import (COMPLETED, FAILED, RUNNING, STOPPED, TIMEOUT,
                            TerminalManager, TerminalSession, detect_shell)
from tests.helpers import requires_posix_terminal


def _session(**kw):
    return TerminalSession("s1", cwd=kw.pop("cwd", tempfile.mkdtemp()), **kw)


class SessionExecutionTests(unittest.TestCase):
    def test_structured_result_shape(self):
        s = _session()
        r = s.exec("echo hello")
        for key in ("command", "cwd", "session_id", "process_id", "shell",
                    "status", "exit_code", "stdout", "stderr", "duration"):
            self.assertIn(key, r)
        self.assertEqual(r["session_id"], "s1")
        self.assertEqual(r["status"], COMPLETED)
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("hello", r["stdout"])
        self.assertEqual(r["stderr"], "")
        self.assertEqual(r["shell"], s.shell["name"])
        s.close()

    @requires_posix_terminal
    def test_stdout_and_stderr_are_separate(self):
        s = _session()
        r = s.exec("echo out; echo err 1>&2")
        self.assertIn("out", r["stdout"])
        self.assertNotIn("err", r["stdout"])
        self.assertIn("err", r["stderr"])
        s.close()

    def test_nonzero_exit_is_structured_failure_not_exception(self):
        s = _session()
        r = s.exec("exit 42")
        self.assertEqual(r["status"], FAILED)
        self.assertEqual(r["exit_code"], 42)
        s.close()

    def test_missing_command_raises(self):
        from astra.core.exceptions import ValidationError
        s = _session()
        with self.assertRaises(ValidationError):
            s.exec("   ")
        s.close()


class PersistenceTests(unittest.TestCase):
    @requires_posix_terminal
    def test_cwd_persists_between_commands(self):
        base = tempfile.mkdtemp()
        sub = os.path.join(base, "sub")
        os.mkdir(sub)
        s = TerminalSession("s", cwd=base)
        r1 = s.exec("cd sub")
        self.assertEqual(r1["status"], COMPLETED)
        self.assertEqual(os.path.realpath(r1["cwd"]), os.path.realpath(sub))
        # the NEXT command continues from the new cwd — no `cd sub` again
        r2 = s.exec("pwd")
        self.assertEqual(os.path.realpath(r2["stdout"].strip()),
                         os.path.realpath(sub))
        self.assertEqual(os.path.realpath(r2["cwd"]), os.path.realpath(sub))
        s.close()

    @requires_posix_terminal
    def test_environment_persists_between_commands(self):
        s = _session()
        s.exec("export ASTRA_TEST_VAR=abc123")
        r = s.exec("echo $ASTRA_TEST_VAR")
        self.assertIn("abc123", r["stdout"])
        s.close()

    def test_set_cwd_validates(self):
        from astra.core.exceptions import ValidationError
        s = _session()
        with self.assertRaises(ValidationError):
            s.set_cwd("/no/such/dir/astra")
        s.close()

    @requires_posix_terminal
    def test_sessions_are_isolated(self):
        a = TerminalSession("a", cwd=tempfile.mkdtemp())
        b = TerminalSession("b", cwd=tempfile.mkdtemp())
        a.exec("export ISOLATED=aaa")
        b.exec("export ISOLATED=bbb")
        self.assertIn("aaa", a.exec("echo $ISOLATED")["stdout"])
        self.assertIn("bbb", b.exec("echo $ISOLATED")["stdout"])
        self.assertNotEqual(a.cwd, b.cwd)
        a.close()
        b.close()


class TimeoutAndStreamTests(unittest.TestCase):
    def test_timeout_kills_and_reports(self):
        s = _session()
        start = time.monotonic()
        r = s.exec("sleep 30", timeout=0.5)
        elapsed = time.monotonic() - start
        self.assertEqual(r["status"], TIMEOUT)
        self.assertLess(elapsed, 15)
        s.close()

    def test_stream_callback_receives_output(self):
        chunks = []
        s = TerminalSession("s", cwd=tempfile.mkdtemp(),
                            on_output=chunks.append)
        s.exec("echo streamed-line")
        self.assertTrue(any("streamed-line" in c for c in chunks))
        s.close()

    def test_output_is_capped(self):
        s = TerminalSession("s", cwd=tempfile.mkdtemp(), max_output_chars=50)
        r = s.exec("yes X | head -c 5000")
        self.assertLessEqual(len(r["stdout"]), 51)
        self.assertTrue(r["truncated"])
        s.close()


class BackgroundProcessTests(unittest.TestCase):
    def test_start_status_stop(self):
        s = _session()
        started = s.exec("sleep 30", wait=False)
        self.assertEqual(started["status"], RUNNING)
        self.assertEqual(s.status(started["process_id"])["status"], RUNNING)
        stopped = s.stop(started["process_id"])
        self.assertEqual(stopped["status"], STOPPED)
        self.assertEqual(s.status(started["process_id"])["status"], STOPPED)
        s.close()

    def test_background_process_completes_and_reports_exit(self):
        s = _session()
        started = s.exec("echo bg-done", wait=False)
        deadline = time.time() + 10
        status = RUNNING
        while time.time() < deadline and status == RUNNING:
            time.sleep(0.05)
            status = s.status(started["process_id"])["status"]
        self.assertEqual(status, COMPLETED)
        self.assertIn("bg-done", s.status(started["process_id"])["stdout"])
        s.close()

    def test_kill_stops_a_stubborn_process(self):
        s = _session()
        started = s.exec("sleep 30", wait=False)
        self.assertEqual(s.kill(started["process_id"])["status"], STOPPED)
        s.close()

    def test_close_kills_background_processes(self):
        s = _session()
        s.exec("sleep 30", wait=False)
        s.close()
        with self.assertRaises(Exception):
            s.exec("echo nope")

    def test_unknown_process_raises(self):
        from astra.core.exceptions import ValidationError
        s = _session()
        with self.assertRaises(ValidationError):
            s.status("nope")
        s.close()


class HistoryAndContextTests(unittest.TestCase):
    def test_history_records_commands_and_results(self):
        s = _session()
        s.exec("echo hi")
        s.exec("exit 5")
        hist = s.history(limit=10)
        self.assertEqual([h["command"] for h in hist], ["echo hi", "exit 5"])
        self.assertEqual(hist[0]["exit_code"], 0)
        self.assertEqual(hist[1]["exit_code"], 5)
        self.assertEqual(hist[1]["status"], FAILED)
        s.close()

    def test_context_text_is_bounded_and_informative(self):
        s = _session()
        s.exec("cd /tmp && echo x")
        s.exec("exit 9")
        text = s.context_text(max_commands=10, max_chars=800)
        self.assertIn(s.session_id, text)
        self.assertIn(s.cwd, text)
        self.assertIn("exit 9", text)
        self.assertLessEqual(len(text), 801)
        # deterministic trim
        small = s.context_text(max_commands=1, max_chars=400)
        self.assertNotIn("cd /tmp", small)
        s.close()

    def test_process_ids_are_unique(self):
        s = _session()
        ids = {s.exec("echo x")["process_id"] for _ in range(5)}
        self.assertEqual(len(ids), 5)
        s.close()


class ManagerTests(unittest.TestCase):
    def test_get_creates_and_reuses(self):
        m = TerminalManager()
        a = m.get("conv-1")
        b = m.get("conv-1")
        self.assertIs(a, b)
        self.assertNotIn("conv-2", m.session_ids())
        m.get("conv-2")
        self.assertIn("conv-2", m.session_ids())
        m.close_all()

    def test_close_all_returns_count_and_marks_closed(self):
        m = TerminalManager()
        s = m.get("conv-1")
        self.assertEqual(m.close_all(), 1)
        self.assertTrue(s.closed)
        self.assertEqual(m.session_ids(), [])

    def test_close_idle(self):
        m = TerminalManager(idle_seconds=1000)
        m.get("conv-1")
        self.assertEqual(m.close_idle(), [])
        m.close_all()

    def test_context_text_empty_for_unknown_session(self):
        m = TerminalManager()
        self.assertEqual(m.context_text("nope"), "")

    def test_config_drives_limits(self):
        class Cfg:
            def get(self, key, default=""):
                return {"TERMINAL_MAX_OUTPUT_CHARS": "64",
                        "TERMINAL_HISTORY_LIMIT": "4"}.get(key, default)
        m = TerminalManager(config=Cfg())
        s = m.get("x")
        self.assertEqual(s.max_output_chars, 64)
        s.close()
        m.close_all()


class ShellDetectionTests(unittest.TestCase):
    def test_default_shell_is_posix_on_this_platform(self):
        shell = detect_shell()
        self.assertIn(shell["kind"], ("posix", "cmd", "powershell"))
        self.assertTrue(shell["name"])
        self.assertTrue(shell["path"])

    def test_override_is_honoured(self):
        old = os.environ.get("ASTRA_TERMINAL_SHELL")
        os.environ["ASTRA_TERMINAL_SHELL"] = "/bin/sh"
        try:
            self.assertEqual(detect_shell()["kind"], "posix")
        finally:
            if old is None:
                os.environ.pop("ASTRA_TERMINAL_SHELL", None)
            else:
                os.environ["ASTRA_TERMINAL_SHELL"] = old


if __name__ == "__main__":
    unittest.main()


class _Recorder:
    """Minimal event sink for terminal lifecycle assertions."""

    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})
        return self.rows[-1]

    def kinds(self, kind=None):
        return [r["kind"] for r in self.rows
                if kind is None or r["kind"] == kind]


class ProbeIsolationTests(unittest.TestCase):
    """Regression: cwd/env/exit-code probes were a single set of files per
    session, written by EVERY command's shell wrapper — so a background
    command finishing (or starting) could overwrite or delete the probes a
    concurrently-running foreground command was about to read, corrupting
    its cwd/exit code. Each command must own its probe files."""

    def test_probe_files_are_per_command_and_not_shared(self):
        s = _session()
        p1 = s._probe_paths_for("s1-1")
        p2 = s._probe_paths_for("s1-2")
        self.assertTrue(set(p1.values()).isdisjoint(set(p2.values())))
        base1 = tempfile.mkdtemp()
        base2 = tempfile.mkdtemp()
        # simulate command #2's wrapper finishing mid-way through command #1
        for probes, base in ((p2, base2), (p1, base1)):
            with open(probes["pwd"], "w") as fh:
                fh.write(base + "\n")
            with open(probes["rc"], "w") as fh:
                fh.write("0\n")
        rc = s._apply_probes(p1)
        self.assertEqual(rc, 0)
        self.assertEqual(os.path.realpath(s.cwd), os.path.realpath(base1))
        # applying a command's probes consumes them (no stale files)
        self.assertFalse(os.path.exists(p1["pwd"]))
        s.close()


class CloseDoesNotBlockTests(unittest.TestCase):
    """Regression: a session lock used to be held for the whole duration of a
    foreground command, so close() (and therefore server shutdown) blocked
    until the command finished, and process-control calls contended behind
    it. close() must return promptly and kill the in-flight command."""

    def test_close_returns_immediately_and_kills_inflight_command(self):
        s = _session()
        done = {}

        def run_slow():
            r = s.exec("sleep 20", timeout=30)
            done["status"] = r["status"]

        th = threading.Thread(target=run_slow)
        th.start()
        time.sleep(0.4)
        start = time.monotonic()
        s.close()
        close_s = time.monotonic() - start
        th.join(timeout=10)
        self.assertFalse(th.is_alive(), "in-flight command was not unblocked")
        self.assertLess(close_s, 3.0,
                        "close() blocked on the in-flight command")
        self.assertTrue(s.closed)
        self.assertIn(done.get("status"), ("failed", "timeout", "completed"))

    def test_stop_does_not_block_behind_a_foreground_command(self):
        s = _session()
        bg = s.exec("sleep 30", wait=False)
        th = threading.Thread(target=lambda: s.exec("sleep 20", timeout=30))
        th.start()
        time.sleep(0.4)
        start = time.monotonic()
        stopped = s.stop(bg["process_id"])
        elapsed = time.monotonic() - start
        self.assertEqual(stopped["status"], STOPPED)
        self.assertLess(elapsed, 3.0, "stop() blocked behind the foreground run")
        s.close()
        th.join(timeout=10)


class DuplicateCompletionEventTests(unittest.TestCase):
    """Regression: concurrent status()/refresh calls could each emit a
    terminal.completed event for the same background process."""

    def test_completion_event_emitted_exactly_once_under_concurrency(self):
        rec = _Recorder()
        s = TerminalSession("dup", cwd=tempfile.mkdtemp(), events=rec)
        started = s.exec("echo done", wait=False)
        pid = started["process_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if s.status(pid)["status"] != RUNNING:
                break
            time.sleep(0.02)
        # hammer the same process from many threads
        threads = [threading.Thread(target=lambda: s.status(pid))
                   for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        completions = rec.kinds("terminal.completed")
        self.assertEqual(len(completions), 1)
        # exactly one terminal event of any kind for that process
        terminals = [r for r in rec.rows if r["data"].get("terminal") is True]
        self.assertEqual(len(terminals), 1)
        s.close()


class ManagerBoundsTests(unittest.TestCase):
    """A long-lived server must not accumulate sessions/processes forever."""

    def test_lru_idle_session_is_evicted_at_the_cap(self):
        m = TerminalManager(max_sessions=2)
        a = m.get("a")
        time.sleep(0.01)
        m.get("b")
        time.sleep(0.01)
        m.get("c")   # evicts the LRU idle session ("a")
        self.assertTrue(a.closed)
        self.assertEqual(set(m.session_ids()), {"b", "c"})
        m.close_all()

    def test_session_with_live_process_is_not_evicted_or_idle_reaped(self):
        m = TerminalManager(max_sessions=2, idle_seconds=0.0001)
        busy = m.get("busy")
        busy.exec("sleep 30", wait=False)
        m.get("x")
        m.get("y")   # must evict x, never the busy session
        self.assertIsNotNone(m.get("busy", create=False))
        self.assertFalse(busy.closed)
        # close_idle must also skip a session that owns a live process
        time.sleep(0.01)
        self.assertNotIn("busy", m.close_idle())
        self.assertFalse(busy.closed)
        m.close_all()

    def test_close_idle_emits_completion_for_finished_background_process(self):
        """Regression: a background process that finished on its own never
        got a terminal Activity-Log event unless someone polled status(), so
        its "… running" row lingered forever. The idle sweep must refresh
        (and therefore close out) the finished process before reaping."""
        rec = _Recorder()
        m = TerminalManager(events=rec, idle_seconds=0.0001)
        session = m.get("bg")
        started = session.exec("echo done", wait=False)
        pid = started["process_id"]
        # Let the process finish on its own WITHOUT polling it (a status()
        # call would emit the completion itself and hide the bug).
        time.sleep(0.5)
        self.assertNotIn("terminal.completed", rec.kinds())
        closed = m.close_idle()
        self.assertIn("bg", closed)
        completions = [r for r in rec.rows
                       if r["kind"] == "terminal.completed"
                       and r["data"].get("process_id") == pid]
        self.assertEqual(len(completions), 1)
        m.close_all()
