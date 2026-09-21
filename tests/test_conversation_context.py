"""Regression tests for multi-turn conversation history / memory.

Covers the full path: ChatLog (persistence) -> ConversationContextBuilder
(canonical, provider-independent history) -> ChatPipeline (hands the SAME
history to both the Gateway and the Provider) -> /api/chat (wires it all
together per-request and persists the reply back).

Bug being regression-tested: before this fix, `/api/chat` read a `context`
string straight off the client request body, and the browser never sent
one — so every turn ran with empty history and a follow-up like "why?" had
nothing to resolve against, even though a full transcript was sitting in
ChatLog the whole time.
"""
import json
import threading
import time
import unittest
import urllib.request

from astra.ai.conversation_context import ConversationContextBuilder
from astra.ai.chat_pipeline import ChatPipeline
from astra.ai.router import RoutingResult
from astra.chat_log import ChatLog
from astra.security import redact
from astra.store import Store

from tests.helpers import LiveServer, make_stack


# ── ConversationContextBuilder: unit tests ─────────────────────────────────
class ConversationContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.log = ChatLog(Store(":memory:"), redact=redact)
        self.builder = ConversationContextBuilder(self.log, max_chars=1000,
                                                   max_turns=20)

    def test_fresh_conversation_has_empty_history(self):
        cid = self.log.current_id
        ctx = self.builder.build(cid)
        self.assertEqual(ctx.messages, [])
        self.assertFalse(ctx)          # __bool__
        self.assertEqual(ctx.as_text(), "")
        self.assertEqual(ctx.as_provider_messages(), [])

    def test_none_conversation_id_is_empty_not_an_error(self):
        ctx = self.builder.build(None)
        self.assertEqual(ctx.messages, [])

    def test_chronological_user_and_assistant_turns(self):
        cid = self.log.current_id
        self.log.add_user("what is the capital of France?", conversation_id=cid)
        self.log.add_reply({"reply": "Paris.", "ok": True}, conversation_id=cid)
        self.log.add_user("and Germany?", conversation_id=cid)
        self.log.add_reply({"reply": "Berlin.", "ok": True}, conversation_id=cid)

        ctx = self.builder.build(cid)
        self.assertEqual(
            [(m["role"], m["content"]) for m in ctx.messages],
            [("user", "what is the capital of France?"),
             ("assistant", "Paris."),
             ("user", "and Germany?"),
             ("assistant", "Berlin.")])

    def test_current_user_message_is_excluded_not_duplicated(self):
        """The message about to be answered must not appear inside its own
        history — callers either build() before persisting it, or pass
        exclude_message_id for the row that was just inserted."""
        cid = self.log.current_id
        self.log.add_user("first message", conversation_id=cid)
        self.log.add_reply({"reply": "first reply", "ok": True}, conversation_id=cid)
        # The current turn IS persisted before build() here, unlike the real
        # /api/chat flow, to prove exclude_message_id does its job even in
        # that ordering. `add_user` returns the CONVERSATION id, not the
        # message row id, so the row id is read back from history().
        self.log.add_user("second message (current)", conversation_id=cid)
        current_id = self.log.history(conversation_id=cid)["messages"][-1]["id"]

        ctx = self.builder.build(cid, exclude_message_id=current_id)
        contents = [m["content"] for m in ctx.messages]
        self.assertIn("first message", contents)
        self.assertIn("first reply", contents)
        self.assertNotIn("second message (current)", contents)
        self.assertEqual(contents.count("second message (current)"), 0)

    def test_build_before_insert_never_needs_exclusion(self):
        """The pattern web.py actually uses: snapshot history BEFORE the
        current message is written, so there is nothing to exclude and no
        way for it to leak into its own context."""
        cid = self.log.current_id
        self.log.add_user("hi", conversation_id=cid)
        self.log.add_reply({"reply": "hello!", "ok": True}, conversation_id=cid)

        history_before = self.builder.build(cid)
        self.log.add_user("now what?", conversation_id=cid)

        contents = [m["content"] for m in history_before.messages]
        self.assertEqual(contents, ["hi", "hello!"])
        self.assertNotIn("now what?", contents)

    def test_conversation_isolation_never_mixes_history(self):
        log = self.log
        cid_a = log.current_id
        log.add_user("secret A stuff", conversation_id=cid_a)
        log.add_reply({"reply": "ack A", "ok": True}, conversation_id=cid_a)

        cid_b = log.new_conversation()
        log.add_user("totally unrelated B stuff", conversation_id=cid_b)
        log.add_reply({"reply": "ack B", "ok": True}, conversation_id=cid_b)

        ctx_a = self.builder.build(cid_a)
        ctx_b = self.builder.build(cid_b)

        text_a = " ".join(m["content"] for m in ctx_a.messages)
        text_b = " ".join(m["content"] for m in ctx_b.messages)
        self.assertIn("secret A stuff", text_a)
        self.assertNotIn("secret A stuff", text_b)
        self.assertIn("totally unrelated B stuff", text_b)
        self.assertNotIn("totally unrelated B stuff", text_a)

    def test_failed_assistant_reply_is_not_useful_context(self):
        cid = self.log.current_id
        self.log.add_user("do the thing", conversation_id=cid)
        self.log.add_reply({"reply": "sorry, failed", "ok": False}, conversation_id=cid)
        ctx = self.builder.build(cid)
        contents = [m["content"] for m in ctx.messages]
        self.assertIn("do the thing", contents)
        self.assertNotIn("sorry, failed", contents)

    def test_trimming_keeps_newest_turns_within_char_budget(self):
        cid = self.log.current_id
        builder = ConversationContextBuilder(self.log, max_chars=60, max_turns=20)
        for i in range(10):
            self.log.add_user(f"user turn {i} " + "x" * 5, conversation_id=cid)
            self.log.add_reply({"reply": f"assistant turn {i}", "ok": True},
                               conversation_id=cid)
        ctx = builder.build(cid)
        contents = [m["content"] for m in ctx.messages]
        # the newest turn must always survive trimming
        self.assertIn("assistant turn 9", contents)
        self.assertIn("user turn 9 xxxxx", contents)
        # something old must have been dropped given the tiny budget
        self.assertNotIn("user turn 0 xxxxx", contents)
        self.assertLessEqual(sum(len(c) for c in contents), 60 + len(contents[0]))
        # still chronological (oldest kept turn before newest kept turn)
        self.assertEqual(contents, sorted(contents, key=contents.index))

    def test_trimming_by_turn_count(self):
        cid = self.log.current_id
        builder = ConversationContextBuilder(self.log, max_chars=10_000, max_turns=4)
        for i in range(10):
            self.log.add_user(f"u{i}", conversation_id=cid)
            self.log.add_reply({"reply": f"a{i}", "ok": True}, conversation_id=cid)
        ctx = builder.build(cid)
        self.assertEqual(len(ctx.messages), 4)
        self.assertEqual([m["content"] for m in ctx.messages], ["u8", "a8", "u9", "a9"])

    def test_trimming_is_deterministic(self):
        cid = self.log.current_id
        builder = ConversationContextBuilder(self.log, max_chars=40, max_turns=6)
        for i in range(8):
            self.log.add_user(f"question number {i}", conversation_id=cid)
            self.log.add_reply({"reply": f"answer number {i}", "ok": True},
                               conversation_id=cid)
        first = builder.build(cid).messages
        second = builder.build(cid).messages
        third = builder.build(cid).messages
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_single_oversized_turn_is_still_kept(self):
        cid = self.log.current_id
        builder = ConversationContextBuilder(self.log, max_chars=5, max_turns=20)
        self.log.add_user("this single message is way over budget", conversation_id=cid)
        ctx = builder.build(cid)
        self.assertEqual(len(ctx.messages), 1)

    def test_as_text_and_provider_messages_agree(self):
        cid = self.log.current_id
        self.log.add_user("hi", conversation_id=cid)
        self.log.add_reply({"reply": "hello", "ok": True}, conversation_id=cid)
        ctx = self.builder.build(cid)
        self.assertEqual(ctx.as_text(), "User: hi\nAssistant: hello")
        self.assertEqual(ctx.as_provider_messages(),
                         [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}])


