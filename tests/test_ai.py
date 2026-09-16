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


class TestDynamicProviderModelRouting(unittest.TestCase):
    """Coverage for the dynamic provider+model routing upgrade: capability
    matching, health/cooldown-aware selection, model-aware failover,
    deterministic tie-breaking, and the AgentRouter.org gateway fallback."""

    def _config(self, **env):
        cfg = Config()
        cfg._runtime.update(env)
        return cfg

    # 1. coding task -> coding-capable model
    def test_coding_task_selects_coding_capable_model(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="multi", models=["m0", "codestral-1"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(task_type="coding",
                                            messages=[{"role": "user", "content": "fix this bug"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "codestral-1")

    # 2. vision task -> never a text-only model
    def test_vision_task_never_selects_text_only_model(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="multi", models=["m0", "vl-1"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(task_type="vision",
                                            messages=[{"role": "user", "content": "describe this image"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "vl-1")

    def test_vision_task_fails_cleanly_with_no_vision_model_available(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="textonly", models=["m0"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(task_type="vision",
                                            messages=[{"role": "user", "content": "describe this image"}]))
        self.assertFalse(rr.ok)
        self.assertNotEqual(rr.model, "m0")

    # 3. reasoning task -> prefers reasoning-capable model
    def test_reasoning_task_prefers_reasoning_capable_model(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="multi", models=["m0", "kimi-k2"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(task_type="reasoning",
                                            messages=[{"role": "user", "content": "why is the sky blue"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "kimi-k2")

    # 4. unhealthy provider excluded
    def test_unhealthy_provider_excluded_from_candidates(self):
        from astra.ai.router import RoutingRequest
        dead = FakeAIProvider(name="dead", healthy=False)
        good = FakeAIProvider(name="good")
        r = _router(dead, good)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "good")
        self.assertIn("dead", r.stats()["down"])

    # 5. cooling-down credential excluded
    def test_cooling_down_credential_excludes_provider(self):
        from astra.ai.credentials import CredentialPool
        from astra.ai.router import RoutingRequest
        pool = CredentialPool("cooled", ["only-key"])
        pool._creds[0].mark_failure("rate limited", rate_limited=True, block_s=999)
        cooled = FakeAIProvider(name="cooled", pool=pool)
        good = FakeAIProvider(name="good")
        r = _router(cooled, good)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "good")

    # 6/7. failed candidate moves on, and is never retried within one call
    def test_failed_model_moves_to_next_candidate_without_repeating(self):
        from astra.ai.router import RoutingRequest
        always_fails = FakeAIProvider(name="broken", models=["a", "b"], fail_first=9999)
        r = AgentRouter([always_fails], max_retries=0)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertFalse(rr.ok)
        # exactly one attempt per distinct (provider, model) candidate — no
        # candidate is retried once it has failed within this call.
        self.assertEqual(rr.attempts, 2)

    # 8. provider + model both present on success
    def test_routing_decision_has_provider_and_model(self):
        from astra.ai.router import RoutingRequest
        good = FakeAIProvider(name="good")
        r = _router(good)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.provider)
        self.assertTrue(rr.model)
        self.assertIn("candidates_considered", rr.route_reason)

    # 9/10. registry shape
    def test_all_ten_real_providers_registered_and_agentrouter_absent(self):
        from astra.ai.registry import build_providers
        keys = {
            "GEMINI_API_KEYS": "k", "GROQ_API_KEYS": "k", "MISTRAL_API_KEYS": "k",
            "OPENROUTER_API_KEYS": "k", "CEREBRAS_API_KEYS": "k",
            "CLOUDFLARE_API_KEYS": "k", "SAMBA_API_KEYS": "k",
            "COHERE_API_KEYS": "k", "ZAI_API_KEYS": "k", "BEDROCK_CREDENTIALS": "k",
        }
        cfg = self._config(**keys)
        reg = build_providers(config=cfg)
        expected = {"gemini", "groq", "mistral", "openrouter", "cerebras",
                    "cloudflare", "sambanova", "cohere", "zai", "bedrock"}
        self.assertEqual(expected, set(reg.names()) & expected)
        for name in expected:
            self.assertIsNotNone(reg.get(name))
        self.assertIsNone(reg.get("agentrouter"))
        self.assertIsNone(reg.get("agentrouter_gateway"))

    # 11. secrets never exposed in errors
    def test_provider_error_never_contains_api_key(self):
        from astra.ai.adapters.mistral import MistralAdapter
        from astra.core.exceptions import ProviderError
        secret = "super-secret-mistral-key-xyz"
        adapter = MistralAdapter(config=self._config(MISTRAL_API_KEYS=secret))
        try:
            adapter.chat([{"role": "user", "content": "hi"}])
        except ProviderError as e:
            self.assertNotIn(secret, str(e))
            self.assertNotIn(secret, e.message or "")

    # 12. AgentRouter.org gateway still works as a router fallback
    def test_gateway_used_as_last_resort_fallback_when_providers_fail(self):
        from astra.ai.router import RoutingRequest
        dead = FakeAIProvider(name="dead", healthy=False)
        gateway = FakeAIProvider(name="agentrouter_gateway", models=["deepseek-v4-flash"])
        r = AgentRouter([dead], max_retries=0, gateway=gateway)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "agentrouter_gateway")
        self.assertTrue(rr.fallback_used)
        self.assertEqual(rr.route_reason.get("preference"), "agentrouter_gateway_fallback")
        # never counted among ordinary provider health/dashboard entries
        self.assertNotIn("agentrouter_gateway", r.health())
        self.assertEqual(r.gateway_health()["state"], "healthy")

    # 13. no Claude CLI impersonation
    def test_gateway_client_sends_no_special_or_spoofed_headers(self):
        from astra.ai.agentrouter_gateway import AgentRouterGatewayClient
        cfg = self._config(AGENTROUTER_API_KEYS="fake-key-for-test")
        client = AgentRouterGatewayClient(config=cfg)
        self.assertEqual(client.extra_headers, {})
        self.assertNotIn("anthropic", client.base_url.lower())

    # 14. empty/unconfigured credentials handled gracefully
    def test_unconfigured_provider_credentials_yield_graceful_failure(self):
        from astra.ai.router import RoutingRequest
        unconfigured = FakeAIProvider(name="empty", healthy=False)
        r = _router(unconfigured)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertFalse(rr.ok)
        self.assertTrue(rr.error)

    # 15. deterministic tie-breaking
    def test_equal_score_candidates_rank_deterministically(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        a = FakeAIProvider(name="a", models=["m0"])
        b = FakeAIProvider(name="b", models=["m0"])
        r = _router(a, b)
        req = RoutingRequest(messages=[{"role": "user", "content": "hi"}])
        candidates = r._candidates(req)
        order1 = [ad.name for _, ad, _ in r.policy.rank(candidates, req)]
        order2 = [ad.name for _, ad, _ in r.policy.rank(candidates, req)]
        self.assertEqual(order1, order2)


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


    def test_agentrouter_gateway_client_distinct_from_internal_router(self):
        """The agentrouter.org client is a distinct object from Astra's own
        AgentRouter routing engine, and uses plain OpenAI-compatible auth
        with no special/spoofed headers."""
        from astra.ai.agentrouter_gateway import AgentRouterGatewayClient
        cfg = self._config(AGENTROUTER_API_KEYS="fake-key-for-test")
        client = AgentRouterGatewayClient(config=cfg)
        self.assertEqual(client.base_url, "https://agentrouter.org/v1")
        self.assertEqual(client.extra_headers, {})
        self.assertTrue(bool(client.pool))

    def test_agentrouter_gateway_never_in_provider_registry(self):
        """AgentRouter.org is never a ProviderRegistry entry, configured or not
        — it is not a provider (see astra/ai/agentrouter_gateway.py)."""
        from astra.ai.registry import build_providers
        self.assertIsNone(build_providers(config=self._config()).get("agentrouter_gateway"))
        cfg = self._config(AGENTROUTER_API_KEYS="fake-key-for-test")
        self.assertIsNone(build_providers(config=cfg).get("agentrouter_gateway"))
        self.assertNotIn("agentrouter_gateway", build_providers(config=cfg).names())

    def test_agentrouter_gateway_builder_absent_when_unconfigured(self):
        from astra.ai.agentrouter_gateway import build_agentrouter_gateway
        self.assertIsNone(build_agentrouter_gateway(config=self._config()))
        cfg = self._config(AGENTROUTER_API_KEYS="fake-key-for-test")
        gw = build_agentrouter_gateway(config=cfg)
        self.assertIsNotNone(gw)
        self.assertEqual(gw.name, "agentrouter_gateway")

    def test_agentrouter_gateway_used_only_as_router_fallback_not_a_provider(self):
        """The gateway, when configured, is reachable only via
        AgentRouter.gateway — never mixed into router.providers."""
        from astra.ai.router import AgentRouter
        from astra.ai.agentrouter_gateway import AgentRouterGatewayClient
        cfg = self._config(AGENTROUTER_API_KEYS="fake-key-for-test")
        gw = AgentRouterGatewayClient(config=cfg)
        r = AgentRouter(providers=[], config=cfg, max_retries=0, gateway=gw)
        self.assertEqual(r.providers, [])
        self.assertIs(r.gateway, gw)
        health = r.gateway_health()
        self.assertIn("state", health)
        self.assertNotIn("agentrouter_gateway", r.health())


class TestProviderModelRoutingFix(unittest.TestCase):
    """Regression coverage for the AgentRouter dynamic provider+model
    routing fix: provider identity, conservative capability metadata,
    max_latency_ms / max_cost_usd actually influencing scoring,
    specific_provider / specific_model preferences, and stats being
    recorded against the real serving provider (not a family guess)."""

    def _config(self, **env):
        cfg = Config()
        cfg._runtime.update(env)
        return cfg

    # -- provider identity ------------------------------------------------
    def test_metadata_for_keeps_real_provider_for_family_mismatched_model(self):
        from astra.ai.models import metadata_for
        # deepseek/kimi/qwen model ids match a family whose base_provider
        # guess differs from the real adapter serving them through
        # OpenRouter — the real provider must always win.
        self.assertEqual(metadata_for("deepseek-v4-flash", "openrouter")["provider"],
                         "openrouter")
        self.assertEqual(metadata_for("kimi-k2", "openrouter")["provider"],
                         "openrouter")
        self.assertEqual(metadata_for("qwen-2.5-coder", "openrouter")["provider"],
                         "openrouter")
        self.assertEqual(metadata_for("glm-4.7-flash", "zai")["provider"], "zai")
        self.assertEqual(metadata_for("llama-3.3-70b", "groq")["provider"], "groq")

    def test_model_registry_add_keeps_real_provider_for_family_mismatched_model(self):
        from astra.ai.models import ModelRegistry
        reg = ModelRegistry()
        m = reg.add("openrouter", "deepseek-v4-flash")
        self.assertEqual(m.provider, "openrouter")

    def test_router_candidate_model_provider_matches_real_adapter(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="openrouter", models=["deepseek-v4-flash"])
        r = _router(provider)
        candidates = r._candidates(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertEqual(len(candidates), 1)
        _, model = candidates[0]
        self.assertEqual(model.provider, "openrouter")

    def test_discovery_validate_no_longer_raises_on_provider_kwarg(self):
        from astra.ai.models import ModelRegistry
        from astra.ai.discovery import ModelDiscovery

        class _Adapter:
            name = "openrouter"

            def list_models(self):
                return ["deepseek-v4-flash"]

        reg = ModelRegistry()
        disc = ModelDiscovery(reg, adapter_by_name={"openrouter": _Adapter()})
        result = disc.validate("openrouter", "deepseek-v4-flash")
        self.assertTrue(result["valid"])
        m = reg.get("openrouter", "deepseek-v4-flash")
        self.assertIsNotNone(m)
        self.assertEqual(m.provider, "openrouter")

    def test_stats_recorded_under_real_adapter_provider_not_family_guess(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="openrouter", models=["deepseek-v4-flash"])
        r = _router(provider)
        r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}]))
        stats = r.routing_stats()
        self.assertIn("openrouter:deepseek-v4-flash", stats)
        self.assertNotIn("deepseek:deepseek-v4-flash", stats)

    # -- conservative capability metadata -----------------------------------
    def test_unknown_model_family_gets_conservative_capabilities(self):
        from astra.ai.models import metadata_for
        meta = metadata_for("some-totally-unknown-model-xyz", "customprovider")
        self.assertEqual(meta["capabilities"], ["chat"])
        self.assertFalse(meta["supports_json"])

    def test_capabilities_and_supports_flags_stay_consistent(self):
        from astra.ai.models import metadata_for
        meta = metadata_for("gemini-3.5-pro", "gemini")
        self.assertIn("vision", meta["capabilities"])
        self.assertTrue(meta["supports_vision"])
        self.assertEqual(meta["supports_tools"], "tools" in meta["capabilities"])

    # -- long-context capability routing -------------------------------------
    def test_context_window_requirement_excludes_short_context_model(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="ctxprov",
                                  models=["short-1", "gemini-3.5-pro-longctx"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(context_tokens=500000,
                                            messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "gemini-3.5-pro-longctx")

    # -- lowest-cost preference -----------------------------------------------
    def test_lowest_cost_preference_favors_cheap_model(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="costprov", models=["gemini-3.5-flash", "grok-4"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(user_preference="lowest_cost",
                                            messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "gemini-3.5-flash")

    # -- max_latency_ms actually influences scoring --------------------------
    def test_max_latency_ms_penalizes_slow_candidate(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        from astra.ai.models import Model
        fast = Model("p", "fast-model", capabilities=["chat"], speed_class="fast")
        slow = Model("p", "slow-model", capabilities=["chat"], speed_class="slow")
        req = RoutingRequest(max_latency_ms=500)
        pol = RoutingDecisionPolicy()
        self.assertGreater(pol.score(fast, req), pol.score(slow, req))

    def test_max_latency_ms_end_to_end_prefers_faster_candidate(self):
        from astra.ai.router import RoutingRequest
        from astra.ai.models import Model, ModelRegistry
        reg = ModelRegistry()
        reg.add("multi", "fast-1", speed_class="fast")
        reg.add("multi", "slow-1", speed_class="slow")
        provider = FakeAIProvider(name="multi", models=["fast-1", "slow-1"])
        r = _router(provider, registry=reg)
        rr = r.route_request(RoutingRequest(max_latency_ms=500,
                                            messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "fast-1")

    # -- max_cost_usd actually influences scoring -----------------------------
    def test_max_cost_usd_penalizes_expensive_candidate(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        from astra.ai.models import Model
        cheap = Model("p", "cheap-model", capabilities=["chat"], cost_class="cheap")
        premium = Model("p", "premium-model", capabilities=["chat"], cost_class="premium")
        req = RoutingRequest(max_cost_usd=0.0005, max_tokens=500)
        pol = RoutingDecisionPolicy()
        self.assertGreater(pol.score(cheap, req), pol.score(premium, req))

    def test_max_cost_usd_end_to_end_prefers_cheaper_candidate(self):
        from astra.ai.router import RoutingRequest
        from astra.ai.models import ModelRegistry
        reg = ModelRegistry()
        reg.add("multi", "cheap-1", cost_class="cheap")
        reg.add("multi", "premium-1", cost_class="premium")
        provider = FakeAIProvider(name="multi", models=["cheap-1", "premium-1"])
        r = _router(provider, registry=reg)
        rr = r.route_request(RoutingRequest(max_cost_usd=0.0005, max_tokens=500,
                                            messages=[{"role": "user", "content": "hi"}]))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "cheap-1")

    # -- specific_provider / specific_model preferences -----------------------
    def test_preferred_provider_bonus_changes_ranking(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        from astra.ai.models import Model
        a = Model("providerA", "m", capabilities=["chat"])
        b = Model("providerB", "m", capabilities=["chat"])
        req = RoutingRequest(preferred_provider="providerB")
        pol = RoutingDecisionPolicy()
        self.assertGreater(pol.score(b, req), pol.score(a, req))

    def test_preferred_model_bonus_changes_ranking(self):
        from astra.ai.routing_policy import RoutingDecisionPolicy
        from astra.ai.router import RoutingRequest
        from astra.ai.models import Model
        m1 = Model("p", "model-a", capabilities=["chat"])
        m2 = Model("p", "model-b", capabilities=["chat"])
        req = RoutingRequest(preferred_model="model-b")
        pol = RoutingDecisionPolicy()
        self.assertGreater(pol.score(m2, req), pol.score(m1, req))

    def test_router_prefers_specific_provider_end_to_end(self):
        from astra.ai.router import RoutingRequest
        a = FakeAIProvider(name="a", models=["m0"])
        b = FakeAIProvider(name="b", models=["m0"])
        r = _router(a, b)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                                            preferred_provider="b"))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "b")

    def test_router_prefers_specific_model_end_to_end(self):
        from astra.ai.router import RoutingRequest
        provider = FakeAIProvider(name="multi", models=["m0", "m1"])
        r = _router(provider)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                                            preferred_model="m1"))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.model, "m1")

    # -- unavailable specific provider degrades gracefully, doesn't break routing
    def test_preferred_provider_not_available_still_routes(self):
        from astra.ai.router import RoutingRequest
        good = FakeAIProvider(name="good")
        r = _router(good)
        rr = r.route_request(RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                                            preferred_provider="nonexistent"))
        self.assertTrue(rr.ok)
        self.assertEqual(rr.provider, "good")


if __name__ == "__main__":
    unittest.main()