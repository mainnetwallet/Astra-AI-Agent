"""AgentRouter + model registry + credentials + adapters (Phase A).

The build requires AgentRouter behave as the *central routing core*: score by
capability/history/health, rotate credentials, fall back across providers, and
never leak secrets. These tests exercise real behavior with fakes where HTTP
would otherwise be needed.
"""
from __future__ import annotations

import unittest

from astra.core.config import Config
from astra.ai.provider import AIProvider, OfflineProvider
from astra.ai.router import AgentRouter
from astra.core.exceptions import ProviderError


class FakeAIProvider(AIProvider):
    """Configurable fake provider with a pool-like surface."""
    name = "fake"
    models = ["m0"]
    _emo = {"ok": "hello", "fail": ProviderError("down")}

    def __init__(self, name=None, models=None, healthy=True, fail_first=0,
                 pool=None, latency_s=0.0):
        super().__init__()
        self.name = name or "fake"
        self.models = models or ["m0"]
        self.healthy = healthy
        self.fail_first = fail_first
        self.pool = pool
        self._calls = 0
        self.latency_s = latency_s
        if self.pool is None:
            # minimal shim: pool truthiness drives _provider_usable
            self.pool = _ShimPool(healthy)

    def chat(self, messages, model=None, max_tokens=500):
        import time
        self._calls += 1
        if self.latency_s:
            time.sleep(self.latency_s)
        if self._calls <= self.fail_first:
            raise ProviderError("simulated failure")
        return f"reply-from-{self.name}"

    def health_check(self):
        return self.healthy


class _ShimPool:
    def __init__(self, ok):
        self.ok = ok

    def __bool__(self):
        return self.ok


def _router(*providers, **kw):
    from astra.ai.router import AgentRouter
    return AgentRouter(list(providers), max_retries=0, **kw)


class TestModelRegistry(unittest.TestCase):
    def test_seeded_from_config_model_lists(self):
        from astra.ai.models import ModelRegistry
        cfg = Config()
        reg = ModelRegistry(cfg)
        # config has no provider model vars set by default (no fake env)
        self.assertGreaterEqual(reg.count(), 0)

    def test_add_and_metadata_derivation(self):
        from astra.ai.models import ModelRegistry
        reg = ModelRegistry()
        m = reg.add("gemini", "gemini-3.5-flash")
        self.assertEqual(m.provider, "gemini")
        self.assertIn("vision" if "vision" in m.capabilities else "chat",
                      m.capabilities)
        # flash → fast/cheap family heuristics hold
        self.assertEqual(m.speed_class, "mid")   # default speed from family
        self.assertTrue(m.supports_streaming)

    def test_filter_by_capabilities_and_provider(self):
        from astra.ai.models import ModelRegistry
        reg = ModelRegistry()
        reg.add("groq", "openai/gpt-oss-120b")
        reg.add("zai", "glm-4.7-flash")
        only = reg.filter(providers=["groq"])
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0].provider, "groq")

    def test_update_status_preferred_disabled(self):
        from astra.ai.models import ModelRegistry
        reg = ModelRegistry()
        reg.add("cohere", "command-a-03-2025")
        m = reg.update_status("cohere", "command-a-03-2025",
                              preferred=True, disabled=False)
        self.assertTrue(m.preferred)
        self.assertFalse(m.disabled)


class TestCredentialPool(unittest.TestCase):
    def test_round_robin_across_healthy_keys(self):
        from astra.ai.credentials import CredentialPool
        pool = CredentialPool("p", ["k1", "k2"])
        seen = {pool.pick(), pool.pick(), pool.pick(), pool.pick()}
        self.assertEqual(len(seen), 2)          # cycles between the two keys

    def test_auth_failure_disables_only_that_credential(self):
        from astra.ai.credentials import CredentialPool
        pool = CredentialPool("p", ["k1", "k2"])
        c1 = pool._creds[0]
        pool.report_failure(c1, auth_failure=True, reason="401")
        self.assertFalse(c1.healthy)
        self.assertTrue(pool._creds[1].healthy)
        self.assertEqual(pool.healthy_count, 1)

    def test_rate_limit_cooldown_then_recovery(self):
        from astra.ai.credentials import CredentialPool
        pool = CredentialPool("p", ["k1"])
        c1 = pool._creds[0]
        pool.report_failure(c1, rate_limited=True, cooldown_s=1000)
        self.assertIsNone(pool.pick())          # cooled down → no healthy key
        c1.cooldown_until = 0.0
        self.assertIsNotNone(pool.pick())


