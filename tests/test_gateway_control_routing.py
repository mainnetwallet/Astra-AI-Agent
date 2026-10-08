"""Gateway control calls (chat-pipeline understand + verify).

They must reply with strict JSON and sit on the critical path of every chat
turn, so target selection for category "control" hard-requires a JSON-capable
model and ranks by measured latency (not by "fast" label alone, and not by
whichever model happened to serve the last request).
"""
import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_routing import (CATEGORY_HARD_CAPS, REQUEST_CATEGORIES,
                                      GatewayModelHealth, GatewayRoutingState,
                                      eligible_targets, meets_gateway_requirements,
                                      rank_targets, score_target)
from astra.ai.models import Model
from tests.test_logs_api_call_events import _Conn


def M(provider, mid, caps, quality="fast"):
    return Model(provider, mid, capabilities=caps, quality_class=quality,
                 supports_json="json" in caps)


def H(provider, mid, avg_ms=0.0, ok=0):
    return GatewayModelHealth(provider, mid, success_count=ok,
                              average_latency_ms=avg_ms)


class TestControlCategory(unittest.TestCase):
    def test_control_is_a_known_category_that_requires_json(self):
        self.assertIn("control", REQUEST_CATEGORIES)
        self.assertEqual(CATEGORY_HARD_CAPS["control"], ("json",))

    def test_chat_only_model_is_never_eligible_for_a_control_call(self):
        chat_only = M("openrouter", "cohere/north-mini-code:free", ["chat"])
        jsonish = M("mistral", "mistral-small", ["chat", "tools", "json"])
        self.assertFalse(meets_gateway_requirements(chat_only, category="control"))
        self.assertTrue(meets_gateway_requirements(jsonish, category="control"))
        # ...but it is still fine for an ordinary "general" call
        self.assertTrue(meets_gateway_requirements(chat_only, category="general"))

    def test_lower_measured_latency_wins_even_over_a_higher_quality_model(self):
        fast = (object(), M("cerebras", "fast-json", ["chat", "json"], "fast"),
                H("cerebras", "fast-json", avg_ms=800, ok=3))
        slow = (object(), M("cloudflare", "big-json", ["chat", "json"], "high"),
                H("cloudflare", "big-json", avg_ms=5000, ok=3))
        ranked = rank_targets([slow, fast], category="control")
        self.assertEqual(ranked[0][1].model_id, "fast-json")

    def test_slow_models_are_no_longer_tied_at_the_latency_floor(self):
        # under the ordinary latency term, 2.5s and 6s both score 0 for latency
        m = M("p", "m", ["chat", "json"], "mid")
        a = score_target(m, H("p", "m", avg_ms=2500, ok=1), category="control")
        b = score_target(m, H("p", "m", avg_ms=6000, ok=1), category="control")
        self.assertGreater(a, b)

    def test_unmeasured_fast_json_model_is_tried_before_unmeasured_big_one(self):
        fast = (object(), M("a", "fast", ["chat", "json"], "fast"), H("a", "fast"))
        big = (object(), M("b", "big", ["chat", "json"], "high"), H("b", "big"))
        self.assertEqual(rank_targets([big, fast], category="control")[0][1].model_id,
                         "fast")

    def test_other_categories_are_scored_exactly_as_before(self):
        m = M("p", "m", ["chat", "json"], "fast")
        h = H("p", "m", avg_ms=1500, ok=1)
        # ordinary latency window: max(0, 2 - 1.5) = 0.5
        with_general = score_target(m, h, category="general")
        expected = 3.0 + 2.0 + 1.5 * h.success_rate() + 0.5 + 0.2
        self.assertAlmostEqual(with_general, round(expected, 4), places=3)


class TestGatewaySelection(unittest.TestCase):
    def _gw(self, *conns):
        return AstraAIGateway(connections=list(conns), events=None)

    def test_control_call_skips_last_successful_stickiness(self):
        gw = self._gw(_Conn("astra-gw-groq", ["a", "b"]))
        fast = M("groq", "a", ["chat", "json"], "fast")
        slow = M("groq", "b", ["chat", "json"], "high")
        conn = gw.connections[0]
        gw._catalog = [(conn, slow), (conn, fast)]
        gw.routing_state.record_success("groq", "b", 6000.0)   # last success = slow
        gw.routing_state.record_success("groq", "a", 400.0)
        gw.routing_state.record_success("groq", "b", 6000.0)   # slow again, last
        _cat, ranked = gw._select_order(
            [{"role": "user", "content": "x"}], None, "control")
        self.assertEqual(ranked[0][1].model_id, "a")
        # while an ordinary call still sticks to the last successful target
        _cat, ranked = gw._select_order(
            [{"role": "user", "content": "x"}], None, "general")
        self.assertEqual(ranked[0][1].model_id, "b")

    def test_control_falls_back_to_general_when_no_model_declares_json(self):
        gw = self._gw(_Conn("astra-gw-groq", ["a"]))
        conn = gw.connections[0]
        gw._catalog = [(conn, M("groq", "a", ["chat"], "fast"))]
        cat, ranked = gw._select_order(
            [{"role": "user", "content": "x"}], None, "control")
        self.assertEqual(cat, "general")
        self.assertEqual([m.model_id for _c, m, _h in ranked], ["a"])
        self.assertEqual(gw.last_category, "general")

    def test_control_chat_uses_only_json_capable_targets(self):
        gw = self._gw(_Conn("astra-gw-groq", ["chat-only", "json-one"]))
        conn = gw.connections[0]
        gw._catalog = [(conn, M("groq", "chat-only", ["chat"], "fast")),
                       (conn, M("groq", "json-one", ["chat", "json"], "mid"))]
        out = gw.chat([{"role": "user", "content": "x"}], category="control")
        self.assertEqual(out, "ok-astra-gw-groq-json-one")
        self.assertEqual(gw.last_category, "control")


if __name__ == "__main__":
    unittest.main()