# ── ChatLog: retry/refresh dedupe ──────────────────────────────────────────
class ChatLogRetryDedupeTests(unittest.TestCase):
    def setUp(self):
        self.log = ChatLog(Store(":memory:"), redact=redact)

    def test_identical_resubmit_while_pending_is_not_duplicated(self):
        cid = self.log.add_user("hello there")
        token = self.log.begin(cid)
        # Simulates a refresh/retry firing the same POST again before the
        # first turn's reply has been recorded.
        cid2 = self.log.add_user("hello there", conversation_id=cid)
        self.assertEqual(cid, cid2)
        msgs = self.log.history(conversation_id=cid)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["hello there"])
        self.log.end(token)

    def test_resubmit_after_turn_ends_is_a_normal_new_message(self):
        cid = self.log.add_user("same text")
        token = self.log.begin(cid)
        self.log.add_reply({"reply": "ok", "ok": True}, conversation_id=cid)
        self.log.end(token)
        self.log.add_user("same text", conversation_id=cid)
        msgs = self.log.history(conversation_id=cid)["messages"]
        self.assertEqual([m["text"] for m in msgs if m["role"] == "user"],
                         ["same text", "same text"])

    def test_different_text_while_pending_is_never_blocked(self):
        cid = self.log.add_user("first")
        token = self.log.begin(cid)
        self.log.add_user("second, genuinely different", conversation_id=cid)
        msgs = self.log.history(conversation_id=cid)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["first", "second, genuinely different"])
        self.log.end(token)


