"""Regression/integration tests for the Browser/Terminal/Execution-history
retrieval work that follows commit 9435840.

These pin the contract: a hard cap (browser max_chars, terminal
max_output_chars, terminal/execution history retention limits) still
bounds what goes into the hot result/context, but nothing is silently
lost — the full content stays retrievable via `astra.core.blob_store` and
the new `*_read` tools.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.core.blob_store import BlobStore
from astra.store import Store
from astra.terminal import TerminalManager, TerminalSession
from astra.terminal.tools import (terminal_exec, terminal_history_read,
                                  terminal_output_read)
from astra.ai.execution_history import AgentExecutionHistory
from astra.tools.builtins import execution_history_read
from astra.core.context import ToolContext


# ── BlobStore itself ─────────────────────────────────────────────────────

class BlobStoreTests(unittest.TestCase):
    def test_put_and_read_full_roundtrip(self):
        blobs = BlobStore()
        text = "x" * 25000
        blob_id = blobs.put("test", text)["id"]
        out = []
        offset = 0
        while True:
            chunk = blobs.read(blob_id, offset=offset, length=6000)
            self.assertEqual(chunk["status"], "ok")
            out.append(chunk["text"])
            if chunk["done"]:
                break
            offset = chunk["next_offset"]
        self.assertEqual("".join(out), text)

    def test_incremental_append_never_drops_data(self):
        blobs = BlobStore()
        blob_id = blobs.open("stream")
        parts = [f"line-{i}\n" for i in range(500)]
        for p in parts:
            blobs.append(blob_id, p)
        chunk = blobs.read(blob_id, offset=0, length=1_000_000)
        self.assertEqual(chunk["text"], "".join(parts))

    def test_persistent_store_survives_new_blobstore_instance(self):
        path = os.path.join(tempfile.mkdtemp(), "blobs.db")
        store1 = Store(path)
        blobs1 = BlobStore(store1)
        blob_id = blobs1.put("persisted", "hello world" * 100)["id"]
        store1.close()

        store2 = Store(path)
        blobs2 = BlobStore(store2)
        chunk = blobs2.read(blob_id, offset=0, length=6000)
        self.assertEqual(chunk["status"], "ok")
        self.assertEqual(chunk["text"], "hello world" * 100)
        store2.close()

    def test_unknown_id_is_a_clean_error_not_a_crash(self):
        blobs = BlobStore()
        r = blobs.read("nope", offset=0, length=100)
        self.assertEqual(r["status"], "error")


# ── Browser: observe() truncation is retrievable ────────────────────────

class FakePlaywright:
    def __init__(self, page):
        b = MagicMock()
        b.launch.return_value = b
        ctx = MagicMock()
        ctx.new_page.return_value = page
        b.new_context.return_value = ctx
        self.chromium = b

    def install_fake(self):
        mod_pw = MagicMock()
        mod_sync = MagicMock()
        sp = MagicMock()
        sp.start.return_value = self
        mod_sync.sync_playwright = lambda: sp
        sys.modules.setdefault("playwright", mod_pw)
        sys.modules["playwright.sync_api"] = mod_sync

    def uninstall_fake(self):
        sys.modules.pop("playwright", None)
        sys.modules.pop("playwright.sync_api", None)


def _fake_page(text):
    p = MagicMock()
    p.title.return_value = "Big Page"
    p.url = "https://example.com"
    p.goto.return_value = None
    body = MagicMock()
    body.inner_text.return_value = text
    body.count.side_effect = [2, 1, 1, 0]
    body.get_attribute.return_value = "x"
    body.evaluate.return_value = "INPUT"
    body.all.return_value = []
    p.locator.return_value = body
    p.close.return_value = None
    return p


class BrowserContentRetrievalTests(unittest.TestCase):
    def setUp(self):
        # A page whose visible text is well past the default max_chars.
        big_text = "\n".join(f"line {i} " + ("z" * 40) for i in range(400))
        self.assertGreater(len(big_text), 6000)
        self.big_text = big_text
        self.fp = FakePlaywright(_fake_page(big_text))
        self.fp.install_fake()
        from astra.browser.sessions import BrowserSession
        BrowserSession._checked = False
        BrowserSession.available = False
        self.BrowserSession = BrowserSession

    def tearDown(self):
        self.fp.uninstall_fake()
        self.BrowserSession._checked = False
        self.BrowserSession.available = False

    def test_large_page_content_is_not_lost_and_is_retrievable(self):
        from astra.browser.manager import BrowserManager
        mgr = BrowserManager()
        mgr.browser_open({"url": "https://example.com", "session": "ut"})
        obs = mgr.browser_observe({"session": "ut"})
        self.assertEqual(obs["status"], "ok")
        self.assertTrue(obs["truncated"])
        self.assertIsNotNone(obs["content_id"])
        self.assertEqual(obs["content_total_chars"], len(
            "\n".join(ln.strip() for ln in self.big_text.splitlines()
                     if ln.strip())))

        # Page through the full content and reconstruct it exactly.
        collected = []
        offset = 0
        while True:
            chunk = mgr.browser_content_read(
                {"content_id": obs["content_id"], "offset": offset,
                 "length": 4000})
            self.assertEqual(chunk["status"], "ok")
            collected.append(chunk["text"])
            if chunk["done"]:
                break
            offset = chunk["next_offset"]
        full = "".join(collected)
        self.assertEqual(len(full), obs["content_total_chars"])
        self.assertTrue(full.startswith("line 0"))
        self.assertIn("line 399", full)
        mgr.close_session("ut")

    def test_content_read_requires_content_id(self):
        from astra.browser.manager import BrowserManager
        mgr = BrowserManager()
        r = mgr.browser_content_read({})
        self.assertEqual(r["status"], "error")


# ── Terminal: stdout/stderr overflow is retrievable ─────────────────────

def _session(history_limit=50, max_output_chars=20000, **kw):
    return TerminalSession("s1", cwd=kw.pop("cwd", tempfile.mkdtemp()),
                           history_limit=history_limit,
                           max_output_chars=max_output_chars, **kw)


class TerminalOutputRetrievalTests(unittest.TestCase):
    def test_large_stdout_is_retrievable_beyond_the_cap(self):
        s = _session(max_output_chars=2000)
        # A command whose stdout is well beyond the 2000-char hot cap.
        r = s.exec("python3 -c \"print('A'*30000)\"")
        self.assertEqual(r["status"], "completed")
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["stdout"]), 2000)
        self.assertEqual(r["stdout_total_chars"], 30001)  # + trailing \n

        collected = []
        offset = 0
        while True:
            chunk = s._blobs.read(r["stdout_blob_id"], offset=offset,
                                  length=6000)
            self.assertEqual(chunk["status"], "ok")
            collected.append(chunk["text"])
            if chunk["done"]:
                break
            offset = chunk["next_offset"]
        full = "".join(collected)
        self.assertEqual(len(full), 30001)
        self.assertEqual(full.count("A"), 30000)
        s.close()

    def test_large_stderr_is_retrievable_beyond_the_cap(self):
        s = _session(max_output_chars=1500)
        r = s.exec("python3 -c \"import sys; sys.stderr.write('E'*20000)\"")
        self.assertTrue(r["truncated"])
        self.assertEqual(r["stderr_total_chars"], 20000)
        chunk = s._blobs.read(r["stderr_blob_id"], offset=19000, length=2000)
        self.assertEqual(chunk["status"], "ok")
        self.assertEqual(chunk["text"], "E" * 1000)
        self.assertTrue(chunk["done"])
        s.close()

    def test_terminal_output_read_tool(self):
        manager = TerminalManager(max_output_chars=1000)
        manager.get("t1", cwd=tempfile.mkdtemp())
        r = terminal_exec({"command": "python3 -c \"print('B'*5000)\"",
                           "session_id": "t1"}, manager=manager)
        self.assertTrue(r["truncated"])
        out = terminal_output_read({"blob_id": r["stdout_blob_id"],
                                    "offset": 0, "length": 6000},
                                   manager=manager)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["chars_returned"], min(6000, r["stdout_total_chars"]))
        manager.close_all()


class TerminalHistoryRetrievalTests(unittest.TestCase):
    def test_older_commands_survive_past_the_hot_deque(self):
        # A tiny hot deque so it's easy to push commands out of it.
        s = _session(history_limit=2)
        for i in range(5):
            r = s.exec(f"echo cmd{i}")
            self.assertEqual(r["status"], "completed")
        # Only the last 2 are in the bounded, in-memory view.
        hot = s.history(limit=10)
        self.assertEqual(len(hot), 2)
        self.assertEqual([h["command"] for h in hot],
                         ["echo cmd3", "echo cmd4"])

        # But ALL 5 are still retrievable from the persistent log, via the
        # tool-level parser.
        entries = []
        offset = 0
        while True:
            page = terminal_history_read(
                {"session_id": "s1", "offset": offset, "length": 400},
                manager=_FakeManagerFor(s))
            entries.extend(page["entries"])
            if page["done"]:
                break
            offset = page["next_offset"]
        self.assertEqual([e["command"] for e in entries],
                         [f"echo cmd{i}" for i in range(5)])
        s.close()


class _FakeManagerFor:
    """Minimal manager stand-in so terminal_history_read can resolve a
    session by id without standing up a full TerminalManager."""

    def __init__(self, session):
        self._session = session

    def get(self, session_id=None, create=True, cwd=None):
        return self._session


# ── Execution history: older entries survive past the hot deque ────────

class ExecutionHistoryRetrievalTests(unittest.TestCase):
    def test_older_entries_survive_past_max_entries(self):
        hist = AgentExecutionHistory(max_entries=3)
        for i in range(10):
            hist.record("scope-a", "some_tool", ok=True,
                       status="completed", result=f"result-{i}", step=i)
        hot = hist.entries("scope-a", limit=100)
        self.assertEqual(len(hot), 3)  # bounded, as before

        # The full log of all 10 is still retrievable.
        rows = []
        offset = 0
        while True:
            page = hist.read_log("scope-a", offset=offset, length=200)
            self.assertEqual(page["status"], "ok")
            rows.extend(page["entries"])
            if page["done"]:
                break
            offset = page["next_offset"]
        self.assertEqual(len(rows), 10)
        self.assertEqual([r["seq"] for r in rows], list(range(1, 11)))
        self.assertTrue(all("result-" in r["summary"] for r in rows))

    def test_execution_history_read_tool_uses_context_scope(self):
        hist = AgentExecutionHistory(max_entries=2)
        for i in range(4):
            hist.record("conv-1", "toolx", ok=True, result=f"r{i}", step=i)
        ctx = ToolContext(execution_history=hist, execution_scope="conv-1")
        page = execution_history_read({}, ctx=ctx)
        self.assertEqual(page["status"], "ok")
        self.assertEqual(page["scope"], "conv-1")
        self.assertGreaterEqual(len(page["entries"]), 1)

    def test_execution_history_read_without_history_errors_cleanly(self):
        ctx = ToolContext()
        r = execution_history_read({}, ctx=ctx)
        self.assertEqual(r["status"], "error")


if __name__ == "__main__":
    unittest.main()
