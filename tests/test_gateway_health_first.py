"""Gateway's own calls: Gemini first (best health first), then every other
provider ordered purely by measured health -- not by provider/list order."""
import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_routing import GatewayRoutingState
from tests.test_logs_api_call_events import _Conn

MSG = [{"role": "user", "content": "hello there, how are you doing today my friend?"}]


def _gw(*conns):
    return AstraAIGateway(connections=list(conns), events=None)


def _order(gw, keep_last=False):
    # Seeding health via record_success also sets the sticky last_success;
    # the health-order tests want the pure fallback order, so drop it.
    if not keep_last:
        gw.routing_state.clear_last_successful()
    _cat, ranked = gw._select_order(MSG, None)
    return [(m.provider, m.model_id) for _c, m, _h in ranked]


class TestHealthFirstOrder(unittest.TestCase):
    def test_primary_gemini_models_come_first_best_health_first(self):
        gw = _gw(_Conn("astra-gw-groq", ["llama-3.1-8b"]),
                 _Conn("astra-gw-gemini", ["gemini-2.0-flash", "gemini-2.0-flash-lite"]))
        gw.routing_state.record_success("groq", "llama-3.1-8b", 50.0)
        gw.routing_state.record_success("gemini", "gemini-2.0-flash", 900.0)
        gw.routing_state.record_success("gemini", "gemini-2.0-flash-lite", 300.0)
        order = _order(gw)
        self.assertEqual(order[0], ("gemini", "gemini-2.0-flash-lite"))
        self.assertEqual(order[1], ("gemini", "gemini-2.0-flash"))
        self.assertEqual(order[2][0], "groq")

    def test_non_primary_providers_ordered_only_by_health_not_list_position(self):
        gw = _gw(_Conn("astra-gw-gemini", ["gemini-2.0-flash"]),
                 _Conn("astra-gw-groq", ["g-model"]),          # listed first...
                 _Conn("astra-gw-mistral", ["m-model"]),
                 _Conn("astra-gw-cerebras", ["c-model"]))       # ...listed last
        gw.routing_state.record_success("groq", "g-model", 100.0)
        gw.routing_state.record_failure("groq", "g-model", cooldown_s=0.0)  # 50% ok
        gw.routing_state.record_success("mistral", "m-model", 800.0)
        gw.routing_state.record_success("cerebras", "c-model", 200.0)
        order = _order(gw)
        self.assertEqual([p for p, _m in order],
                         ["gemini", "cerebras", "mistral", "groq"])

    def test_last_successful_is_sticky_and_beats_gemini(self):
        gw = _gw(_Conn("astra-gw-groq", ["fast", "slow"]),
                 _Conn("astra-gw-gemini", ["gemini-2.0-flash"]))
        gw.routing_state.record_success("groq", "fast", 100.0)
        gw.routing_state.record_success("gemini", "gemini-2.0-flash", 100.0)
        gw.routing_state.record_success("groq", "slow", 5000.0)   # last success
        order = _order(gw, keep_last=True)
        self.assertEqual(order[0], ("groq", "slow"))
        # the rest is the normal fallback order: Gemini first, then health
        self.assertEqual(order[1], ("gemini", "gemini-2.0-flash"))
        self.assertEqual(order[2], ("groq", "fast"))

    def test_sticky_target_used_alone_on_next_call_no_gemini(self):
        gem = _Conn("astra-gw-gemini", ["gemini-2.0-flash"])
        groq = _Conn("astra-gw-groq", ["llama-3.1-8b"])
        gw = _gw(groq, gem)
        gw.routing_state.record_success("groq", "llama-3.1-8b", 100.0)
        gw.chat(MSG)
        self.assertEqual(gw.last_connection, "astra-gw-groq")
        self.assertEqual(gw.last_model, "llama-3.1-8b")

    def test_sticky_failure_clears_and_retries_gemini_first(self):
        gem = _Conn("astra-gw-gemini", ["gemini-2.0-flash"])
        groq = _Conn("astra-gw-groq", ["llama-3.1-8b"])
        other = _Conn("astra-gw-mistral", ["m-model"])
        gw = _gw(groq, other, gem)
        gw.routing_state.record_success("groq", "llama-3.1-8b", 100.0)
        groq.fail = True
        gw.chat(MSG)
        # sticky target failed -> Gemini (primary) is tried next and wins
        self.assertEqual(gw.last_connection, "astra-gw-gemini")
        # ...and Gemini becomes the NEW last_success
        last = gw.routing_state.last_successful()
        self.assertEqual((last["provider"], last["model"]),
                         ("gemini", "gemini-2.0-flash"))

    def test_failure_clears_last_success(self):
        rs = GatewayRoutingState(None)
        rs.record_success("groq", "a", 10.0)
        rs.record_failure("groq", "b")             # different target: keep
        self.assertIsNotNone(rs.last_successful())
        rs.record_failure("groq", "a")             # the sticky one: clear
        self.assertIsNone(rs.last_successful())

    def test_health_probe_does_not_set_last_success(self):
        rs = GatewayRoutingState(None)
        rs.record_success("groq", "a", 10.0, mark_last=False)
        self.assertIsNone(rs.last_successful())

    def test_chat_falls_through_gemini_then_next_best_health(self):
        gem = _Conn("astra-gw-gemini", ["gemini-2.0-flash"])
        gem.fail = True
        ok = _Conn("astra-gw-groq", ["llama-3.1-8b"])
        gw = _gw(ok, gem)
        gw.chat(MSG)
        self.assertEqual(gw.last_connection, "astra-gw-groq")
        # the failure is recorded so gemini's health worsens for next time
        self.assertEqual(
            gw.routing_state.get_health("gemini", "gemini-2.0-flash").failure_count, 1)

    def test_health_persists_across_restart(self):
        from astra.store import Store
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        gw1 = AstraAIGateway(connections=[_Conn("astra-gw-groq", ["a"])],
                             store=Store(path))
        gw1.routing_state.record_success("groq", "a", 123.0)
        gw2 = AstraAIGateway(connections=[_Conn("astra-gw-groq", ["a"])],
                             store=Store(path))
        self.assertEqual(gw2.routing_state.get_health("groq", "a").success_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestPrimaryMaxThree(unittest.TestCase):
    def test_only_three_best_gemini_models_before_other_providers(self):
        gem = _Conn("astra-gw-gemini", ["g1", "g2", "g3", "g4", "g5"])
        gw = _gw(_Conn("astra-gw-groq", ["llama"]), gem)
        for m, ms in (("g1", 100.0), ("g2", 200.0), ("g3", 300.0),
                      ("g4", 400.0), ("g5", 500.0)):
            gw.routing_state.record_success("gemini", m, ms)
        gw.routing_state.record_success("groq", "llama", 50.0)
        order = _order(gw)
        self.assertEqual(order[:3], [("gemini", "g1"), ("gemini", "g2"),
                                     ("gemini", "g3")])
        # 4th slot is the next-best HEALTH model overall (groq, 50ms),
        # not a 4th Gemini model.
        self.assertEqual(order[3], ("groq", "llama"))
        self.assertEqual(order[4], ("gemini", "g4"))

    def test_fewer_than_three_gemini_models_all_come_first(self):
        gw = _gw(_Conn("astra-gw-groq", ["llama"]),
                 _Conn("astra-gw-gemini", ["g1", "g2"]))
        gw.routing_state.record_success("groq", "llama", 10.0)
        gw.routing_state.record_success("gemini", "g1", 900.0)
        gw.routing_state.record_success("gemini", "g2", 800.0)
        order = _order(gw)
        self.assertEqual([p for p, _ in order[:2]], ["gemini", "gemini"])
        self.assertEqual(order[2], ("groq", "llama"))

    def test_config_can_change_the_cap(self):
        gw = _gw(_Conn("astra-gw-groq", ["llama"]),
                 _Conn("astra-gw-gemini", ["g1", "g2", "g3"]))
        gw.primary_max_models = 1
        for m in ("g1", "g2", "g3"):
            gw.routing_state.record_success("gemini", m, 500.0)
        gw.routing_state.record_success("groq", "llama", 50.0)
        order = _order(gw)
        self.assertEqual(order[0][0], "gemini")
        self.assertEqual(order[1], ("groq", "llama"))
