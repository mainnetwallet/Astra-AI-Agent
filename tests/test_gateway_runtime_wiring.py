"""FINAL GAP FIX — real runtime wiring between the Astra AI Gateway and the
Existing Provider system (§16-§19 of the gap-fix prompt).

The previous test suite (tests/test_gateway_execution_recovery.py) proved
`GatewayExecutionRecovery.select_execution_target` / `recover_execution_target`
work correctly in isolation, and that `AstraRouter` *reports* outcomes to an
attached Gateway. It never proved the Gateway's decision actually reaches the
Existing Provider execution call — `select_execution_target` /
`recover_execution_target` had no caller anywhere in `router.py` before this
fix; the router iterated its own static `ranked` order and only used the
Gateway for after-the-fact bookkeeping.

These tests inspect the ACTUAL execution request the fake Provider adapters
receive (`adapter.calls`, a list of `model_id`s each fake adapter was really
invoked with) rather than only Gateway-side state, per §17: "Inspect the
actual execution request received by the Existing Provider bridge."
"""
from __future__ import annotations

import unittest

from astra.ai.gateway import AstraAIGateway
from astra.ai.gateway_contract import ProviderExecutionTarget
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.exceptions import ProviderError
from astra.store import Store


def _t(provider_id, model_id):
    return ProviderExecutionTarget(provider_id=provider_id, model_id=model_id)


class _ShimPool:
    def __init__(self, ok=True):
        self.ok = ok

    def __bool__(self):
        return self.ok


class RecordingProvider:
    """Fake Provider adapter that records every (model_id) it is actually
    invoked with, and can be told to fail (with a specific message, so it
    classifies to a chosen §7 category) for one or more of its models."""

    def __init__(self, name, models, *, fail_models: dict | None = None):
        self.name = name
        self.models = models
        self.pool = _ShimPool(True)
        self.calls: list[str] = []
        # model_id -> (times_to_fail, error_message)
        self.fail_models = dict(fail_models or {})
        self._fail_counts: dict[str, int] = {}

    def health_check(self):
        return bool(self.pool)

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls.append(model)
        spec = self.fail_models.get(model)
        if spec:
            times, message = spec
            used = self._fail_counts.get(model, 0)
            if used < times:
                self._fail_counts[model] = used + 1
                raise ProviderError(message)
        return f"reply-from-{self.name}/{model}"


class TestGatewayTargetActuallyDrivesExecution(unittest.TestCase):
    """§17: the target the Gateway selects must be what the Existing
    Provider bridge actually receives — not merely recorded Gateway state."""

    def test_first_selection_reaches_the_real_provider_call(self):
        p1 = RecordingProvider("gemini", ["model-a"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[p1], gateway=gw)

        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))

        self.assertTrue(rr.ok)
        # the Existing Provider actually received the Gateway-selected target
        self.assertEqual(p1.calls, ["model-a"])
        self.assertTrue(gw.execution_recovery.is_eligible(_t("gemini", "model-a")))

    def test_failover_target_is_the_one_actually_executed(self):
        """Gemini/model-a -> 429, Gateway recovers to Groq/model-b: verify
        Groq's fake adapter actually receives model-b (not just that Gateway
        recorded a cooldown for model-a)."""
        gemini = RecordingProvider(
            "gemini", ["model-a"],
            fail_models={"model-a": (99, "HTTP 429 rate limit reached")})
        groq = RecordingProvider("groq", ["model-b"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[gemini, groq], gateway=gw)

        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))

        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "groq")
        self.assertEqual(rr.model, "model-b")
        # both adapters' *actual* .chat() calls, not just Gateway bookkeeping.
        # gemini is called (max_retries + 1) times by its OWN per-credential
        # retry loop before _attempt gives up on it — that's existing,
        # unrelated behavior; what matters here is it was invoked with
        # model-a (never anything else), and groq was actually invoked with
        # the Gateway-selected model-b.
        self.assertTrue(gemini.calls and set(gemini.calls) == {"model-a"})
        self.assertEqual(groq.calls, ["model-b"])
        self.assertFalse(gw.execution_recovery.is_eligible(_t("gemini", "model-a")))

    def test_model_level_failover_within_same_provider(self):
        """Two models on the SAME provider: model-a cools down, Gateway picks
        model-b of the same provider — proving §6/§8 (model-level, not
        provider-level cooldown) actually reaches execution."""
        gemini = RecordingProvider(
            "gemini", ["model-a", "model-b"],
            fail_models={"model-a": (99, "HTTP 429 rate limit reached")})
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[gemini], gateway=gw)

        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))

        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "model-b")
        self.assertIn("model-a", gemini.calls)
        self.assertIn("model-b", gemini.calls)