# ── ChatPipeline: Gateway and Provider must see the SAME history ──────────
def understand(final_request="", was_incomplete=False, provider="gemini",
               model="gemini-pro", criteria=("answers the question",)):
    return json.dumps({"final_request": final_request,
                       "was_incomplete": was_incomplete, "provider": provider,
                       "model": model, "criteria": list(criteria),
                       "reason": "best fit"})


def verdict(v="complete", missing=(), action="fix", instructions=""):
    return json.dumps({"verdict": v, "missing": list(missing),
                       "action": action, "instructions": instructions})


class _FakeGateway:
    def __init__(self, replies, usable=True):
        self.replies = list(replies)
        self.calls = []
        self.usable = usable

    def is_usable(self):
        return self.usable

    def chat(self, messages, model=None, max_tokens=500, category=None, trace=""):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence=None, semantic_verifier=None, max_tokens=500):
        from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
        return GatewayTaskCompletionSupervisor().supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)


class _FakeRouter:
    def __init__(self, outputs, targets=None):
        self.outputs = list(outputs)
        self.requests = []
        self._targets = targets or [
            {"provider": "gemini", "model": "gemini-pro",
             "capabilities": ["chat"], "quality": "high",
             "context_window": 100000}]

    def available_targets(self):
        return list(self._targets)

    def route_request(self, req):
        self.requests.append(req)
        out = self.outputs.pop(0)
        if out is None:
            return RoutingResult(ok=False, error="all providers failed")
        return RoutingResult(ok=True, text=out,
                             provider=req.preferred_provider or "gemini",
                             model=req.preferred_model or "gemini-pro")


