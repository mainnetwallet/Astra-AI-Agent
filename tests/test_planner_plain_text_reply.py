"""A Provider that answers in prose instead of the JSON plan (typical for a
chatty model replying to "Hi") must still get its reply to the user — not the
generic "couldn't make a plan" fallback."""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.router import AstraRouter
from astra.core.executor import Executor
from astra.core.orchestrator import Orchestrator
from astra.core.planner import Planner, _fallback_text
from astra.store import Store
from tests.test_planner_task_completion_integration import SequencedProvider


def _stack(replies):
    store = Store(":memory:")
    provider = SequencedProvider("openrouter", ["m"], replies)
    router = AstraRouter(providers=[provider],
                         gateway=AstraAIGateway(connections=[], store=store))
    planner = Planner(router=router, tools=["answer"])
    return store, provider, router, planner


class TestPlainTextReply(unittest.TestCase):
    def test_prose_reply_becomes_the_answer(self):
        _, provider, _, planner = _stack(["Hello! How can I help you today?"] * 6)
        steps = planner.plan("Hi")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertEqual(steps[0]["params"]["text"], "Hello! How can I help you today?")
        self.assertNotEqual(steps[0]["params"]["text"],
                            _fallback_text("provider_no_plan"))
        self.assertEqual(planner.last_plan_failure_reason, "")

    def test_orchestrator_completes_with_the_provider_reply(self):
        store, provider, router, planner = _stack(["Hey there!"] * 6)
        orch = Orchestrator(store, planner=planner,
                            executor=Executor(registry=None), router=router)
        rec = orch.submit("Hi", sync=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["results"]["a1"]["output"]["text"], "Hey there!")

    def test_valid_json_plan_is_still_preferred(self):
        _, provider, _, planner = _stack(
            ['{"steps":[{"id":"s1","tool":"answer","params":{"text":"from plan"},'
             '"description":"reply"}]}'])
        steps = planner.plan("Hi")
        self.assertEqual(steps[0]["params"]["text"], "from plan")
        self.assertEqual(len(provider.calls), 1)   # no corrections needed

    def test_broken_json_is_never_shown_as_an_answer(self):
        _, _, _, planner = _stack(['{"steps": [{"id": "s1", "tool"'] * 6)
        steps = planner.plan("do something")
        self.assertEqual(steps[0]["params"]["text"], _fallback_text("provider_no_plan"))

    def test_empty_placeholder_and_think_only_replies_fall_back(self):
        for reply in ("(no reply)", "<think>hmm, a greeting</think>", "   "):
            with self.subTest(reply=reply):
                _, _, _, planner = _stack([reply] * 6)
                steps = planner.plan("Hi")
                self.assertEqual(steps[0]["params"]["text"],
                                 _fallback_text("provider_no_plan"))

    def test_think_block_is_stripped_from_the_reply(self):
        self.assertEqual(
            Planner._plain_text_reply("<think>plan</think>\n\nHello!"), "Hello!")


if __name__ == "__main__":
    unittest.main()
