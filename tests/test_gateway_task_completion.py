"""Gateway Task Completion Contract, evidence/semantic verification,
correction and final result gate (§1-§12 of the "FINAL FIX" spec).

Mirrors the style of test_gateway_result_supervision.py: unit tests for
the pure functions, then real-adapter-call tests proving a correction
actually reaches `ProviderExecutionPort.execute()` and isn't just a mock.
"""
from __future__ import annotations

import ast
import json
import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_contract import (ProviderExecutionPort,
                                       ProviderExecutionResult,
                                       ProviderExecutionTarget)
from astra.ai.gateway_task_completion import (COMPLETE, FAILED, INCOMPLETE,
                                              UNCERTAIN,
                                              GatewayTaskCompletionSupervisor,
                                              build_task_completion_contract,
                                              build_task_correction_instruction,
                                              verify_task_completion)
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.correction import MAX_CORRECTION_ATTEMPTS
from astra.core.exceptions import ProviderError
from astra.store import Store


def _t(provider_id="gemini", model_id="m1"):
    return ProviderExecutionTarget(provider_id=provider_id, model_id=model_id)


def _r(text="fine", ok=True, error=""):
    return ProviderExecutionResult(ok=ok, text=text, error=error)


# ── §9: simple requests stay minimal ────────────────────────────────────────
class TestContractBuilding(unittest.TestCase):
    def test_bare_request_is_minimal_contract(self):
        contract = build_task_completion_contract("what is 2+2?")
        self.assertTrue(contract.is_minimal)

    def test_never_invents_requirements(self):
        contract = build_task_completion_contract("fix the bug")
        self.assertEqual(contract.required_actions, ())
        self.assertEqual(contract.evidence_required, ())

    def test_complex_request_keeps_exactly_what_was_supplied(self):
        contract = build_task_completion_contract(
            "fix login bug and run tests",
            goal="fix the login bug",
            required_actions=("modify auth code", "run tests"),
            evidence_required=("changed_files", "test_result"))
        self.assertFalse(contract.is_minimal)
        self.assertEqual(contract.evidence_required, ("changed_files", "test_result"))


# ── §2/§3: evidence-based + semantic verification ───────────────────────────
class TestVerifyTaskCompletion(unittest.TestCase):
    def test_simple_request_passes_without_evidence(self):
        """Test 1: simple request passes without unnecessary verification."""
        contract = build_task_completion_contract("hello")
        outcome = verify_task_completion(contract, _r("hi there"))
        self.assertEqual(outcome.status, COMPLETE)

    def test_complete_task_with_all_evidence_passes(self):
        """Test 2."""
        contract = build_task_completion_contract(
            "fix bug", evidence_required=("changed_files", "test_result"))
        evidence = {"changed_files": ["auth.py"], "test_result": "passed"}
        outcome = verify_task_completion(contract, _r("fixed it"), evidence)
        self.assertEqual(outcome.status, COMPLETE)

    def test_empty_result_rejected(self):
        """Test 3."""
        contract = build_task_completion_contract("hello")
        outcome = verify_task_completion(contract, _r("   "))
        self.assertEqual(outcome.status, INCOMPLETE)

    def test_invalid_structured_output_rejected(self):
        """Test 4."""
        contract = build_task_completion_contract("give json", require_json=True)
        outcome = verify_task_completion(contract, _r("not json"))
        self.assertEqual(outcome.status, INCOMPLETE)

    def test_missing_required_field_rejected(self):
        """Test 5."""
        contract = build_task_completion_contract(
            "give json", require_json=True, required_fields=("a", "b"))
        outcome = verify_task_completion(contract, _r(json.dumps({"a": 1})))
        self.assertEqual(outcome.status, INCOMPLETE)
        self.assertIn("b", outcome.missing)

    def test_claimed_done_without_evidence_is_rejected(self):
        """Test 6: "Done." with no matching evidence must not pass."""
        contract = build_task_completion_contract(
            "fix login bug and run tests",
            evidence_required=("changed_files", "test_result"))
        outcome = verify_task_completion(contract, _r("Done."), evidence=None)
        self.assertEqual(outcome.status, INCOMPLETE)
        self.assertIn("changed_files", outcome.missing)
        self.assertIn("test_result", outcome.missing)

    def test_provider_failure_is_failed_not_incomplete(self):
        contract = build_task_completion_contract("hello")
        outcome = verify_task_completion(contract, _r(ok=False, error="boom"))
        self.assertEqual(outcome.status, FAILED)
        self.assertFalse(outcome.correctable)

    def test_semantic_verifier_only_consulted_after_deterministic_passes(self):
        contract = build_task_completion_contract(
            "explain relativity", require_semantic=True)
        calls = []

        def verifier(contract, result, evidence):
            calls.append(result.text)
            return (COMPLETE, "looks right")

        outcome = verify_task_completion(contract, _r("   "), semantic_verifier=verifier)
        self.assertEqual(outcome.status, INCOMPLETE)
        self.assertEqual(calls, [])   # never reached — empty text failed first

        outcome2 = verify_task_completion(
            contract, _r("a real explanation"), semantic_verifier=verifier)
        self.assertEqual(outcome2.status, COMPLETE)
        self.assertEqual(calls, ["a real explanation"])

    def test_semantic_verifier_uncertain_does_not_pass(self):
        contract = build_task_completion_contract("summarize this", require_semantic=True)

        def verifier(contract, result, evidence):
            return (UNCERTAIN, "can't tell if it's a faithful summary")

        outcome = verify_task_completion(contract, _r("some summary"), semantic_verifier=verifier)
        self.assertEqual(outcome.status, UNCERTAIN)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.correctable)

    def test_semantic_verifier_exception_is_uncertain_not_complete(self):
        contract = build_task_completion_contract("summarize this", require_semantic=True)

        def broken_verifier(contract, result, evidence):
            raise RuntimeError("model unavailable")

        outcome = verify_task_completion(contract, _r("some summary"), semantic_verifier=broken_verifier)
        self.assertEqual(outcome.status, UNCERTAIN)
        self.assertFalse(outcome.ok)

    def test_evidence_already_present_treated_as_complete_no_duplicate_work(self):
        """Test 16: if evidence shows the side effect already happened,
        verification passes without asking for it again."""
        contract = build_task_completion_contract(
            "send the transaction", evidence_required=("tx_hash",))
        outcome = verify_task_completion(
            contract, _r("already sent"), evidence={"tx_hash": "0xabc"})
        self.assertEqual(outcome.status, COMPLETE)