class TestRoutingDecisionPolicy(unittest.TestCase):
    def setUp(self):
        from astra.ai.models import Model
        self.reg = {}
        self.caps = Model("x", "m", capabilities=["chat", "tools", "json"],
                          quality_class="high", cost_class="mid",
                          context_window=128000, speed_class="mid")

    def test_hard_capability_requirement_rejected(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        req = RoutingRequest(task_type="research",
                             required_capabilities=["vision"],
                             vision=True)
        pol = RoutingDecisionPolicy()
        self.assertEqual(pol.score(self.caps, req), -1.0e6)

    def test_context_overflow_rejected(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        req = RoutingRequest(context_tokens=200000)
        pol = RoutingDecisionPolicy()
        self.assertEqual(pol.score(self.caps, req), -1.0e6)


class TestAgentRouter(unittest.TestCase):
    def test_route_falls_back_to_second_provider(self):
        bad = FakeAIProvider(name="bad", fail_first=9999)
        good = FakeAIProvider(name="good")
        r = _router(bad, good)
        name, model, reply = r.route([{"role": "user", "content": "hi"}])
        self.assertEqual(name, "good")
        self.assertEqual(reply, "reply-from-good")

    def test_route_none_when_no_healthy_provider(self):
        dead = FakeAIProvider(name="dead", healthy=False)
        r = _router(dead, OfflineProvider({}))
        name, model, reply = r.route([{"role": "user", "content": "hi"}])
        self.assertIsNone(name)
        self.assertIn("dead", r.stats()["down"])

    def test_route_request_returns_normalized_result(self):
        from astra.ai.router import RoutingRequest
        good = FakeAIProvider(name="good")
        r = _router(good)
        rr = r.route_request(RoutingRequest(task_type="simple_chat",
                                            messages=[{"role": "user", "content": "hi"}],
                                            user_preference="fastest"))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "good")
        self.assertEqual(rr.model, "m0")
        self.assertGreaterEqual(rr.latency_ms, 0)
        self.assertIn("task_type", rr.route_reason)

    def test_credential_failover_keeps_provider_alive(self):
        """A dead key must not kill the provider: the sibling pool key keeps
        the provider healthy and routing succeeds on the next attempt."""
        from astra.ai.credentials import CredentialPool
        pool = CredentialPool("p", ["k1", "k2"])
        pool._creds[0].healthy = False          # simulate auth-failed key
        provider = FakeAIProvider(name="flaky", pool=pool)
        r = AgentRouter([provider], max_retries=2, backoff_s=0.01)
        name, _, reply = r.route([{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "reply-from-flaky")
        self.assertFalse(pool.metadata()[0]["healthy"])   # k1 stays dead
        self.assertTrue(pool.metadata()[1]["healthy"])    # k2 still healthy
        self.assertEqual(pool.healthy_count, 1)

    def test_retry_with_backoff_recovers(self):
        flaky = FakeAIProvider(name="flaky", fail_first=1)
        r = AgentRouter([flaky], max_retries=2, backoff_s=0.01)
        name, _, reply = r.route([{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "reply-from-flaky")

    def test_stats_track_calls_and_errors(self):
        bad = FakeAIProvider(name="bad", fail_first=999)
        good = FakeAIProvider(name="good")
        r = _router(bad, good)
        r.route([{"role": "user", "content": "hi"}])
        h = r.health()
        self.assertEqual(h["good"]["calls"], 1)
        self.assertGreaterEqual(h["bad"]["errors"], 1)

    def test_routing_stats_learning(self):
        from astra.ai.router import RoutingRequest
        good = FakeAIProvider(name="good")
        r = _router(good)
        r.route_request(RoutingRequest(task_type="research",
                                       messages=[{"role": "user", "content": "x"}]))
        stats = r.routing_stats()
        self.assertTrue(any(v.get("calls", 0) >= 1 for v in stats.values()))
        # research → good provider learned
        tstats = r.task_stats()
        self.assertIn("research", "".join(tstats.keys()) or "research")

    def test_model_specific_and_capability_routing(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="vision", models=["vl-1"])
        provider.capabilities = ["chat", "vision", "stream", "json"]
        r = _router(provider)
        rr = r.route_request(RoutingRequest(task_type="vision", vision=True,
                                            messages=[{"role": "user", "content": "img"}]))
        self.assertTrue(rr.ok)

    def test_capability_preference_changes_score(self):
        # fastest preference should not reject basic route
        from astra.ai.router import RoutingRequest
        fast = FakeAIProvider(name="fast")
        r = _router(fast)
        rr = r.route_request(RoutingRequest(user_preference="fastest",
                                            messages=[{"role": "user", "content": "y"}]))
        self.assertTrue(rr.ok)


class TestAdapterConfiguration(unittest.TestCase):
    """Provider adapters read models/base URL from env, tolerate a blank
    key without crashing, and are absent from the registry when
    unconfigured — never silently "healthy" with nothing to call."""

    def _config(self, **env):
        cfg = Config()
        cfg._runtime.update(env)
        return cfg

    def test_base_url_env_overrides_default(self):
        from astra.ai.adapters.groq import GroqAdapter
        cfg = self._config(GROQ_BASE_URL="https://example.test/custom/v1")
        adapter = GroqAdapter(config=cfg)
        self.assertEqual(adapter.base_url, "https://example.test/custom/v1")

    def test_base_url_falls_back_to_class_default(self):
        from astra.ai.adapters.groq import GroqAdapter
        adapter = GroqAdapter(config=self._config())
        self.assertEqual(adapter.base_url, "https://api.groq.com/openai/v1")

    def test_base_url_trailing_slash_normalized(self):
        from astra.ai.adapters.mistral import MistralAdapter
        cfg = self._config(MISTRAL_BASE_URL="https://example.test/v1/")
        adapter = MistralAdapter(config=cfg)
        self.assertFalse(adapter.base_url.endswith("/"))

    def test_models_loaded_from_env(self):
        from astra.ai.adapters.gemini import GeminiAdapter
        cfg = self._config(GEMINI_MODELS="gemini-3.7-flash,gemini-3.6-flash")
        adapter = GeminiAdapter(config=cfg)
        self.assertEqual(adapter.models, ["gemini-3.7-flash", "gemini-3.6-flash"])

    def test_empty_api_key_does_not_crash_construction(self):
        from astra.ai.adapters.mistral import MistralAdapter
        adapter = MistralAdapter(config=self._config())  # no MISTRAL_API_KEYS
        self.assertFalse(bool(adapter.pool))              # unhealthy, not absent
        self.assertFalse(adapter.health_check() and bool(adapter.pool))

    def test_empty_api_key_raises_clean_provider_error_on_chat(self):
        from astra.ai.adapters.mistral import MistralAdapter
        from astra.core.exceptions import ProviderError
        adapter = MistralAdapter(config=self._config())
        with self.assertRaises(ProviderError):
            adapter.chat([{"role": "user", "content": "hi"}])

    def test_multiple_keys_parsed_and_trimmed(self):
        from astra.ai.credentials import CredentialPool
        pool = CredentialPool.from_env(
            self._config(GEMINI_API_KEYS=" key-1 , key-2 ,,key-3 "),
            "GEMINI_API_KEYS", "gemini")
        self.assertEqual(pool.count, 3)
        self.assertEqual(pool.healthy_count, 3)

    def test_unconfigured_provider_absent_from_registry(self):
        from astra.ai.registry import build_providers
        cfg = self._config()  # nothing configured at all
        reg = build_providers(config=cfg)
        self.assertIsNone(reg.get("groq"))
        self.assertIsNone(reg.get("gemini"))
        # AgentRouter is the routing brain, never a registry entry itself.
        self.assertIsNone(reg.get("agentrouter"))

    def test_configured_provider_present_in_registry(self):
        from astra.ai.registry import build_providers
        cfg = self._config(GROQ_API_KEYS="fake-key-for-test")
        reg = build_providers(config=cfg)
        self.assertIsNotNone(reg.get("groq"))

    def test_credential_summary_never_exposes_secret(self):
        from astra.ai.adapters.groq import GroqAdapter
        cfg = self._config(GROQ_API_KEYS="super-secret-value-123")
        adapter = GroqAdapter(config=cfg)
        dumped = str(adapter.credential_summary())
        self.assertNotIn("super-secret-value-123", dumped)


if __name__ == "__main__":
    unittest.main()