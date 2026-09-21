"""Persistent terminal session + manager: the shared capability itself.

These pin the behaviour both the Gateway and every Provider rely on:
structured results, cwd/env persistence, streaming buffers, background
process control, timeout, history and session isolation.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.terminal import (COMPLETED, FAILED, RUNNING, STOPPED, TIMEOUT,
                            TerminalManager, TerminalSession, detect_shell)


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
