"""Gateway-OWNED result supervision (§6-§12) — the piece that was missing.

`tests/test_gateway_execution_recovery.py` and `test_gateway_runtime_wiring.py`
prove the Gateway decides WHO to execute against (target selection,
cooldown, failover) and that the decision actually reaches the Existing
Provider. Neither ever proved the Gateway looks at WHAT came back: nothing
called `select_execution_target`'s sibling for *result* validation, nothing
ever built or sent a correction, and `ProviderExecutionPort` had no
implementation anywhere in the repo.

These tests inspect the ACTUAL calls a fake Provider adapter receives
(`adapter.calls`, a list of the exact `messages` each real `.chat()`
invocation was made with) — not just Gateway-side return values — so a
correction round-trip that only exists in a mock would fail these tests.
"""
from __future__ import annotations

import ast
import json
import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_contract import (ProviderExecutionPort,
                                       ProviderExecutionResult,
                                       ProviderExecutionTarget)
from astra.ai.gateway_supervision import (GatewayResultSupervision,
                                          build_correction_messages,
                                          validate_execution_result)
from astra.ai.router import AstraRouter, RoutingRequest, _RouterExecutionPort
from astra.core.correction import MAX_CORRECTION_ATTEMPTS
from astra.core.exceptions import ProviderError
from astra.store import Store


def _t(provider_id, model_id):
    return ProviderExecutionTarget(provider_id=provider_id, model_id=model_id)


class _ShimPool:
    def __bool__(self):
        return True


class SequencedProvider:
    """Fake Provider adapter whose `.chat()` returns a different reply each
    call (a queue), and records the exact `messages` list it was really
    invoked with each time — so a test can assert the correction turn
    (§11) actually reached the real adapter, not just Gateway state."""

    def __init__(self, name, models, replies: list[str]):
        self.name = name
        self.models = models
        self.pool = _ShimPool()
        self.calls: list[list] = []   # each element = the messages list
        self._replies = list(replies)

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500):
        self.calls.append(list(messages))
        if not self._replies:
            raise ProviderError(f"{self.name}: no more scripted replies")
        return self._replies.pop(0)


class TestValidateExecutionResult(unittest.TestCase):
    def test_provider_failure_is_invalid(self):
        outcome = validate_execution_result(
            ProviderExecutionResult(ok=False, error="boom"))
        self.assertEqual(outcome.status, "invalid")
        self.assertEqual(outcome.reason, "boom")

    def test_empty_text_is_invalid(self):
        outcome = validate_execution_result(ProviderExecutionResult(ok=True, text="   "))
        self.assertFalse(outcome.ok)

    def test_plain_text_valid_by_default(self):
        outcome = validate_execution_result(ProviderExecutionResult(ok=True, text="hi there"))
        self.assertTrue(outcome.ok)

    def test_non_json_rejected_when_json_required(self):
        outcome = validate_execution_result(
            ProviderExecutionResult(ok=True, text="not json"), require_json=True)
        self.assertEqual(outcome.status, "invalid")

    def test_missing_required_field_is_partial(self):
        outcome = validate_execution_result(
            ProviderExecutionResult(ok=True, text=json.dumps({"a": 1})),
            require_json=True, required_fields=("a", "b"))
        self.assertEqual(outcome.status, "partial")
        self.assertIn("b", outcome.missing)

    def test_all_required_fields_present_is_valid(self):
        outcome = validate_execution_result(
            ProviderExecutionResult(ok=True, text=json.dumps({"a": 1, "b": 2})),
            require_json=True, required_fields=("a", "b"))
        self.assertTrue(outcome.ok)