class TestCorrectionInstruction(unittest.TestCase):
    def test_instruction_names_missing_and_next_action(self):
        contract = build_task_completion_contract(
            "fix login bug and run tests", goal="fix the login bug",
            required_actions=("modify auth code", "run tests"),
            evidence_required=("changed_files", "test_result"))
        outcome = verify_task_completion(contract, _r("Done."))
        text = build_task_correction_instruction(contract, _r("Done."), outcome)
        self.assertIn("fix the login bug", text)
        self.assertIn("changed_files", text)
        self.assertIn("Required Next Action", text)
        self.assertNotIn("Try again", text)


# ── §5-§8: the bounded verify -> correct -> re-verify -> gate loop ─────────
class _RecordingPort(ProviderExecutionPort):
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def execute(self, target, messages, max_tokens=500, **kwargs):
        self.calls.append((target.key(), list(messages)))
        if not self.replies:
            raise ProviderError("no more replies")
        return self.replies.pop(0)


class TestGatewayTaskCompletionSupervisorUnit(unittest.TestCase):
    def test_complete_first_time_never_calls_port(self):
        """Test 1/2 combined: valid result short-circuits, no correction."""
        sup = GatewayTaskCompletionSupervisor()
        port = _RecordingPort([])
        contract = build_task_completion_contract("hello")
        final, outcome, attempts = sup.supervise(
            port, _t(), [{"role": "user", "content": "hi"}], _r("hi there"), contract)
        self.assertEqual(outcome.status, COMPLETE)
        self.assertEqual(attempts, 0)
        self.assertEqual(port.calls, [])

    def test_incomplete_task_generates_correction_and_reaches_port(self):
        """Tests 7/8: correction instruction actually reaches the port,
        every attempt, up to the bound (evidence is never supplied here,
        so it stays incomplete both times — proving the request really
        did round-trip to the real port and not just once)."""
        sup = GatewayTaskCompletionSupervisor()
        port = _RecordingPort(["Done, first pass", "Done, second pass"])
        contract = build_task_completion_contract(
            "fix login bug and run tests",
            evidence_required=("changed_files", "test_result"))
        result = _r("I looked at the bug.")
        final, outcome, attempts = sup.supervise(
            port, _t(), [{"role": "user", "content": "fix it"}], result, contract,
            evidence=None)
        self.assertEqual(attempts, MAX_CORRECTION_ATTEMPTS)
        self.assertEqual(len(port.calls), MAX_CORRECTION_ATTEMPTS)
        self.assertIn("changed_files", port.calls[0][1][-1]["content"])
        self.assertIn("Required Next Action", port.calls[0][1][-1]["content"])
        self.assertFalse(outcome.ok)

    def test_provider_fixes_task_and_gateway_revalidates(self):
        """Tests 9/10/11: provider fixes the (deterministic) shape problem
        on the FIRST correction, Gateway re-validates, and the corrected
        result is what the caller gets back — only after re-validation."""
        sup = GatewayTaskCompletionSupervisor()
        fixed_reply = json.dumps({"fix": "patched auth.py", "tests": "passed"})
        port = _RecordingPort([fixed_reply])
        contract = build_task_completion_contract(
            "fix bug and report as json", require_json=True,
            required_fields=("fix", "tests"))
        final, outcome, attempts = sup.supervise(
            port, _t(), [{"role": "user", "content": "fix it, respond as json"}],
            _r("not json"), contract)
        self.assertTrue(outcome.ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(json.loads(final.text), {"fix": "patched auth.py", "tests": "passed"})
        self.assertEqual(len(port.calls), 1)

    def test_bounded_gives_up_after_max_attempts(self):
        """Test 12."""
        sup = GatewayTaskCompletionSupervisor()
        port = _RecordingPort(["still no evidence"] * 10)
        contract = build_task_completion_contract(
            "fix bug", evidence_required=("test_result",))
        final, outcome, attempts = sup.supervise(
            port, _t(), [{"role": "user", "content": "fix it"}],
            _r("working on it"), contract)
        self.assertFalse(outcome.ok)
        self.assertEqual(attempts, MAX_CORRECTION_ATTEMPTS)
        self.assertEqual(len(port.calls), MAX_CORRECTION_ATTEMPTS)

    def test_port_exception_during_correction_stops_as_failed(self):
        """Tests 13/14/15 (partial): a genuine execution failure mid-
        correction must not be treated as more correction — it stops
        immediately with FAILED so the caller's existing recovery/
        failover path (astra.ai.gateway_recovery) can take over."""
        sup = GatewayTaskCompletionSupervisor()
        port = _RecordingPort([])   # raises on first .execute()
        contract = build_task_completion_contract(
            "fix bug", evidence_required=("test_result",))
        final, outcome, attempts = sup.supervise(
            port, _t(), [{"role": "user", "content": "fix it"}],
            _r("working on it"), contract)
        self.assertEqual(outcome.status, FAILED)
        self.assertEqual(attempts, 1)
        self.assertFalse(final.ok)

    def test_failed_task_produces_explicit_status_never_silently_complete(self):
        """Test 17."""
        sup = GatewayTaskCompletionSupervisor()
        port = _RecordingPort(["still incomplete"] * 5)
        contract = build_task_completion_contract(
            "fix bug", evidence_required=("test_result",))
        final, outcome, attempts = sup.supervise(
            port, _t(), [{"role": "user", "content": "fix it"}],
            _r("working on it"), contract)
        self.assertIn(outcome.status, (INCOMPLETE, UNCERTAIN, FAILED))
        self.assertNotEqual(outcome.status, COMPLETE)


# ── real Router + real Gateway + fake Provider adapter, end-to-end ─────────
class SequencedProvider:
    def __init__(self, name, models, replies):
        self.name = name
        self.models = models
        self.pool = _ShimPool()
        self.calls = []
        self._replies = list(replies)

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls.append(list(messages))
        if not self._replies:
            raise ProviderError(f"{self.name}: no more scripted replies")
        return self._replies.pop(0)


class _ShimPool:
    def __bool__(self):
        return True


class TestRouterEndToEnd(unittest.TestCase):
    """Test 20: real AstraRouter + real AstraAIGateway + a fake (but real
    call-path) Provider adapter, driven through RoutingRequest.task_contract."""

    def test_complete_task_end_to_end_no_correction(self):
        provider = SequencedProvider("gemini", ["model-a"], ["all done, files changed"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)
        contract = build_task_completion_contract("simple task")
        req = RoutingRequest(messages=[{"role": "user", "content": "do the simple task"}],
                             task_contract=contract)
        rr = router.route_request(req)
        self.assertTrue(rr.ok)
        self.assertEqual(rr.completion_status, COMPLETE)
        self.assertEqual(len(provider.calls), 1)   # no correction needed

    def test_incomplete_task_end_to_end_triggers_real_correction(self):
        provider = SequencedProvider(
            "gemini", ["model-a"],
            ["Done.", "Fixed auth.py, tests still pending",
             "Fixed auth.py, tests passed"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)
        contract = build_task_completion_contract(
            "fix login bug and run tests",
            evidence_required=("changed_files", "test_result"))
        req = RoutingRequest(
            messages=[{"role": "user", "content": "fix login bug and run tests"}],
            task_contract=contract)
        rr = router.route_request(req)
        self.assertTrue(rr.ok)
        # evidence was never supplied -> stays INCOMPLETE even after both
        # real correction round-trips (bounded at MAX_CORRECTION_ATTEMPTS);
        # proves the loop actually asked the REAL provider adapter for a
        # fix each time (1 initial + 2 corrections = 3 total calls).
        self.assertEqual(rr.completion_status, INCOMPLETE)
        self.assertEqual(len(provider.calls), 1 + MAX_CORRECTION_ATTEMPTS)
        self.assertIn("changed_files", provider.calls[1][-1]["content"])

    def test_evidence_satisfied_end_to_end_reports_complete(self):
        provider = SequencedProvider("gemini", ["model-a"], ["Done."])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)
        contract = build_task_completion_contract(
            "run the smoke test", evidence_required=("test_result",))
        req = RoutingRequest(
            messages=[{"role": "user", "content": "run the smoke test"}],
            task_contract=contract, evidence={"test_result": "passed"})
        rr = router.route_request(req)
        self.assertTrue(rr.ok)
        self.assertEqual(rr.completion_status, COMPLETE)
        self.assertEqual(len(provider.calls), 1)


class TestFailOpenAndIsolation(unittest.TestCase):
    def test_no_provider_system_imports_in_task_completion_module(self):
        """Test 18: Gateway task-completion code stays isolated from the
        Existing Provider system's internals."""
        import astra.ai.gateway_task_completion as mod
        forbidden = ("astra.ai.registry", "astra.ai.provider", "astra.ai.adapters")
        with open(mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    self.assertFalse(module.startswith(f))
            elif isinstance(node, ast.Name):
                self.assertNotEqual(node.id, "ProviderRegistry")

    def test_router_without_task_contract_is_unaffected(self):
        """Regression safety: RoutingRequest.task_contract defaults to
        None, so a caller who never sets it gets exactly today's
        behavior (plain supervise_execution path, no completion_status)."""
        provider = SequencedProvider("gemini", ["model-a"], ["hello"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[provider], gateway=gw)
        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.text, "hello")
        self.assertEqual(rr.completion_status, "")

    def test_gateway_supervise_task_exception_never_breaks_response(self):
        class _BrokenGateway(AstraAIGateway):
            def supervise_task(self, *a, **kw):
                raise RuntimeError("boom")

        provider = SequencedProvider("gemini", ["model-a"], ["hello"])
        gw = _BrokenGateway(connections=[])
        router = AstraRouter(providers=[provider], gateway=gw)
        contract = build_task_completion_contract("hi")
        rr = router.route_request(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_contract=contract))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.text, "hello")   # fail-open: original text kept


# ── Activity Log visibility: what did the model actually say? ───────────────
class _Bus:
    """Same minimal fixture as tests/test_logs_api_call_events.py."""

    def __init__(self):
        self.rows = []

    def emit(self, kind, agent="", **data):
        self.rows.append({"kind": kind, "agent": agent, "data": data})

    def kinds(self, prefix):
        return [r for r in self.rows if r["kind"].startswith(prefix)]


class TestCorrectionRequestedShowsRejectedReply(unittest.TestCase):
    """The "Reply rejected, asking again" log line used to give only the
    reason ("required field(s) missing from JSON response") with no way
    to see what the model actually sent. `correction_requested` now also
    carries `got`: the first 120 chars of the rejected reply."""

    def test_got_is_the_rejected_replys_first_120_chars(self):
        bus = _Bus()
        sup = GatewayTaskCompletionSupervisor(events=bus)
        port = _RecordingPort(["still not json"] * MAX_CORRECTION_ATTEMPTS)
        contract = build_task_completion_contract(
            "hi", require_json=True, required_fields=("steps",))
        long_reply = "x" * 200
        sup.supervise(port, _t(), [{"role": "user", "content": "hi"}],
                      _r(long_reply), contract)
        requested = bus.kinds("gateway.task_completion.correction_requested")
        self.assertEqual(len(requested), MAX_CORRECTION_ATTEMPTS)
        self.assertEqual(requested[0]["data"]["got"], long_reply[:120])
        self.assertEqual(len(requested[0]["data"]["got"]), 120)

    def test_got_reflects_each_attempts_own_rejected_reply(self):
        bus = _Bus()
        sup = GatewayTaskCompletionSupervisor(events=bus)
        port = _RecordingPort(['{"nope":1}', '{"also_nope":1}'])
        contract = build_task_completion_contract(
            "hi", require_json=True, required_fields=("steps",))
        sup.supervise(port, _t(), [{"role": "user", "content": "hi"}],
                      _r('{"foo":1}'), contract)
        requested = bus.kinds("gateway.task_completion.correction_requested")
        self.assertEqual([r["data"]["got"] for r in requested],
                         ['{"foo":1}', '{"nope":1}'])


if __name__ == "__main__":
    unittest.main()
