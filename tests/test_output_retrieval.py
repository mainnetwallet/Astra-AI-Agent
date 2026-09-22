"""Regression/integration tests for the Browser/Terminal/Execution-history
retrieval work that follows commit 9435840.

These pin the contract: a hard cap (browser max_chars, terminal
max_output_chars, terminal/execution history retention limits) still
bounds what goes into the hot result/context, but nothing is silently
lost — the full content stays retrievable via `astra.core.blob_store` and
the new `*_read` tools.
"""
from __future__ import annotations

import json
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

    def test_oversized_single_command_does_not_stall_retrieval(self):
        # The command text itself is never capped in a history entry, so a
        # single very long command line can exceed BlobStore's per-call
        # chunk ceiling. terminal_history_read must still make forward
        # progress and let the caller reconstruct it, never stall forever.
        import json
        s = _session(history_limit=5)
        long_arg = "x" * 30000
        r1 = s.exec(f"echo {long_arg}")
        self.assertEqual(r1["status"], "completed")
        r2 = s.exec("echo short")
        self.assertEqual(r2["status"], "completed")

        parts = []
        normal_entries = []
        offset = 0
        for _ in range(20):
            page = terminal_history_read(
                {"session_id": "s1", "offset": offset, "length": 6000},
                manager=_FakeManagerFor(s))
            self.assertEqual(page["status"], "ok")
            if page.get("partial_line") is not None:
                parts.append(page["partial_line"])
            normal_entries.extend(page["entries"])
            if page["done"]:
                break
            self.assertIsNotNone(page["next_offset"],
                                 "must not stall: next_offset required "
                                 "while not done")
            self.assertNotEqual(page["next_offset"], offset,
                                "must not stall: offset must advance")
            offset = page["next_offset"]
        else:
            self.fail("terminal_history_read never finished paging (stalled)")

        recovered = json.loads("".join(parts))
        self.assertIn(long_arg, recovered["command"])
        self.assertEqual([e["command"] for e in normal_entries],
                         ["echo short"])
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

    def test_oversized_single_entry_does_not_stall_retrieval(self):
        # A single tool-result summary can now legitimately exceed
        # BlobStore's per-call chunk ceiling (no artificial cap before
        # 9435840/0d788f7). read_log must still make forward progress and
        # let the caller reconstruct the complete entry, never get stuck
        # returning zero entries at the same offset forever.
        import json
        hist = AgentExecutionHistory()
        big = json.dumps({"stdout": "x" * 20000, "ok": True})
        hist.record("s", "terminal_exec", ok=True, status="completed",
                   result=big, step=1)
        hist.record("s", "terminal_output_read", ok=True,
                   status="completed", result="small", step=2)

        parts = []
        normal_entries = []
        offset = 0
        for _ in range(20):  # generous ceiling; must finish well before this
            page = hist.read_log("s", offset=offset, length=6000)
            self.assertEqual(page["status"], "ok")
            if page.get("partial_line") is not None:
                parts.append(page["partial_line"])
            normal_entries.extend(page["entries"])
            if page["done"]:
                break
            self.assertIsNotNone(page["next_offset"],
                                 "must not stall: next_offset required "
                                 "while not done")
            self.assertNotEqual(page["next_offset"], offset,
                                "must not stall: offset must advance")
            offset = page["next_offset"]
        else:
            self.fail("read_log never finished paging (stalled)")

        recovered = json.loads("".join(parts))
        self.assertEqual(recovered["tool"], "terminal_exec")
        self.assertEqual([e["tool"] for e in normal_entries],
                         ["terminal_output_read"])


# ── BlobStore retention audit (no silent deletion) ─────────────────────