class TestBuildCorrectionMessages(unittest.TestCase):
    def test_appends_assistant_then_correction_user_turn(self):
        base = [{"role": "user", "content": "give me json"}]
        outcome = validate_execution_result(
            ProviderExecutionResult(ok=True, text=json.dumps({"a": 1})),
            require_json=True, required_fields=("a", "b"))
        result = ProviderExecutionResult(ok=True, text=json.dumps({"a": 1}))
        corrected = build_correction_messages(base, result, outcome)
        self.assertEqual(len(corrected), 3)
        self.assertEqual(corrected[0], base[0])
        self.assertEqual(corrected[1]["role"], "assistant")
        self.assertEqual(corrected[2]["role"], "user")
        self.assertIn("b", corrected[2]["content"])
        # original list is never mutated in place
        self.assertEqual(len(base), 1)


class _RecordingPort(ProviderExecutionPort):
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def execute(self, target, messages, max_tokens=500, **kwargs):
        self.calls.append((target.key(), list(messages)))
        if not self.replies:
            raise ProviderError("no more replies")
        return self.replies.pop(0)


class TestGatewayResultSupervisionUnit(unittest.TestCase):
    """Proves the loop itself: validate -> correct -> revalidate, bounded,
    same target every time, via a minimal fake port (isolated from routing)."""

    def test_valid_first_time_never_calls_port(self):
        sup = GatewayResultSupervision()
        port = _RecordingPort([])
        target = _t("gemini", "m1")
        result = ProviderExecutionResult(ok=True, text="fine")
        final, outcome = sup.supervise(port, target, [{"role": "user", "content": "hi"}],
                                       result, require_json=False)
        self.assertTrue(outcome.ok)
        self.assertEqual(port.calls, [])   # never corrected — nothing to fix

    def test_invalid_json_triggers_one_correction_to_same_target(self):
        sup = GatewayResultSupervision()
        port = _RecordingPort([json.dumps({"a": 1, "b": 2})])
        target = _t("gemini", "m1")
        result = ProviderExecutionResult(ok=True, text="not json")
        final, outcome = sup.supervise(
            port, target, [{"role": "user", "content": "give json"}], result,
            require_json=True, required_fields=("a", "b"))
        self.assertTrue(outcome.ok)
        self.assertEqual(json.loads(final.text), {"a": 1, "b": 2})
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(port.calls[0][0], ("gemini", "m1"))     # SAME target
        # the correction turn reached the port's messages
        self.assertIn("Missing / Invalid", port.calls[0][1][-1]["content"])

    def test_bounded_gives_up_after_max_attempts(self):
        sup = GatewayResultSupervision()
        # always returns invalid non-JSON, forever
        port = _RecordingPort(["still not json"] * 10)
        target = _t("gemini", "m1")
        result = ProviderExecutionResult(ok=True, text="not json")
        final, outcome = sup.supervise(
            port, target, [{"role": "user", "content": "give json"}], result,
            require_json=True)
        self.assertFalse(outcome.ok)
        self.assertEqual(len(port.calls), MAX_CORRECTION_ATTEMPTS)

    def test_port_exception_stops_loop_without_raising(self):
        sup = GatewayResultSupervision()

        class _RaisingPort(ProviderExecutionPort):
            def execute(self, target, messages, max_tokens=500, **kwargs):
                raise ProviderError("target went down mid-correction")

        target = _t("gemini", "m1")
        result = ProviderExecutionResult(ok=True, text="not json")
        final, outcome = sup.supervise(_RaisingPort(), target, [{"role": "user", "content": "x"}],
                                       result, require_json=True)
        self.assertFalse(outcome.ok)
        self.assertFalse(final.ok)