class GatewayAndProviderSeeSameHistoryTests(unittest.TestCase):
    def test_gateway_and_provider_both_receive_identical_history(self):
        history = [{"role": "user", "content": "my name is Alex"},
                   {"role": "assistant", "content": "nice to meet you, Alex"}]
        gw = _FakeGateway([understand(final_request="what is my name?"),
                           verdict("complete")])
        rt = _FakeRouter(["Your name is Alex."])
        pipe = ChatPipeline(gw, rt, max_tokens=500)

        out = pipe.run("why?", history=history)
        self.assertTrue(out["ok"])

        # Gateway call #1 (understand) was shown the same prior turns...
        understand_prompt = gw.calls[0][1]["content"]
        self.assertIn("my name is Alex", understand_prompt)
        self.assertIn("nice to meet you, Alex", understand_prompt)

        # ...and the Provider received them as real conversation turns, in
        # order, ahead of the current user turn — not just Gateway-only.
        provider_messages = rt.requests[0].messages
        roles_and_content = [(m["role"], m["content"]) for m in provider_messages
                             if isinstance(m.get("content"), str)]
        self.assertIn(("user", "my name is Alex"), roles_and_content)
        self.assertIn(("assistant", "nice to meet you, Alex"), roles_and_content)
        # current turn appears exactly once, and after the history
        self.assertEqual(provider_messages[-1]["role"], "user")

    def test_current_message_is_not_duplicated_in_provider_history(self):
        history = [{"role": "user", "content": "hello"},
                   {"role": "assistant", "content": "hi!"}]
        gw = _FakeGateway([understand(final_request="how are you?"),
                           verdict("complete")])
        rt = _FakeRouter(["I'm doing well."])
        pipe = ChatPipeline(gw, rt, max_tokens=500)

        pipe.run("how are you?", history=history)
        provider_messages = rt.requests[0].messages
        current_turn_occurrences = sum(
            1 for m in provider_messages
            if isinstance(m.get("content"), str) and m["content"] == "how are you?")
        self.assertEqual(current_turn_occurrences, 1)

    def test_empty_history_is_a_fresh_conversation(self):
        gw = _FakeGateway([understand(final_request="hi"), verdict("complete")])
        rt = _FakeRouter(["hello!"])
        pipe = ChatPipeline(gw, rt, max_tokens=500)
        out = pipe.run("hi", history=[])
        self.assertTrue(out["ok"])
        # no prior-turn messages before the current user turn
        provider_messages = rt.requests[0].messages
        self.assertEqual(len(provider_messages), 2)   # system + current user
        self.assertEqual(provider_messages[-1]["content"], "hi")

    def test_conversation_context_object_accepted_directly(self):
        """`run(history=...)` also accepts a ConversationContext instance
        (what ConversationContextBuilder.build() returns), not just a bare
        list — web.py passes the object straight through."""
        from astra.ai.conversation_context import ConversationContext
        ctx = ConversationContext(
            messages=[{"role": "user", "content": "remember 42"},
                     {"role": "assistant", "content": "ok, 42"}],
            conversation_id=7)
        gw = _FakeGateway([understand(final_request="what number?"),
                           verdict("complete")])
        rt = _FakeRouter(["42."])
        pipe = ChatPipeline(gw, rt, max_tokens=500)
        pipe.run("what number?", history=ctx)
        understand_prompt = gw.calls[0][1]["content"]
        self.assertIn("remember 42", understand_prompt)
        provider_messages = rt.requests[0].messages
        contents = [m["content"] for m in provider_messages
                   if isinstance(m.get("content"), str)]
        self.assertIn("remember 42", contents)