class BlobStoreRetentionAuditTests(unittest.TestCase):
    """Pins the audited retention contract: nothing in this process deletes
    `output_blobs` rows. Closing/reaping a session must NOT drop the full
    output a live model call may still be paging through, and a restart must
    not delete persisted data (it can only make rows unreachable — the known,
    documented orphan risk, not silent data loss)."""

    def _store(self):
        path = os.path.join(tempfile.mkdtemp(), "retention.db")
        return Store(path)

    def test_closing_a_session_does_not_delete_its_blobs(self):
        from astra.terminal.manager import DEFAULT_SESSION_ID
        store = self._store()
        mgr = TerminalManager(store=store)
        r = terminal_exec({"command": "python3 -c \"print('A'*30000)\""},
                          ctx=None, manager=mgr)
        self.assertEqual(r["status"], "completed")
        self.assertTrue(r["truncated"])
        blob_id = r["stdout_blob_id"]
        rows_before = store.fetchone("SELECT COUNT(*) c FROM output_blobs")["c"]
        self.assertGreaterEqual(rows_before, 1)

        # Close AND reap the session.
        self.assertTrue(mgr.close(DEFAULT_SESSION_ID))
        mgr.close_idle(0)

        rows_after = store.fetchone("SELECT COUNT(*) c FROM output_blobs")["c"]
        self.assertGreaterEqual(rows_after, rows_before,
                                "session close must not delete blobs")

        # The full stdout is still retrievable by blob id after close.
        collected, offset = [], 0
        while True:
            chunk = terminal_output_read({"blob_id": blob_id, "offset": offset,
                                          "length": 6000}, manager=mgr)
            self.assertEqual(chunk["status"], "ok")
            collected.append(chunk["text"])
            if chunk["done"]:
                break
            offset = chunk["next_offset"]
        self.assertEqual(len("".join(collected)), r["stdout_total_chars"])

    def test_restart_keeps_rows_but_drops_the_scope_mapping(self):
        # Documents the exact orphan risk instead of pretending a GC exists:
        # the persisted rows survive, but the in-RAM scope -> blob id map that
        # made them readable does not, so they become unreachable, not lost.
        store = self._store()
        hist1 = AgentExecutionHistory(blobs=BlobStore(store))
        hist1.record("conv-x", "toolx", ok=True, result="kept", step=1)
        page = hist1.read_log("conv-x", offset=0, length=6000)
        self.assertEqual(len(page["entries"]), 1)
        rows = store.fetchone("SELECT COUNT(*) c FROM output_blobs")["c"]
        self.assertEqual(rows, 1)

        # "Restart": a fresh history over the same persistent Store.
        hist2 = AgentExecutionHistory(blobs=BlobStore(store))
        after = hist2.read_log("conv-x", offset=0, length=6000)
        self.assertEqual(after["entries"], [])
        self.assertTrue(after["done"])
        # ...but the row is still there: no silent deletion.
        self.assertEqual(
            store.fetchone("SELECT COUNT(*) c FROM output_blobs")["c"], 1)


# ── Execution history: trusted-scope isolation ─────────────────────────

