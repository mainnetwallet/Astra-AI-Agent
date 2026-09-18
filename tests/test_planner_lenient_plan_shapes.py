"""A weak/instruction-light model very often returns valid JSON that just
isn't wrapped in the exact {"steps":[...]} envelope the planner prompt asks
for: a bare {"answer": "..."} (or text/reply/response/message/content), a
single step dict with no list around it, or a bare list of steps with no
"steps" wrapper.

Before this fix, none of these had a "steps" field, so `parse_plan_json`
returned no steps — and because the raw text still starts with "{",
`_plain_text_reply` also refused to hand it back as prose. Both paths
rejected it, so the model's own usable answer was thrown away for the
generic "couldn't make a plan" fallback message. This must not happen:
these shapes are recognized as a plan/answer directly, with no correction
round-trip and no fallback message shown to the user.
"""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.router import AstraRouter
from astra.core.correction import MAX_CORRECTION_ATTEMPTS
from astra.core.planner import Planner, _fallback_text
from astra.store import Store
from tests.test_planner_task_completion_integration import SequencedProvider


def _planner(replies):
    store = Store(":memory:")
    provider = SequencedProvider("gemini", ["m"], replies)
    router = AstraRouter(providers=[provider],
                         gateway=AstraAIGateway(connections=[], store=store))
    return provider, Planner(router=router, tools=["answer"])


class TestNormalizePlanShapeUnit(unittest.TestCase):
    """Direct unit coverage of the shape-normalizer, no provider needed."""

    def test_bare_answer_key(self):
        p = Planner()
        steps = p.parse_plan_json({"answer": "Hello!"})
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool"], "answer")
        self.assertEqual(steps[0]["params"]["text"], "Hello!")

    def test_text_reply_response_message_content_synonyms(self):
        for key in ("text", "reply", "response", "message", "content"):
            with self.subTest(key=key):
                p = Planner()
                steps = p.parse_plan_json({key: "hi there"})
                self.assertEqual(steps[0]["params"]["text"], "hi there")

    def test_bare_single_step_no_list_wrapper(self):
        p = Planner(tools=["fetch_url"])
        steps = p.parse_plan_json(
            {"id": "s1", "tool": "fetch_url", "params": {"url": "x"},
             "description": "get it"})
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool"], "fetch_url")
        self.assertEqual(steps[0]["params"], {"url": "x"})

    def test_bare_step_list_no_steps_wrapper(self):
        p = Planner(tools=["fetch_url"])
        steps = p.parse_plan_json(
            [{"id": "s1", "tool": "fetch_url", "params": {"url": "x"},
             "description": "get it"}])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool"], "fetch_url")

    def test_steps_wrapper_still_takes_priority(self):
        """A dict that already has "steps" is never re-interpreted, even
        if it also happens to carry an "answer"-shaped key."""
        p = Planner()
        steps = p.parse_plan_json(
            {"answer": "ignored", "steps": [
                {"id": "s1", "tool": "answer", "params": {"text": "real"},
                 "description": "d"}]})
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["params"]["text"], "real")

    def test_unrelated_shape_is_left_alone(self):
        """{"plan": [...]} matches none of the tolerated shapes and must
        still fall through to the existing required-field/correction
        path (empty steps here, not silently misread)."""
        p = Planner()
        steps = p.parse_plan_json({"plan": []})
        self.assertEqual(steps, [])

    def test_empty_answer_string_is_not_treated_as_usable(self):
        p = Planner()
        steps = p.parse_plan_json({"answer": "   "})
        self.assertEqual(steps, [])


class TestLenientShapesEndToEnd(unittest.TestCase):
    """Same shapes, but through the full Planner.plan() -> provider round
    trip. The Gateway's own contract still asks for a "steps" field, so a
    model that keeps returning one of these tolerated shapes still gets
    the full, bounded correction round-trip on every one of these calls
    (that part is unchanged, and IS the real cost a weak/slow model adds —
    see the module docstring's "weak model" note); what changes is what
    happens once that loop is exhausted: the model's own last reply is
    now recognized and used, never the generic fallback message."""

    def test_bare_answer_json_survives_exhausted_correction(self):
        provider, planner = _planner(
            ['{"answer":"Hello!"}'] * (MAX_CORRECTION_ATTEMPTS + 1))
        steps = planner.plan("Hi")
        self.assertEqual(len(provider.calls), MAX_CORRECTION_ATTEMPTS + 1)
        self.assertEqual(steps[0]["params"]["text"], "Hello!")
        self.assertNotEqual(steps[0]["params"]["text"],
                            _fallback_text("provider_no_plan"))

    def test_bare_single_step_survives_exhausted_correction(self):
        provider, planner = _planner(
            ['{"id":"s1","tool":"answer","params":{"text":"yo"},'
             '"description":"d"}'] * (MAX_CORRECTION_ATTEMPTS + 1))
        steps = planner.plan("hey")
        self.assertEqual(len(provider.calls), MAX_CORRECTION_ATTEMPTS + 1)
        self.assertEqual(steps[0]["params"]["text"], "yo")

    def test_bare_step_list_survives_exhausted_correction(self):
        provider, planner = _planner(
            ['[{"id":"s1","tool":"answer","params":{"text":"listed"},'
             '"description":"d"}]'] * (MAX_CORRECTION_ATTEMPTS + 1))
        steps = planner.plan("hey")
        self.assertEqual(len(provider.calls), MAX_CORRECTION_ATTEMPTS + 1)
        self.assertEqual(steps[0]["params"]["text"], "listed")

    def test_valid_plan_on_a_later_correction_still_short_circuits(self):
        """If a correction attempt DOES come back with a real "steps"
        plan, that's still preferred outright — the tolerant shapes above
        are a safety net for when it never does, not a reason to stop
        trying for the real thing."""
        provider, planner = _planner(
            ['{"answer":"Hello!"}',
             '{"steps":[{"id":"s1","tool":"answer","params":{"text":"real plan"},'
             '"description":"d"}]}'])
        steps = planner.plan("Hi")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(steps[0]["params"]["text"], "real plan")

    def test_still_falls_back_for_a_genuinely_unrelated_shape(self):
        """{"foo": 1} has none of the tolerated shapes, so it still goes
        through the existing correction loop and, once exhausted, the
        graceful fallback — never garbage shown to the user."""
        provider, planner = _planner(
            ['{"foo":1}'] * (MAX_CORRECTION_ATTEMPTS + 1))
        steps = planner.plan("do something")
        self.assertEqual(steps[0]["params"]["text"],
                         _fallback_text("provider_no_plan"))


if __name__ == "__main__":
    unittest.main()