# ── end-to-end over real HTTP: /api/chat wires everything together ────────
class EndToEndConversationMemoryTests(unittest.TestCase):
    """Uses a lightweight spy agent (like test_chat_history.py's
    slow_handle) so these tests exercise the REAL route handler in
    astra/web.py — including ChatLog persistence and
    ConversationContextBuilder — without needing live AI credentials."""

    def setUp(self):
        self.stack = make_stack()
        agent = self.stack["agent"]
        self.calls = []          # every (message, history-as-list) the agent saw

        def spy_handle(message, context="", history=None, attachments=None):
            hist = list(getattr(history, "messages", history) or [])
            self.calls.append({"message": message, "history": hist})
            n = len(self.calls)
            return {"reply": f"reply #{n} to: {message}", "action": "none",
                    "ok": True, "data": {}}
        agent.handle = spy_handle
        self.srv = LiveServer(stack=self.stack, agent=agent)

    def tearDown(self):
        self.srv.stop()

    def _post(self, path, body):
        req = urllib.request.Request(
            f"{self.srv.base}{path}", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _get(self, path):
        with urllib.request.urlopen(f"{self.srv.base}{path}", timeout=10) as r:
            return json.loads(r.read())

    def test_three_plus_turn_conversation_accumulates_history(self):
        self._post("/api/chat", {"message": "my name is Alex"})
        self._post("/api/chat", {"message": "what's 2+2?"})
        self._post("/api/chat", {"message": "why?"})

        # turn 1: nothing came before it
        self.assertEqual(self.calls[0]["history"], [])
        # turn 2: sees turn 1's user+assistant messages
        h2 = [(m["role"], m["content"]) for m in self.calls[1]["history"]]
        self.assertEqual(h2, [("user", "my name is Alex"),
                              ("assistant", "reply #1 to: my name is Alex")])
        # turn 3 ("why?"): sees both prior turns, in order, and NOT itself
        h3 = [(m["role"], m["content"]) for m in self.calls[2]["history"]]
        self.assertEqual(h3, [("user", "my name is Alex"),
                              ("assistant", "reply #1 to: my name is Alex"),
                              ("user", "what's 2+2?"),
                              ("assistant", "reply #2 to: what's 2+2?")])
        self.assertNotIn(("user", "why?"), h3)

    def test_follow_up_why_has_previous_context_available(self):
        self._post("/api/chat", {"message": "explain bitcoin halving"})
        self._post("/api/chat", {"message": "why?"})
        history_for_followup = self.calls[-1]["history"]
        self.assertTrue(any(m["content"] == "explain bitcoin halving"
                            for m in history_for_followup))

    def test_conversation_isolation_over_http(self):
        self._post("/api/chat", {"message": "secret in chat A"})
        chat_a = self._get("/api/chat/history")["data"]["conversation_id"]

        chat_b = self._call_post_conversations()
        self.assertNotEqual(chat_a, chat_b)

        self._post("/api/chat", {"message": "unrelated in chat B"})
        # turn in B must not see A's message
        b_history = self.calls[-1]["history"]
        self.assertFalse(any("secret in chat A" in m["content"] for m in b_history))

        # switching back to A and sending again must see ONLY A's history
        self._switch(chat_a)
        self._post("/api/chat", {"message": "follow-up in chat A"})
        a_history = self.calls[-1]["history"]
        self.assertTrue(any("secret in chat A" in m["content"] for m in a_history))
        self.assertFalse(any("unrelated in chat B" in m["content"] for m in a_history))

    def _call_post_conversations(self):
        req = urllib.request.Request(f"{self.srv.base}/api/chat/conversations",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["data"]["id"]

    def _switch(self, cid):
        req = urllib.request.Request(
            f"{self.srv.base}/api/chat/conversations/{cid}", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read())

    def test_current_message_never_duplicated_in_its_own_history(self):
        self._post("/api/chat", {"message": "turn one"})
        self._post("/api/chat", {"message": "turn two"})
        h = self.calls[-1]["history"]
        contents = [m["content"] for m in h]
        self.assertEqual(contents.count("turn two"), 0)

    def test_assistant_response_is_persisted_and_seen_on_next_turn(self):
        r1 = self._post("/api/chat", {"message": "hello"})
        self.assertTrue(r1["ok"])
        cid = r1["data"]["conversation_id"]

        stored = self._get(f"/api/chat/history?conversation_id={cid}")["data"]["messages"]
        self.assertEqual([(m["role"], m["text"]) for m in stored],
                         [("user", "hello"), ("ai", "reply #1 to: hello")])

        self._post("/api/chat", {"message": "and now?"})
        h2 = self.calls[-1]["history"]
        self.assertTrue(any(m["role"] == "assistant" and
                            m["content"] == "reply #1 to: hello" for m in h2))


class EndToEndTrimmingTests(unittest.TestCase):
    """Same spy-agent setup, but with a tiny context budget so trimming is
    exercised over the real HTTP path."""

    def setUp(self):
        self.stack = make_stack()
        agent = self.stack["agent"]
        self.calls = []

        def spy_handle(message, context="", history=None, attachments=None):
            hist = list(getattr(history, "messages", history) or [])
            self.calls.append({"message": message, "history": hist})
            n = len(self.calls)
            return {"reply": f"a{n}", "ok": True, "action": "none", "data": {}}
        agent.handle = spy_handle
        self.srv = LiveServer(stack=self.stack, agent=agent)
        # tiny budget: only a turn or two can survive
        self.srv.site.context_builder.max_chars = 60
        self.srv.site.context_builder.max_turns = 20

    def tearDown(self):
        self.srv.stop()

    def _post(self, body):
        req = urllib.request.Request(
            f"{self.srv.base}/api/chat", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_context_trimming_keeps_only_newest_turns_under_budget(self):
        for i in range(6):
            self._post({"message": f"message number {i} with some padding text"})
        last_history = self.calls[-1]["history"]
        # the immediately preceding turn must have survived...
        self.assertTrue(any("message number 4" in m["content"] for m in last_history))
        # ...but the budget is tiny, so far-older turns must be gone
        self.assertFalse(any("message number 0" in m["content"] for m in last_history))


class RetryDoesNotDuplicateHistoryHttpTests(unittest.TestCase):
    """A page refresh / connection retry mid-turn must not create a second
    user row nor a second (duplicated) entry in later history."""

    def setUp(self):
        self.stack = make_stack()
        agent = self.stack["agent"]
        self.release = threading.Event()
        self.calls = []

        def slow_spy_handle(message, context="", history=None, attachments=None):
            self.calls.append(message)
            self.release.wait(5)
            return {"reply": f"echo: {message}", "action": "none",
                    "ok": True, "data": {}}
        agent.handle = slow_spy_handle
        self.srv = LiveServer(stack=self.stack, agent=agent)

    def tearDown(self):
        self.release.set()
        self.srv.stop()

    def _get(self, path):
        with urllib.request.urlopen(f"{self.srv.base}{path}", timeout=10) as r:
            return json.loads(r.read())

    def test_retry_while_pending_does_not_duplicate_the_user_message(self):
        import socket

        def fire():
            body = json.dumps({"message": "ping"}).encode()
            s = socket.create_connection(("127.0.0.1", self.srv.port))
            s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\n"
                      b"Content-Type: application/json\r\n" +
                      f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            time.sleep(0.3)
            s.close()

        fire()   # first "send" — page then refreshes / connection drops
        fire()   # retry: identical message, turn still pending

        h = self._get("/api/chat/history")["data"]
        user_msgs = [m["text"] for m in h["messages"] if m["role"] == "user"]
        self.assertEqual(user_msgs, ["ping"])   # not duplicated

        self.release.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            h = self._get("/api/chat/history")["data"]
            if not h["pending"]:
                break
            time.sleep(0.1)

        user_msgs = [m["text"] for m in h["messages"] if m["role"] == "user"]
        self.assertEqual(user_msgs, ["ping"])
        ai_msgs = [m["text"] for m in h["messages"] if m["role"] == "ai"]
        self.assertEqual(ai_msgs, ["echo: ping"])


if __name__ == "__main__":
    unittest.main()
