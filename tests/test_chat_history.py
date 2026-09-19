"""Assistant chat survives a page refresh: the transcript is saved server-side,
including a reply that finishes after the browser already went away."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request

from astra.chat_log import ChatLog
from astra.security import redact
from astra.store import Store

from tests.helpers import make_stack


class ChatLogUnit(unittest.TestCase):
    def setUp(self):
        self.log = ChatLog(Store(":memory:"), redact=redact)

    def test_round_trip_and_after_id(self):
        self.log.add_user("hello", files=["a.pdf"])
        self.log.add_reply({"reply": "hi", "action": "none", "ok": True,
                            "data": {"x": 1}, "artifacts": [{"k": "v"}]})
        h = self.log.history()
        self.assertEqual([m["role"] for m in h["messages"]], ["user", "ai"])
        self.assertEqual(h["messages"][0]["files"], ["a.pdf"])
        self.assertEqual(h["messages"][1]["artifacts"], [{"k": "v"}])
        first = h["messages"][0]["id"]
        self.assertEqual([m["text"] for m in self.log.history(after_id=first)["messages"]], ["hi"])

    def test_pending_lifecycle(self):
        self.assertFalse(self.log.history()["pending"])
        t = self.log.begin()
        self.assertTrue(self.log.history()["pending"])
        self.log.end(t)
        self.assertFalse(self.log.history()["pending"])

    def test_pending_is_scoped_per_conversation(self):
        # A turn running in chat 1 must not show "typing…" in an unrelated
        # chat 2, and vice versa.
        self.log.add_user("hello")  # chat 1 needs a message or new_conversation() reuses it
        chat2 = self.log.new_conversation()
        t = self.log.begin(1)
        self.assertTrue(self.log.history(conversation_id=1)["pending"])
        self.assertFalse(self.log.history(conversation_id=chat2)["pending"])
        self.log.end(t)
        self.assertFalse(self.log.history(conversation_id=1)["pending"])

    def test_reply_follows_the_chat_it_was_sent_in_not_current(self):
        # Regression test: send a message in chat 1, then switch "current"
        # to a new chat (as the UI does when the user opens a new chat)
        # *before* the reply is recorded. The reply must still land in
        # chat 1, not whichever chat is "current" when add_reply runs.
        cid = self.log.add_user("hello from chat 1")
        chat2 = self.log.new_conversation()          # user opens a new chat
        self.assertEqual(self.log.current_id, chat2)  # current moved on
        self.log.add_reply({"reply": "hi", "ok": True}, conversation_id=cid)
        self.assertEqual(
            [m["text"] for m in self.log.history(conversation_id=cid)["messages"]],
            ["hello from chat 1", "hi"])
        self.assertEqual(self.log.history(conversation_id=chat2)["messages"], [])

    def test_secrets_are_redacted_and_clear_works(self):
        self.log.add_reply({"reply": "ok", "data": {"api_key": "sk-supersecretvalue123456"}})
        self.assertNotIn("supersecret", json.dumps(self.log.history()))
        self.log.clear()
        self.assertEqual(self.log.history()["messages"], [])


class ChatHistoryHttp(unittest.TestCase):
    def setUp(self):
        from astra.web import AstraServer
        self.stack = make_stack()
        agent = self.stack["agent"]
        self.release = threading.Event()

        def slow_handle(message, context="", attachments=None):
            self.release.wait(5)
            return {"reply": f"echo: {message}", "action": "none", "ok": True, "data": {}}
        agent.handle = slow_handle
        self.srv = AstraServer(("127.0.0.1", 0), self.stack["store"], agent, stack=self.stack)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        time.sleep(0.2)

    def tearDown(self):
        self.release.set()
        self.srv.shutdown()
        self.srv.server_close()

    def _req(self, path, method="GET", body=None, timeout=10):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def test_refresh_mid_turn_keeps_message_shows_pending_then_reply(self):
        # A "page" sends a message and is closed before the reply is ready.
        import socket
        body = json.dumps({"message": "ping"}).encode()
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        time.sleep(0.3)
        s.close()                                  # <- refresh: client is gone

        # The reloaded page: message is there and the turn is still running.
        h = self._req("/api/chat/history")["data"]
        self.assertEqual([(m["role"], m["text"]) for m in h["messages"]], [("user", "ping")])
        self.assertTrue(h["pending"])

        self.release.set()                         # the reply finishes now
        deadline = time.time() + 5
        while time.time() < deadline:
            h = self._req("/api/chat/history")["data"]
            if not h["pending"]:
                break
            time.sleep(0.1)
        self.assertFalse(h["pending"])
        self.assertEqual([(m["role"], m["text"]) for m in h["messages"]],
                         [("user", "ping"), ("ai", "echo: ping")])

        # Poll for just what's new, then clear.
        first = h["messages"][0]["id"]
        self.assertEqual(len(self._req(f"/api/chat/history?after_id={first}")["data"]["messages"]), 1)
        self._req("/api/chat/history", method="DELETE")
        self.assertEqual(self._req("/api/chat/history")["data"]["messages"], [])

    def test_new_chat_then_back_does_not_steal_the_reply(self):
        """Reproduces the reported bug: ask a question, open a new chat
        while it's still working, then switch back to the original chat.
        The reply must show up in the original chat (and only there), and
        the new chat must never show a stray "typing…" for it."""
        import socket
        body = json.dumps({"message": "ping in chat A"}).encode()
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        time.sleep(0.3)
        s.close()  # simulate navigating away mid-turn

        chat_a = self._req("/api/chat/history")["data"]["conversation_id"]

        # Open a new chat while chat A's reply is still pending.
        chat_b = self._req("/api/chat/conversations", method="POST")["data"]["id"]
        self.assertNotEqual(chat_a, chat_b)
        # Chat B is empty and must NOT show chat A's turn as pending.
        hb = self._req(f"/api/chat/conversations/{chat_b}")["data"]
        self.assertEqual(hb["messages"], [])
        self.assertFalse(hb["pending"])

        # Switch back to chat A — still pending, from A's own point of view.
        ha = self._req(f"/api/chat/conversations/{chat_a}")["data"]
        self.assertTrue(ha["pending"])

        self.release.set()  # let the agent finish
        deadline = time.time() + 5
        while time.time() < deadline:
            ha = self._req(f"/api/chat/conversations/{chat_a}")["data"]
            if not ha["pending"]:
                break
            time.sleep(0.1)

        self.assertEqual([(m["role"], m["text"]) for m in ha["messages"]],
                         [("user", "ping in chat A"), ("ai", "echo: ping in chat A")])
        # The reply must not have leaked into chat B.
        hb = self._req(f"/api/chat/conversations/{chat_b}")["data"]
        self.assertEqual(hb["messages"], [])

    def test_chat_response_carries_conversation_id(self):
        self.release.set()  # this test doesn't need the slow/pending behavior
        r = self._req("/api/chat", method="POST", body={"message": "hi"})
        self.assertEqual(r["data"]["conversation_id"],
                         self._req("/api/chat/history")["data"]["conversation_id"])

    def test_history_endpoint_accepts_explicit_conversation_id(self):
        # The frontend's background poller pins to a specific chat id rather
        # than trusting whatever the server considers "current" at poll
        # time (see chatWaitForReply in static/js/astra.js).
        self.release.set()
        self._req("/api/chat", method="POST", body={"message": "hi"})
        chat_a = self._req("/api/chat/history")["data"]["conversation_id"]
        chat_b = self._req("/api/chat/conversations", method="POST")["data"]["id"]
        self.assertNotEqual(chat_a, chat_b)
        # chat_b is now "current" server-side, but asking for chat_a by id
        # explicitly must still return chat_a's own (non-empty) transcript.
        h = self._req(f"/api/chat/history?conversation_id={chat_a}")["data"]
        self.assertEqual(h["conversation_id"], chat_a)
        self.assertEqual(len(h["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