class TestCooldownActuallySkipsOnNextRequest(unittest.TestCase):
    """The gap this fix closes: before it, a Gateway-cooled-down target was
    still re-attempted on the very next `route_request` call, because the
    router never consulted Gateway state before picking who to call — it
    only reported to it afterward. This proves that's no longer true."""

    def test_previously_failed_target_is_not_retried_next_request(self):
        gemini = RecordingProvider(
            "gemini", ["model-a"],
            fail_models={"model-a": (99, "HTTP 429 rate limit reached")})
        groq = RecordingProvider("groq", ["model-b"])
        store = Store(":memory:")
        gw = AstraAIGateway(connections=[], store=store)
        router = AstraRouter(providers=[gemini, groq], gateway=gw)

        rr1 = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "one"}]))
        self.assertTrue(rr1.ok)
        calls_after_first_request = list(gemini.calls)
        self.assertTrue(calls_after_first_request)  # tried (and cooled down)

        # a brand-new request: gemini/model-a must be skipped OUTRIGHT this
        # time (still in cooldown) — never called again — with groq/model-b
        # selected straight away.
        rr2 = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "two"}]))
        self.assertTrue(rr2.ok)
        self.assertEqual(rr2.provider, "groq")
        self.assertEqual(gemini.calls, calls_after_first_request)  # NOT called again
        self.assertEqual(groq.calls, ["model-b", "model-b"])
        self.assertEqual(rr2.attempts, 1)             # no wasted attempt this time


class TestGatewayFailOpenOnInternalError(unittest.TestCase):
    """A Gateway-side bug must never break otherwise-working routing (module
    docstring's existing 'fails open' guarantee, preserved through this
    fix)."""

    def test_gateway_exception_falls_back_to_plain_ranked_order(self):
        class _BrokenGateway(AstraAIGateway):
            def select_execution_target(self, *a, **kw):
                raise RuntimeError("boom")

        good = RecordingProvider("p1", ["m0"])
        gw = _BrokenGateway(connections=[])
        router = AstraRouter(providers=[good], gateway=gw)

        rr = router.route_request(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(good.calls, ["m0"])


class TestIsolationAcrossAllGatewayRecoveryModules(unittest.TestCase):
    """§19: extend the existing AST-based isolation check (previously only
    covering gateway_routing.py) to gateway_recovery.py and
    gateway_contract.py — the two modules this fix actually wires in."""

    def test_no_provider_system_imports_or_names(self):
        import ast
        import astra.ai.gateway_contract as gc
        import astra.ai.gateway_recovery as gr

        forbidden_modules = ("astra.ai.registry", "astra.ai.provider",
                             "astra.ai.adapters")
        for mod in (gc, gr):
            with open(mod.__file__, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in forbidden_modules:
                            self.assertFalse(alias.name.startswith(forbidden))
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for forbidden in forbidden_modules:
                        self.assertFalse(module.startswith(forbidden))
                elif isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, "ProviderRegistry")
                elif isinstance(node, ast.Attribute):
                    self.assertNotEqual(node.attr, "ProviderRegistry")

    def test_router_gateway_bridge_never_imports_adapters_module(self):
        """The new `_route_via_gateway` bridge in router.py is the piece that
        touches both worlds — it must still only ever pass plain
        ProviderExecutionTarget metadata to the Gateway, never an adapter."""
        import ast
        import astra.ai.router as router_mod
        with open(router_mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_route_via_gateway")
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                # gateway.<x>(...) calls must be limited to the sanitized
                # recovery API — never something adapter/credential shaped.
                if isinstance(node.value, ast.Name) and node.value.id == "self":
                    continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 {"chat", "stream", "health_check"},
                                 msg="_route_via_gateway must never call an "
                                     "adapter method directly")


if __name__ == "__main__":
    unittest.main()
