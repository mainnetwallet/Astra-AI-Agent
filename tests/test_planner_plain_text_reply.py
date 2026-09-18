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

    def test_correction_scaffold_echo_is_never_shown_as_an_answer(self):
        """Reproduces a real Activity Log case (nemotron-3.5-lightning:free
        on "Hi"): a reasoning-heavy model burns its whole token budget on a
        "thinking process" and, on the exhausted-correction attempt, ends
        up quoting/musing about the correction instruction WE sent it
        ("Goal: Produce a valid ordered step plan...", "Required Next
        Action: ...") instead of ever answering "Hi". That ramble must
        never be handed to the user as if it were the Provider's reply."""
        ramble = (
            "Here's a thinking process:\n\n"
            "1.  Analyze User Input:\n"
            "   - User says: \"Goal: Produce a valid ordered step plan "
            "(JSON) for the user's goal, or an \\\"answer\\\" step if no "
            "tool fits.\"\n"
            "   - Then there's a \"Status\" block saying task is "
            "incomplete, missing JSON, etc.\n"
            "   - Then \"Required Next Action: disregard the previous "
            "attempt entirely and redo the task from scratch...\"\n")
        self.assertEqual(Planner._plain_text_reply(ramble), "")

    def test_exhausted_correction_scaffold_echo_falls_back_gracefully(self):
        """End-to-end: every scripted reply is the same scaffold-echoing
        ramble (never valid JSON, never a real answer) -> correction is
        exhausted -> the graceful fallback message is shown, never the
        raw ramble."""
        ramble = (
            "Here's a thinking process:\n\n"
            "1. Analyze User Input:\n"
            "   - Goal: Produce a valid ordered step plan (JSON) for the "
            "user's goal, or an \"answer\" step if no tool fits.\n"
            "   - Required Next Action: disregard the previous attempt "
            "entirely and redo the task from scratch.\n")
        _, _, _, planner = _stack([ramble] * 6)
        steps = planner.plan("Hi")
        self.assertEqual(steps[0]["params"]["text"],
                         _fallback_text("provider_no_plan"))
        self.assertNotIn("Required Next Action", steps[0]["params"]["text"])
        self.assertNotIn("thinking process", steps[0]["params"]["text"])


if __name__ == "__main__":
    unittest.main()