class TestGatewayOwnsSupervisionEndToEndThroughRealRouter(unittest.TestCase):
    """THE proof the task asked for: a real AstraRouter + a real
    AstraAIGateway + a fake Provider adapter (never a mocked Gateway
    method). The Gateway must receive a ProviderExecutionResult, validate
    it, build a correction, send it back through `_RouterExecutionPort` —
    which calls `AstraRouter._attempt` -> `adapter.chat()` again, the exact
    same path a normal (non-corrected) request takes — and validate the
    corrected reply, all inside one `router.route_request()` call.
    """

    def test_valid_json_reply_is_not_corrected(self):
        # "gemini-2.0" matches the "gemini" model family (models.py), which
        # already declares "json" among its capabilities — needed because
        # RoutingDecisionPolicy hard-filters candidates on req.structured_output
        # requiring json support (astra/ai/routing_policy.py).
        reply = json.dumps({"name": "Astra"})
        provider = SequencedProvider("gemini", ["gemini-2.0"], [reply])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)

        req = RoutingRequest(
            messages=[{"role": "user", "content": "describe yourself as json"}],
            structured_output=True)
        rr = router.route_request(req)

        self.assertTrue(rr.ok)
        # already valid JSON -> supervision runs its check but never needs
        # to correct: the Existing Provider's real .chat() is invoked
        # exactly ONCE.
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(json.loads(rr.text), {"name": "Astra"})

    def test_invalid_json_reply_is_corrected_via_real_provider_call(self):
        provider = SequencedProvider("gemini", ["gemini-2.0"],
                                     ["not valid json", '{"name": "Astra"}'])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)

        req = RoutingRequest(
            messages=[{"role": "user", "content": "describe yourself as json"}],
            structured_output=True)
        rr = router.route_request(req)

        self.assertTrue(rr.ok)
        self.assertEqual(json.loads(rr.text), {"name": "Astra"})
        # the real adapter was invoked TWICE: the original (invalid) call,
        # then the Gateway-driven correction — both through the real
        # _attempt()/adapter.chat() path, never a mock.
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("Missing / Invalid", provider.calls[1][-1]["content"])

    def test_empty_reply_from_healthy_target_is_corrected_not_failed_over(self):
        """§9: healthy provider, incomplete (empty) result -> correction,
        NOT provider/model failover. Same (provider, model) both times."""
        provider = SequencedProvider("gemini", ["model-a"], ["", "a real answer"])
        other = SequencedProvider("groq", ["model-b"], ["should never be called"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider, other], gateway=gw)

        req = RoutingRequest(
            messages=[{"role": "user", "content": "hello"}],
            preferred_provider="gemini", preferred_model="model-a")
        rr = router.route_request(req)

        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "gemini")
        self.assertEqual(rr.model, "model-a")
        self.assertEqual(rr.text, "a real answer")
        # the REAL adapter received exactly two .chat() calls: the original
        # empty one, then the correction — both against gemini/model-a.
        self.assertEqual(len(provider.calls), 2)
        # the second call's messages carry the correction turn built by
        # gateway_supervision.build_correction_messages (assistant echo +
        # a user turn naming what was missing) — proving the Gateway's
        # correction instruction is what the Existing Provider actually saw.
        second_call_messages = provider.calls[1]
        self.assertEqual(second_call_messages[-2]["role"], "assistant")
        self.assertEqual(second_call_messages[-1]["role"], "user")
        self.assertIn("Missing / Invalid", second_call_messages[-1]["content"])
        # the other provider was never touched — this was correction, not
        # provider-level failover.
        self.assertEqual(other.calls, [])

    def test_incomplete_json_with_required_fields_is_corrected_end_to_end(self):
        """Drives the exact acceptance scenario: Gateway receives a
        ProviderExecutionResult, validates (JSON missing a required field),
        builds+sends a correction through ProviderExecutionPort to the real
        adapter, and validates the corrected result again — using the
        Gateway's own public `supervise_execution` surface directly against
        a live `_RouterExecutionPort` bound to a real AstraRouter, exactly
        as `router._maybe_supervise_result` does internally."""
        first_reply = json.dumps({"name": "Astra"})
        second_reply = json.dumps({"name": "Astra", "version": 2})
        provider = SequencedProvider("gemini", ["model-a"], [first_reply, second_reply])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)
        req = RoutingRequest(messages=[{"role": "user", "content": "describe as json"}],
                             structured_output=True)
        target = ProviderExecutionTarget(provider_id="gemini", model_id="model-a")
        by_key = {target.key(): (0.0, provider, _Model("model-a"))}

        port = _RouterExecutionPort(router, by_key, req)
        initial_text = provider.chat(req.messages, model="model-a", max_tokens=req.max_tokens)
        result = ProviderExecutionResult(ok=True, text=initial_text)

        final_result, outcome = gw.supervise_execution(
            port, target, req.messages, result,
            max_tokens=req.max_tokens, require_json=True,
            required_fields=("name", "version"))

        self.assertTrue(outcome.ok)
        self.assertEqual(json.loads(final_result.text), {"name": "Astra", "version": 2})
        # provider.chat was called twice total: once above (initial), once
        # more inside the correction round-trip driven by the Gateway.
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("Missing / Invalid", provider.calls[1][-1]["content"])
        self.assertIn("version", provider.calls[1][-1]["content"])


