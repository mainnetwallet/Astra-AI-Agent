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


if __name__ == "__main__":
    unittest.main()
