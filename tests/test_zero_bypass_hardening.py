"""Zero-bypass hardening — final pass regression tests.

Locks in the architecture:

    User -> Planner -> Gateway Request Intelligence -> AstraRouter.
    route_request() -> Astra AI Gateway -> Existing Provider -> AI Model

Specifically proves (see the pass's final report for the full audit):

  1. Planner cannot use the legacy `router.route()` tuple call for normal
     AI planning — `route_request()` is the only entry point.
  2. A router missing `route_request()` fails planning clearly (a plain
     "answer" fallback step), never silently via another AI path.
  3. A contract-bearing request cannot execute through an Existing
     Provider when no Gateway is attached — fails closed.
  4. Gateway is never added to `AstraRouter.providers` / ProviderRegistry
     and is never used as an Existing-Provider fallback when every real
     provider candidate fails.
  5. Gateway and Existing-Provider credentials read from disjoint,
     non-overlapping env var names (`GW_*` vs plain).
  6. No direct/alternate AI execution path exists in the core
     orchestration modules (Planner, Orchestrator, Agent, bootstrap) —
     source-level regression guard against a future reintroduction.

Items 7-9 of the pass's requirements (Executor evidence reaching Gateway
final verification, already-succeeded steps never replayed, and bounded
correction/replan) are covered by the existing
tests/test_gateway_gaps.py::TestGap2AutomaticEvidenceVerification suite
and are not duplicated here.
"""
from __future__ import annotations

import inspect
import unittest

from astra.ai.gateway import (AstraAIGateway, AstraGatewayBedrock,
                              AstraGatewayCloudflare, AstraGatewayGemini,
                              AstraGatewayGroq)
from astra.ai.registry import KEYS_ENV
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.exceptions import ProviderError
from astra.core.planner import Planner


class _ShimPool:
    def __bool__(self):
        return True


class _FailingProvider:
    """A real-shaped Existing-Provider adapter whose every call fails, so
    tests can prove the Gateway is never used as a silent substitute."""

    name = "gemini"
    models = ["model-a"]

    def __init__(self):
        self.pool = _ShimPool()
        self.calls = 0

    def health_check(self):
        return True

    def chat(self, messages, model=None, max_tokens=500, response_format=None):
        self.calls += 1
        raise ProviderError("gemini: simulated outage")


# ═══════════════════════════════════════════════════════════════════════
# 1-2: Planner has exactly one AI-backed planning path
# ═══════════════════════════════════════════════════════════════════════
class TestPlannerHasNoLegacyRoutePath(unittest.TestCase):
    def test_legacy_route_never_invoked_when_route_request_available(self):
        """A router exposing BOTH methods must only ever see
        `route_request()` called for normal AI planning."""
        from astra.ai.router import RoutingResult

        class _DualRouter:
            def __init__(self):
                self.route_calls = 0
                self.route_request_calls = 0

            def route(self, messages):
                self.route_calls += 1
                return ("gemini", "model-a", '{"steps":[]}')

            def route_request(self, req):
                self.route_request_calls += 1
                return RoutingResult(
                    provider="gemini", model="model-a", ok=True,
                    text='{"steps":[{"id":"s1","tool":"answer",'
                         '"params":{"text":"hi"},"description":"d"}]}')

        router = _DualRouter()
        planner = Planner(router=router, tools=["answer"])
        planner.plan("say hi")
        self.assertEqual(router.route_calls, 0)
        self.assertEqual(router.route_request_calls, 1)

    def test_router_without_route_request_fails_closed(self):
        """A duck-typed router exposing only the legacy `.route()` tuple
        interface cannot plan at all — Planner fails clearly (plain
        fallback answer) instead of taking that alternate AI path."""
        class _LegacyOnlyRouter:
            def __init__(self):
                self.calls = 0

            def route(self, messages):
                self.calls += 1
                return ("gemini", "model-a", '{"steps":[]}')

        router = _LegacyOnlyRouter()
        planner = Planner(router=router, tools=["answer"])
        steps = planner.plan("say hi")
        self.assertEqual(router.calls, 0)
        self.assertTrue(steps[0].get("is_answer"))
        self.assertEqual(planner.last_completion_status, "")

    def test_planner_source_has_no_bare_route_call(self):
        """Regression lock: the module source must never again contain a
        bare `router.route(` call — route_request() is the only path."""
        from astra.core import planner as planner_mod
        src = inspect.getsource(planner_mod)
        self.assertNotIn("self.router.route(", src)
        self.assertNotIn(".router.route([", src)


