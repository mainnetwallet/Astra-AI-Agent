"""Activity Log: every Gateway API call must be reported exactly once, with
the AI (provider) and model that was actually called — on the routed path AND
on the explicit-model path (which used to emit nothing)."""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway
from astra.core.exceptions import ProviderError


class _Bus:
    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})

    def kinds(self, prefix):
        return [r for r in self.rows if r["kind"].startswith(prefix)]


class _Pool:
    def __bool__(self):
        return True


class _Conn:
    base_url = ""

    def __init__(self, name, models, fail=False):
        self.name, self.models, self.fail = name, list(models), fail
        self.pool = _Pool()

    def chat(self, messages, model=None, max_tokens=500):
        if self.fail:
            raise ProviderError(f"{self.name}: boom")
        return f"ok-{self.name}-{model}"

    def stream(self, messages, model=None, max_tokens=500):
        if self.fail:
            raise ProviderError(f"{self.name}: boom")
        yield f"ok-{self.name}-{model}"

    def health_check(self):
        return True


MSG = [{"role": "user", "content": "hi"}]


class TestGatewayCallEvents(unittest.TestCase):
    def _gw(self, *conns):
        bus = _Bus()
        return AstraAIGateway(connections=list(conns), events=bus), bus

    def test_explicit_model_chat_reports_provider_and_model(self):
        gw, bus = self._gw(_Conn("astra-gw-gemini", ["g-model"]),
                           _Conn("astra-gw-groq", ["q-model"]))
        gw.chat(MSG, model="q-model")
        ok = bus.kinds("astra_gateway.success")
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["data"]["provider"], "groq")
        self.assertEqual(ok[0]["data"]["model"], "q-model")
        self.assertIn("latency_ms", ok[0]["data"])

    def test_explicit_model_failure_then_success_is_one_error_one_success(self):
        gw, bus = self._gw(_Conn("astra-gw-gemini", ["shared"], fail=True),
                           _Conn("astra-gw-groq", ["shared"]))
        gw.chat(MSG, model="shared")
        err = bus.kinds("astra_gateway.error")
        ok = bus.kinds("astra_gateway.success")
        self.assertEqual([e["data"]["provider"] for e in err], ["gemini"])
        self.assertEqual([e["data"]["provider"] for e in ok], ["groq"])

    def test_skipped_connections_are_not_reported_as_calls(self):
        gw, bus = self._gw(_Conn("astra-gw-gemini", ["other"]),
                           _Conn("astra-gw-groq", ["wanted"]))
        gw.chat(MSG, model="wanted")
        calls = bus.kinds("astra_gateway.success") + bus.kinds("astra_gateway.error")
        self.assertEqual(len(calls), 1)

    def test_explicit_model_stream_reports_success(self):
        gw, bus = self._gw(_Conn("astra-gw-groq", ["q-model"]))
        self.assertEqual(list(gw.stream(MSG, model="q-model")), ["ok-astra-gw-groq-q-model"])
        ok = bus.kinds("astra_gateway.success")
        self.assertEqual(len(ok), 1)
        self.assertEqual((ok[0]["data"]["provider"], ok[0]["data"]["model"]),
                         ("groq", "q-model"))

    def test_routed_chat_reports_exactly_one_terminal_event_per_call(self):
        gw, bus = self._gw(_Conn("astra-gw-groq", ["q-model"]))
        gw.chat(MSG)
        terminal = bus.kinds("astra_gateway.success") + bus.kinds("astra_gateway.error")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["data"]["model"], "q-model")


if __name__ == "__main__":
    unittest.main()


class TestProviderCallEvents(unittest.TestCase):
    """Provider side: each real provider/model attempt -> one terminal event
    carrying provider AND model; the 'all providers failed' summary is flagged
    so the Logs panel doesn't count it as another API call."""

    def test_each_attempt_has_provider_and_model_and_summary_is_flagged(self):
        from astra.ai.router import AstraRouter, RoutingRequest
        from tests.test_ai import FakeAIProvider
        bus = _Bus()
        r = AstraRouter([FakeAIProvider(name="broken", models=["a", "b"],
                                        fail_first=9999)], max_retries=0)
        r.attach_events(bus)
        rr = r.route_request(RoutingRequest(messages=MSG))
        self.assertFalse(rr.ok)
        failed = bus.kinds("ai.failed")
        per_call = [f for f in failed if not f["data"].get("aggregate")]
        summary = [f for f in failed if f["data"].get("aggregate")]
        self.assertEqual({(f["data"]["provider"], f["data"]["model"]) for f in per_call},
                         {("broken", "a"), ("broken", "b")})
        self.assertEqual(len(summary), 1)

    def test_success_event_has_provider_and_model(self):
        from astra.ai.router import AstraRouter, RoutingRequest
        from tests.test_ai import FakeAIProvider
        bus = _Bus()
        r = AstraRouter([FakeAIProvider(name="good", models=["m1"])], max_retries=0)
        r.attach_events(bus)
        self.assertTrue(r.route_request(RoutingRequest(messages=MSG)).ok)
        done = bus.kinds("ai.completed")
        self.assertEqual(len(done), 1)
        self.assertEqual((done[0]["data"]["provider"], done[0]["data"]["model"]),
                         ("good", "m1"))