class ExecutionHistoryScopeIsolationTests(unittest.TestCase):
    """`execution_history_read` must read ONLY the trusted scope the runtime
    attached to the tool call. Conversation ids are not secrets, so a
    model-supplied `scope` argument must never be used for lookup or
    authorization — otherwise conversation A could page through B's log by
    guessing/knowing B's id."""

    def _hist_with_two_conversations(self):
        hist = AgentExecutionHistory(max_entries=2)
        for i in range(5):
            hist.record("conv-a", "tool_a", ok=True, status="completed",
                        result=f"a-result-{i}", step=i)
        for i in range(5):
            hist.record("conv-b", "tool_b", ok=True, status="completed",
                        result=f"b-result-{i}", step=i)
        return hist

    def test_conversation_reads_its_own_history(self):
        hist = self._hist_with_two_conversations()
        ctx = ToolContext(execution_history=hist, execution_scope="conv-a")
        page = execution_history_read({}, ctx=ctx)
        self.assertEqual(page["status"], "ok")
        self.assertEqual(page["scope"], "conv-a")
        self.assertTrue(page["entries"])
        self.assertTrue(all(e["tool"] == "tool_a" for e in page["entries"]))

    def test_model_scope_argument_cannot_read_another_conversation(self):
        hist = self._hist_with_two_conversations()
        ctx = ToolContext(execution_history=hist, execution_scope="conv-a")
        page = execution_history_read({"scope": "conv-b"}, ctx=ctx)
        # Still conv-a's log — never conv-b's.
        self.assertEqual(page["scope"], "conv-a")
        self.assertTrue(page["entries"])
        self.assertTrue(all(e["tool"] == "tool_a" for e in page["entries"]))
        self.assertNotIn("tool_b", json.dumps(page))
        # and the caller is told the argument was ignored
        self.assertIn("ignored", page.get("note", ""))

    def test_other_conversation_reads_its_own_history(self):
        hist = self._hist_with_two_conversations()
        ctx = ToolContext(execution_history=hist, execution_scope="conv-b")
        page = execution_history_read({"scope": "conv-a"}, ctx=ctx)
        self.assertEqual(page["scope"], "conv-b")
        self.assertTrue(page["entries"])
        self.assertTrue(all(e["tool"] == "tool_b" for e in page["entries"]))
        self.assertNotIn("tool_a", json.dumps(page))

    def test_pagination_contract_through_the_tool(self):
        hist = AgentExecutionHistory(max_entries=1)
        for i in range(12):
            hist.record("conv-p", "toolp", ok=True, result=f"p-{i}", step=i)
        ctx = ToolContext(execution_history=hist, execution_scope="conv-p")
        rows, seen_offsets, offset = [], [], 0
        while True:
            page = execution_history_read(
                {"offset": offset, "length": 200}, ctx=ctx)
            self.assertEqual(page["status"], "ok")
            self.assertEqual(page["offset"], offset)
            self.assertIn("done", page)
            self.assertIn("next_offset", page)
            self.assertIn("total_chars", page)
            seen_offsets.append(page["offset"])
            rows.extend(page["entries"])
            if page["done"]:
                self.assertIsNone(page["next_offset"])
                break
            self.assertIsNotNone(page["next_offset"])
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual([r["seq"] for r in rows], list(range(1, 13)))
        self.assertEqual(len(seen_offsets), len(set(seen_offsets)))

    def test_missing_trusted_scope_fails_closed(self):
        # A context with history but NO trusted scope must not silently
        # fall back to a model-supplied scope.
        hist = self._hist_with_two_conversations()
        ctx = ToolContext(execution_history=hist, execution_scope=None)
        with self.assertRaises(Exception):
            execution_history_read({"scope": "conv-b"}, ctx=ctx)

    def test_no_scope_override_through_the_agent_tool_loop(self):
        # End-to-end: the real loop builds the ToolContext, and the model's
        # scripted call names another conversation's scope.
        from astra.ai.agent_tool_loop import AgentToolLoop
        from astra.core.permissions import Policy
        from astra.tools.registry import ToolRegistry
        from astra.tools.builtins import register_builtins
        from tests.helpers import ScriptedBrain

        hist = self._hist_with_two_conversations()
        reg = ToolRegistry(policy=Policy(granted=["read"]))
        register_builtins(reg)
        brain = ScriptedBrain([
            json.dumps({"action": "tool", "tool": "execution_history_read",
                        "args": {"scope": "conv-b"},
                        "thought": "read the other conversation"}),
            json.dumps({"action": "final", "answer": "done"}),
        ])
        loop = AgentToolLoop(reg, execution_history=hist, max_steps=3)
        res = loop.run("read the history", brain, system_prompt="You are Astra.",
                       scope="conv-a")
        self.assertTrue(res.ok)
        # The tool result the model received must contain conv-a's entries
        # only — never conv-b's.
        joined = "\n".join(
            str(m.get("content", "")) for m in brain.calls[1]
            if m.get("role") == "user")
        self.assertIn("tool_a", joined)
        self.assertNotIn("tool_b", joined)


if __name__ == "__main__":
    unittest.main()
