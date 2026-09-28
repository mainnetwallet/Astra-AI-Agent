"""Gateway's own calls: Gemini first (best health first), then every other
provider ordered purely by measured health -- not by provider/list order."""
import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_routing import GatewayRoutingState
from tests.test_logs_api_call_events import _Conn

MSG = [{"role": "user", "content": "hello there, how are you doing today my friend?"}]


def _gw(*conns):
    return AstraAIGateway(connections=list(conns), events=None)


def _order(gw):
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

    def test_last_successful_does_not_jump_the_queue(self):
        gw = _gw(_Conn("astra-gw-groq", ["fast", "slow"]))
        gw.routing_state.record_success("groq", "fast", 100.0)
        gw.routing_state.record_success("groq", "slow", 5000.0)   # last success
        self.assertEqual(_order(gw)[0], ("groq", "fast"))

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