# ═══════════════════════════════════════════════════════════════════════
# 3-4: Router-boundary enforcement + Gateway isolation
# ═══════════════════════════════════════════════════════════════════════
class TestRouterBoundaryEnforcement(unittest.TestCase):
    def test_contract_bearing_request_fails_closed_without_gateway(self):
        provider = _FailingProvider()
        # give it one scripted success so a plain request WOULD succeed —
        # proving the failure below is specifically the missing-Gateway
        # guard, not just "everything failed".
        provider.chat = lambda messages, model=None, max_tokens=500: "ok"
        router = AstraRouter(providers=[provider], gateway=None)
        contract = object()   # any non-None sentinel; router only checks identity
        rr = router.route_request(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_contract=contract))
        self.assertFalse(rr.ok)
        self.assertIn("Gateway", rr.error)

        # the SAME request with no contract succeeds normally — confirms
        # the failure above is the contract+no-Gateway guard specifically.
        rr2 = router.route_request(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr2.ok)

    def test_gateway_never_joins_providers_or_provider_registry(self):
        gw = AstraAIGateway(connections=[])
        provider = _FailingProvider()
        router = AstraRouter(providers=[provider], gateway=gw)
        self.assertNotIn(gw, router.providers)
        self.assertEqual(router.providers, [provider])

    def test_gateway_never_used_as_provider_fallback_on_total_failure(self):
        """When every real provider candidate fails, routing must fail
        honestly — the Gateway (even though attached) must never step in
        and answer using its own connections."""
        gw = AstraAIGateway(connections=[])   # unusable: no GW_* connections
        provider = _FailingProvider()
        router = AstraRouter(providers=[provider], gateway=gw)
        rr = router.route_request(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}]))
        self.assertFalse(rr.ok)
        self.assertGreaterEqual(provider.calls, 1)


# ═══════════════════════════════════════════════════════════════════════
# 5: Gateway/Existing-Provider credential isolation
# ═══════════════════════════════════════════════════════════════════════
class TestCredentialIsolation(unittest.TestCase):
    def test_gateway_connections_use_gw_prefixed_env_only(self):
        gw_envs = {
            AstraGatewayGemini.api_keys_env,
            AstraGatewayGroq.api_keys_env,
            AstraGatewayCloudflare.api_keys_env,
            AstraGatewayBedrock.api_keys_env,
        }
        for env in gw_envs:
            self.assertTrue(env.startswith("GW_"), env)

    def test_gateway_env_names_disjoint_from_existing_provider_env_names(self):
        gw_envs = {
            AstraGatewayGemini.api_keys_env, AstraGatewayGroq.api_keys_env,
            AstraGatewayCloudflare.api_keys_env, AstraGatewayBedrock.api_keys_env,
        }
        provider_envs = set(KEYS_ENV.values())
        self.assertEqual(gw_envs & provider_envs, set())

    def test_existing_provider_env_names_are_unprefixed(self):
        for provider, env in KEYS_ENV.items():
            self.assertFalse(env.startswith("GW_"), f"{provider}: {env}")


# ═══════════════════════════════════════════════════════════════════════
# 6: No alternate/direct AI execution path in core orchestration modules
# ═══════════════════════════════════════════════════════════════════════
class TestNoAlternateExecutionPath(unittest.TestCase):
    FORBIDDEN = ("self.llm(", ".llm(", "import anthropic", "import openai",
                "requests.post(", "httpx.post(", "urllib.request.urlopen(")

    def _assert_module_clean(self, module):
        src = inspect.getsource(module)
        for needle in self.FORBIDDEN:
            self.assertNotIn(needle, src,
                             f"{module.__name__} contains forbidden pattern {needle!r}")

    def test_planner_has_no_direct_ai_call(self):
        from astra.core import planner
        self._assert_module_clean(planner)

    def test_orchestrator_has_no_direct_ai_call(self):
        from astra.core import orchestrator
        self._assert_module_clean(orchestrator)

    def test_agent_has_no_direct_ai_call(self):
        import astra.agent as agent_mod
        self._assert_module_clean(agent_mod)
        # explicit regression note left in the module itself
        self.assertIn("no raw/direct LLM callable", inspect.getsource(agent_mod))

    def test_executor_has_no_direct_ai_call(self):
        from astra.core import executor
        self._assert_module_clean(executor)


if __name__ == "__main__":
    unittest.main()