class _Model:
    """Minimal stand-in with a `.model_id` attribute, only needed for the
    `by_key` lookup shape `_RouterExecutionPort.execute` expects."""
    def __init__(self, model_id):
        self.model_id = model_id


class TestFailOpenAndIsolation(unittest.TestCase):
    def test_gateway_without_supervise_execution_is_a_no_op(self):
        """A Gateway object that doesn't implement supervise_execution (e.g.
        an older/stub Gateway) must never break routing."""
        class _NoSupervisionGateway:
            def select_execution_target(self, candidates, **kw):
                return candidates[0] if candidates else None

            def report_execution_success(self, *a, **kw):
                pass

            def report_execution_failure(self, *a, **kw):
                pass

            def recover_execution_target(self, *a, **kw):
                return None

        provider = SequencedProvider("gemini", ["model-a"], ["hello"])
        router = AstraRouter(providers=[provider], gateway=_NoSupervisionGateway())
        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.text, "hello")

    def test_supervision_exception_never_breaks_a_successful_response(self):
        class _BrokenSupervisionGateway(AstraAIGateway):
            def supervise_execution(self, *a, **kw):
                raise RuntimeError("boom")

        provider = SequencedProvider("gemini", ["model-a"], [""])
        gw = _BrokenSupervisionGateway(connections=[])
        router = AstraRouter(providers=[provider], gateway=gw)
        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)   # empty text, but supervision blew up -> fail-open
        self.assertEqual(rr.text, "")

    def test_no_provider_system_imports_in_supervision_module(self):
        import astra.ai.gateway_supervision as mod
        forbidden = ("astra.ai.registry", "astra.ai.provider", "astra.ai.adapters")
        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    self.assertFalse(module.startswith(f))
            elif isinstance(node, ast.Name):
                self.assertNotEqual(node.id, "ProviderRegistry")


class _Bus:
    """Same minimal fixture as tests/test_logs_api_call_events.py."""

    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})

    def kinds(self, prefix):
        return [r for r in self.rows if r["kind"].startswith(prefix)]


class TestCorrectionRequestedShowsRejectedReply(unittest.TestCase):
    """Same Activity Log visibility fix as the Task Completion supervisor:
    `correction_requested` now carries `got`, the first 120 chars of the
    reply that was actually rejected, not just the abstract `reason`."""

    def test_got_is_the_rejected_replys_first_120_chars(self):
        bus = _Bus()
        sup = GatewayResultSupervision(events=bus)
        port = _RecordingPort(['{"a": 1, "b": 2}'])
        long_reply = "y" * 200
        sup.supervise(port, _t("gemini", "model-a"),
                      [{"role": "user", "content": "hi"}],
                      ProviderExecutionResult(ok=True, text=long_reply),
                      require_json=True, required_fields=("a", "b"))
        requested = bus.kinds("gateway.supervision.correction_requested")
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["data"]["got"], long_reply[:120])
        self.assertEqual(len(requested[0]["data"]["got"]), 120)


if __name__ == "__main__":
    unittest.main()
