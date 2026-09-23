"""The read-only terminal-output endpoint the chat execution cards page.

The Assistant chat's cards show only a short preview; the full stdout/stderr
is fetched on demand from GET /api/v1/terminal/output, which is a thin
read-only wrapper over the SAME TerminalManager/BlobStore the
`terminal_output_read` tool uses.

What this pins:
  * it returns the captured record + one bounded page of output,
  * paging with offset/length reassembles the FULL stream (long output is
    never lost, only deferred),
  * a blob id can be read directly (the id the lifecycle events carry),
  * unknown process/blob are clean 404s, bad params clean 400s,
  * secrets are redacted on the way out,
  * IT NEVER RE-RUNS THE COMMAND (issuing the request twice does not execute
    anything a second time) — "clicking a card" must never execute a tool.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from astra.web import AstraSite, Request, WebApp

from tests.helpers import make_stack


class TerminalOutputApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.site = AstraSite(("127.0.0.1", 0), self.stack["store"],
                              self.stack["agent"], stack=self.stack)
        self.app = WebApp(self.site)
        self.manager = self.stack["terminal"]
        self.tmp = tempfile.mkdtemp()
        self.session = self.manager.get("conv-1", create=True, cwd=self.tmp)

    def tearDown(self):
        self.manager.close_all()
        self.stack["store"].close()

    def _get(self, query):
        resp = self.app.handle(Request("GET", "/api/terminal/output?" + query))
        return resp.status, json.loads(resp.body.decode("utf-8"))

    def _run(self, command):
        return self.session.exec(command, timeout=20, wait=True)

    # -- the happy path ------------------------------------------------------
    def test_captured_output_is_returned_with_its_record(self):
        result = self._run("printf 'hello\\nworld\\n'")
        status, body = self._get("process_id=" + result["process_id"])
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["command"], "printf 'hello\\nworld\\n'")
        self.assertEqual(data["cwd"], os.path.realpath(self.tmp))
        self.assertEqual(data["stream"], "stdout")
        self.assertEqual(data["text"], "hello\nworld\n")
        self.assertTrue(data["done"])
        self.assertIsNone(data["next_offset"])
        self.assertGreaterEqual(data["duration_ms"], 0)

    def test_v1_alias_hits_the_same_route(self):
        result = self._run("echo v1")
        resp = self.app.handle(Request(
            "GET", "/api/v1/terminal/output?process_id=" + result["process_id"]))
        self.assertEqual(resp.status, 200)

    # -- long output ---------------------------------------------------------
    def test_long_output_pages_back_the_full_stream(self):
        result = self._run("seq 1 4000")           # ~19k chars
        full = result["stdout"]
        self.assertGreater(len(full), 5000)
        collected, offset, guard = [], 0, 0
        while True:
            status, body = self._get(
                "process_id=%s&offset=%d&length=1200" % (result["process_id"], offset))
            self.assertEqual(status, 200)
            data = body["data"]
            collected.append(data["text"])
            if data["done"]:
                break
            offset = data["next_offset"]
            guard += 1
            self.assertLess(guard, 100, "pagination must terminate")
        self.assertEqual("".join(collected), full)

    def test_blob_id_can_be_read_directly(self):
        result = self._run("printf 'blob-content\\n'")
        blob = result["stdout_blob_id"]
        self.assertTrue(blob)
        status, body = self._get("blob_id=" + blob + "&length=100")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["text"], "blob-content\n")
        self.assertEqual(body["data"]["blob_id"], blob)
        self.assertEqual(body["data"]["total_chars"], len("blob-content\n"))

    def test_stderr_stream_is_available(self):
        result = self._run("printf 'oops\\n' 1>&2")
        status, body = self._get("process_id=%s&stream=stderr" % result["process_id"])
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["stream"], "stderr")
        self.assertIn("oops", body["data"]["text"])

    # -- errors --------------------------------------------------------------
    def test_unknown_process_is_a_clean_404(self):
        status, body = self._get("process_id=does-not-exist")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertNotIn("Traceback", json.dumps(body))

    def test_unknown_blob_is_a_clean_404(self):
        status, body = self._get("blob_id=nope-not-real")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_missing_and_bad_parameters_are_client_errors(self):
        status, body = self._get("")
        self.assertEqual(status, 400)
        result = self._run("echo x")
        status, body = self._get("process_id=%s&length=abc" % result["process_id"])
        self.assertEqual(status, 400)

    def test_unknown_session_does_not_create_one(self):
        before = list(self.manager.session_ids())
        status, _ = self._get("process_id=p1&session_id=no-such-session")
        self.assertEqual(status, 404)
        self.assertEqual(sorted(self.manager.session_ids()), sorted(before))

    # -- redaction -----------------------------------------------------------
    def test_secrets_in_the_output_are_redacted(self):
        self._run("printf 'ghp_0123456789abcdefghijklmnopqrstuvwxyz\\n'")
        result = self.session.exec("printf 'ghp_0123456789abcdefghijklmnopqrstuvwxyz\\n'",
                                   timeout=20, wait=True)
        status, body = self._get("process_id=" + result["process_id"])
        self.assertEqual(status, 200)
        blob = json.dumps(body)
        self.assertNotIn("ghp_0123456789abcdefghijklmnopqrstuvwxyz", blob)
        self.assertIn("***redacted***", blob)

    # -- READ-ONLY: it never executes anything again --------------------------
    def test_requesting_output_never_re_runs_the_command(self):
        result = self._run("printf 'x' >> counter.txt")
        counter = os.path.join(self.tmp, "counter.txt")
        with open(counter, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x")
        # hit BOTH read paths twice each
        for _ in range(2):
            self._get("process_id=" + result["process_id"])
            self._get("blob_id=" + result["stdout_blob_id"])
        with open(counter, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x",
                             "the command must not have run again")


if __name__ == "__main__":
    unittest.main()
