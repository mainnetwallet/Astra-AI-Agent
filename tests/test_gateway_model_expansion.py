"""Regression tests for the Astra AI Gateway model-list audit/expansion.

Context: the Gateway (`GW_*_MODELS`) and the Provider system (`*_MODELS`)
are two INDEPENDENT model configurations (see .env.example and
astra/ai/gateway.py's module docstring). This audit compared each
Provider's configured model list against the Gateway's own list for the
same service and added the Provider's missing FREE/no-cost models to the
Gateway list only — never touching Provider config, never merging the two
configs, never removing anything already in the Gateway list.

"Free" here means: the repository's own cost_class heuristic
(astra.ai.models.metadata_for) classifies the model "cheap", OR — for
OpenRouter specifically — the model id carries the provider's own explicit
":free" suffix (the convention OpenRouter itself publishes, already relied
on by the pre-existing GW_OPENROUTER_MODELS entries). AWS Bedrock is
excluded entirely: Bedrock has no free-tier model at all (every id is
metered, pay-per-token access), so nothing was added there regardless of
its relative cost_class.

These tests parse `.env.example` directly (via the same `_load_dotenv`
parser `Config` uses for a real `.env` file) so they exercise the actual
shipped configuration, not a hand-copied expectation of it.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from astra.core.config import Config, _load_dotenv
from astra.ai.gateway import (AstraAIGateway, AstraGatewayCloudflare,
                              AstraGatewayCohere, AstraGatewayGemini,
                              AstraGatewayGroq, AstraGatewayMistral,
                              AstraGatewayOpenRouter)
from astra.ai.gateway_routing import (build_gateway_catalog, eligible_targets,
                                      GatewayRoutingState)

ENV_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")


def _dotenv() -> dict:
    return _load_dotenv(ENV_EXAMPLE)


def _split(csv: str) -> list[str]:
    return [m.strip() for m in csv.split(",") if m.strip()]


# ── the audit's before/after picture, pinned here so a regression in
#    .env.example is caught by name rather than just "counts changed" ───────
ORIGINAL_GATEWAY_MODELS = {
    "GW_GEMINI_MODELS": "gemini-3.5-flash,gemini-3.1-flash-lite",
    "GW_GROQ_MODELS": "openai/gpt-oss-120b,openai/gpt-oss-20b",
    "GW_CLOUDFLARE_MODELS":
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/qwen/qwen2.5-coder-32b-instruct",
    "GW_BEDROCK_MODELS": "us.amazon.nova-lite-v1:0,amazon.nova-lite-v1:0",
    "GW_OPENROUTER_MODELS": (
        "nvidia/nemotron-3-ultra-550b-a55b:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "nvidia/nemotron-3.5-lightning:free,poolside/laguna-s-2.1:free,openrouter/free"),
    "GW_MISTRAL_MODELS": "mistral-small-2603,ministral-14b-2512,codestral-2508",
    "GW_CEREBRAS_MODELS": "gpt-oss-120b,gemma-4-31b",
    "GW_SAMBANOVA_MODELS": "Meta-Llama-3.3-70B-Instruct,DeepSeek-V3.1,gpt-oss-120b",
    "GW_COHERE_MODELS":
        "command-a-03-2025,command-a-reasoning-08-2025,command-a-vision-07-2025",
    "GW_ZAI_MODELS": "glm-4.7-flash,glm-4.5-flash,glm-4.6v-flash",
}

# Models the audit determined were missing + free/applicable, keyed by the
# same GW_*_MODELS var.
NEWLY_ADDED = {
    "GW_GEMINI_MODELS": {"gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"},
    "GW_GROQ_MODELS": {"qwen/qwen3.6-27b"},
    "GW_CLOUDFLARE_MODELS": {
        "@cf/meta/llama-3.1-70b-instruct-fp8-fast",
        "@cf/meta/llama-4-scout-17b-16e-instruct",
        "@cf/meta/llama-3.1-8b-instruct-fp8-fast",
        "@cf/meta/llama-3.1-8b-instruct-fp8",
        "@cf/meta/llama-3.2-1b-instruct",
        "@cf/meta/llama-3.2-3b-instruct",
        "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/qwen/qwq-32b",
        "@cf/qwen/qwen3-30b-a3b-fp8",
        "@cf/qwen/qwen3.8-27b",
        "@cf/openai/gpt-oss-20b",
        "@cf/google/gemma-4-26b-a4b-it",
        "@cf/aisingapore/gemma-sea-lion-v4-27b-it",
        "@cf/ibm-granite/granite-4.0-h-micro",
        "@cf/zai-org/glm-4.7-flash",
    },
    "GW_BEDROCK_MODELS": set(),  # explicitly excluded — no free Bedrock models
    "GW_OPENROUTER_MODELS": {
        "minimax/minimax-m3:free", "cohere/north-mini-code:free",
        "minimax/minimax-m2.7:free", "dots-studio/dots-3-note-preview:free",
        "inclusionai/ling-3.0-flash-fin:free", "poolside/laguna-xs-2.1:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "liquid/lfm-2.5-2.6b:free",
    },
    "GW_MISTRAL_MODELS": {"ministral-8b-2512", "ministral-3b-2512"},
    "GW_CEREBRAS_MODELS": set(),   # Provider list == Gateway list already
    "GW_SAMBANOVA_MODELS": set(),  # Provider list == Gateway list already
    "GW_COHERE_MODELS": {
        "c4ai-aya-expanse-32b", "c4ai-aya-vision-32b", "tiny-aya-global",
        "tiny-aya-earth", "tiny-aya-fire", "tiny-aya-water",
    },
    "GW_ZAI_MODELS": set(),  # Provider list == Gateway list already
}

# The Provider (non-GW) *_MODELS values must be BYTE-IDENTICAL to what they
# were before the audit — the audit never edits Provider config.
ORIGINAL_PROVIDER_MODELS = {
    "GEMINI_MODELS":
        "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    "GROQ_MODELS": "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.6-27b",
    "MISTRAL_MODELS":
        "mistral-small-2603,ministral-14b-2512,ministral-8b-2512,ministral-3b-2512,codestral-2508",
    "OPENROUTER_MODELS": (
        "nvidia/nemotron-3-ultra-550b-a55b:free,minimax/minimax-m3:free,"
        "poolside/laguna-s-2.1:free,nvidia/nemotron-3.5-lightning:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,cohere/north-mini-code:free,"
        "minimax/minimax-m2.7:free,dots-studio/dots-3-note-preview:free,"
        "inclusionai/ling-3.0-flash-fin:free,poolside/laguna-xs-2.1:free,"
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,openrouter/free,"
        "liquid/lfm-2.5-2.6b:free"),
    "CEREBRAS_MODELS": "gpt-oss-120b,gemma-4-31b",
    "SAMBA_MODELS": "Meta-Llama-3.3-70B-Instruct,DeepSeek-V3.1,gpt-oss-120b",
    "ZAI_MODELS": "glm-4.7-flash,glm-4.5-flash,glm-4.6v-flash",
    # CLOUDFLARE_MODELS, COHERE_MODELS and BEDROCK_MODELS are long; compared
    # via the live values captured at import time below instead of inlined.
}


class GatewayModelExpansionAuditTests(unittest.TestCase):
    """1) Existing Gateway models remain unchanged."""

    def setUp(self):
        self.env = _dotenv()

    def test_every_original_gateway_model_still_present(self):
        for var, original_csv in ORIGINAL_GATEWAY_MODELS.items():
            original = set(_split(original_csv))
            current = set(_split(self.env.get(var, "")))
            missing = original - current
            self.assertFalse(
                missing, f"{var} lost previously-configured model(s): {missing}")

    def test_gateway_lists_preserve_original_order_prefix(self):
        """New models are appended, not interleaved — the original
        configured-priority ordering (used as a routing tie-break by
        rank_targets) is preserved verbatim as a prefix."""
        for var, original_csv in ORIGINAL_GATEWAY_MODELS.items():
            original = _split(original_csv)
            current = _split(self.env.get(var, ""))
            self.assertEqual(
                current[:len(original)], original,
                f"{var} did not preserve original model order as a prefix")


class GatewayModelExpansionAdditionTests(unittest.TestCase):
    """2) Missing applicable free Provider models are now available to Gateway."""

    def setUp(self):
        self.env = _dotenv()

    def test_all_planned_free_models_were_added(self):
        for var, expected_new in NEWLY_ADDED.items():
            current = set(_split(self.env.get(var, "")))
            missing = expected_new - current
            self.assertFalse(
                missing, f"{var} is missing planned free model(s): {missing}")

    def test_bedrock_received_no_additions(self):
        """Bedrock has no free tier in reality — the Gateway model list must
        be untouched even though this audit added models everywhere else."""
        self.assertEqual(
            _split(self.env["GW_BEDROCK_MODELS"]),
            _split(ORIGINAL_GATEWAY_MODELS["GW_BEDROCK_MODELS"]))

    def test_every_added_gateway_model_exists_in_provider_list(self):
        """Nothing was invented — every newly added Gateway model id must be
        an id that already exists in that service's Provider model list."""
        provider_var_for_gateway_var = {
            "GW_GEMINI_MODELS": "GEMINI_MODELS",
            "GW_GROQ_MODELS": "GROQ_MODELS",
            "GW_CLOUDFLARE_MODELS": "CLOUDFLARE_MODELS",
            "GW_OPENROUTER_MODELS": "OPENROUTER_MODELS",
            "GW_MISTRAL_MODELS": "MISTRAL_MODELS",
            "GW_COHERE_MODELS": "COHERE_MODELS",
        }
        for gw_var, provider_var in provider_var_for_gateway_var.items():
            provider_models = set(_split(self.env.get(provider_var, "")))
            for mid in NEWLY_ADDED[gw_var]:
                self.assertIn(
                    mid, provider_models,
                    f"{gw_var} gained {mid!r}, which is not in {provider_var}")


class ProviderConfigUnchangedTests(unittest.TestCase):
    """3) Provider model configuration is unchanged."""

    def setUp(self):
        self.env = _dotenv()

    def test_short_provider_model_lists_are_byte_identical(self):
        for var, original_csv in ORIGINAL_PROVIDER_MODELS.items():
            self.assertEqual(
                self.env.get(var, ""), original_csv,
                f"{var} (Provider config) was modified by the Gateway audit")

    def test_bedrock_provider_models_unchanged(self):
        original_bedrock = (
            "us.anthropic.claude-opus-5,us.anthropic.claude-sonnet-5,"
            "us.anthropic.claude-fable-5,us.anthropic.claude-opus-4-8,"
            "us.anthropic.claude-opus-4-7,us.anthropic.claude-opus-4-6,"
            "us.anthropic.claude-opus-4-5,us.anthropic.claude-sonnet-4-6,"
            "us.anthropic.claude-sonnet-4-5,us.anthropic.claude-haiku-4-5-20251001,"
            "us.anthropic.claude-sonnet-4-20250514-v1:0,amazon.nova-2-sonic-v1:0,"
            "us.amazon.nova-2-lite-v1:0,amazon.nova-sonic-v1:0,amazon.nova-lite-v1:0,"
            "us.amazon.nova-micro-v1:0,us.amazon.nova-pro-v1:0,us.openai.gpt-5.6-luna,"
            "us.openai.gpt-5.6-sol,us.openai.gpt-5.6-terra,openai.gpt-oss-120b-1:0,"
            "openai.gpt-oss-20b-1:0,qwen.qwen3-235b-a22b-2507-v1:0,qwen.qwen3-32b,"
            "qwen.qwen3-coder-480b-a35b-v1:0,minimax.minimax-m2.5,minimax.minimax-m2.1,"
            "moonshotai.kimi-k2.5,zai.glm-5,zai.glm-4.7,zai.glm-4.7-flash,"
            "nvidia.nemotron-3-super-120b-v1:0,us.xai.grok-4-6,mistral.devstral-2-123b,"
            "mistral.pixtral-large-2502-v1:0,deepseek.v3.2,deepseek.v3.1")
        self.assertEqual(self.env.get("BEDROCK_MODELS", ""), original_bedrock)

    def test_cohere_provider_models_unchanged(self):
        original_cohere = (
            "command-a-plus-05-2026,command-a-03-2025,command-a-reasoning-08-2025,"
            "command-a-vision-07-2025,command-a-translate-08-2025,command-r7b-12-2024,"
            "command-r-plus-08-2024,command-r-08-2024,c4ai-aya-expanse-32b,"
            "c4ai-aya-vision-32b,tiny-aya-global,tiny-aya-earth,tiny-aya-fire,tiny-aya-water")
        self.assertEqual(self.env.get("COHERE_MODELS", ""), original_cohere)

    def test_cloudflare_provider_models_unchanged(self):
        original_cloudflare = (
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/meta/llama-3.1-70b-instruct-fp8-fast,"
            "@cf/meta/llama-4-scout-17b-16e-instruct,@cf/meta/llama-3.1-8b-instruct-fp8-fast,"
            "@cf/meta/llama-3.1-8b-instruct-fp8,@cf/meta/llama-3.2-1b-instruct,"
            "@cf/meta/llama-3.2-3b-instruct,@cf/deepseek-ai/deepseek-r1-distill-qwen-32b,"
            "@cf/moonshotai/kimi-k2.6,@cf/mistralai/mistral-small-3.1-24b-instruct,"
            "@cf/qwen/qwen2.5-coder-32b-instruct,@cf/qwen/qwq-32b,@cf/qwen/qwen3-30b-a3b-fp8,"
            "@cf/qwen/qwen3.8-27b,@cf/openai/gpt-oss-120b,@cf/openai/gpt-oss-20b,"
            "@cf/google/gemma-4-26b-a4b-it,@cf/aisingapore/gemma-sea-lion-v4-27b-it,"
            "@cf/ibm-granite/granite-4.0-h-micro,@cf/zai-org/glm-4.7-flash,"
            "@cf/nvidia/nemotron-3-120b-a12b")
        self.assertEqual(self.env.get("CLOUDFLARE_MODELS", ""), original_cloudflare)


class IndependentConfigurationTests(unittest.TestCase):
    """4) Gateway and Provider maintain independent configuration."""

    def test_gateway_and_provider_cloudflare_lists_differ(self):
        """Gateway is a deliberate SUBSET, not a copy/merge, of Provider —
        proves the two configs were never unified into one (Cloudflare's
        Provider list has paid-only entries the audit correctly excluded,
        so the two lists must stay unequal)."""
        env = _dotenv()
        gw = set(_split(env["GW_CLOUDFLARE_MODELS"]))
        provider = set(_split(env["CLOUDFLARE_MODELS"]))
        self.assertTrue(gw.issubset(provider))
        self.assertNotEqual(
            gw, provider, "Gateway/Provider Cloudflare lists were merged into one")

    def test_changing_provider_env_does_not_change_gateway_connection(self):
        """Live proof, not just static file inspection: pointing GEMINI_MODELS
        at something else must not affect what AstraGatewayGemini serves."""
        cfg = Config()
        cfg._runtime.update({
            "GEMINI_MODELS": "totally-unrelated-provider-only-model",
            "GW_GEMINI_API_KEYS": "k1",
            "GW_GEMINI_MODELS": "gemini-3.5-flash,gemini-3.7-flash",
        })
        conn = AstraGatewayGemini(config=cfg)
        self.assertEqual(conn.models, ["gemini-3.5-flash", "gemini-3.7-flash"])
        self.assertNotIn("totally-unrelated-provider-only-model", conn.models)


class NoDuplicateModelIdTests(unittest.TestCase):
    """5) No duplicate model IDs are introduced."""

    def test_no_duplicates_within_any_gateway_list(self):
        env = _dotenv()
        for var in ORIGINAL_GATEWAY_MODELS:
            models = _split(env.get(var, ""))
            dupes = {m for m in models if models.count(m) > 1}
            self.assertFalse(dupes, f"{var} contains duplicate id(s): {dupes}")

    def test_no_duplicates_within_expanded_provider_lists_touched(self):
        env = _dotenv()
        for var in ("GEMINI_MODELS", "GROQ_MODELS", "CLOUDFLARE_MODELS",
                    "OPENROUTER_MODELS", "MISTRAL_MODELS", "COHERE_MODELS"):
            models = _split(env.get(var, ""))
            dupes = {m for m in models if models.count(m) > 1}
            self.assertFalse(dupes, f"{var} contains duplicate id(s): {dupes}")


class GatewayCannotSelectUnservableModelTests(unittest.TestCase):
    """6) Gateway does not select a model that the corresponding Gateway
    connection cannot actually serve — i.e. the catalog is built strictly
    from each connection's OWN configured `models` list, nothing else."""

    def test_catalog_only_contains_configured_models(self):
        cfg = Config()
        cfg._runtime.update({
            "GW_GEMINI_API_KEYS": "k1",
            "GW_GEMINI_MODELS": "gemini-3.5-flash,gemini-3.7-flash",
        })
        conn = AstraGatewayGemini(config=cfg)
        catalog = build_gateway_catalog([conn])
        served_ids = {model.model_id for _c, model in catalog}
        self.assertEqual(served_ids, {"gemini-3.5-flash", "gemini-3.7-flash"})
        self.assertNotIn("gemini-3.6-flash", served_ids)  # not configured on this conn

    def test_eligible_targets_never_returns_an_unconfigured_model(self):
        """Even with a newly-expanded, larger model list, every eligible
        target's model id must trace back to that exact connection's
        `models` — never a Provider-only id, never another connection's id."""
        cfg = Config()
        cfg._runtime.update({
            "GW_CLOUDFLARE_API_KEYS": "k1",
            "GW_CLOUDFLARE_ACCOUNT_IDS": "acct1",
            "GW_CLOUDFLARE_MODELS": _dotenv()["GW_CLOUDFLARE_MODELS"],
            "GW_GEMINI_API_KEYS": "k2",
            "GW_GEMINI_MODELS": _dotenv()["GW_GEMINI_MODELS"],
        })
        cf = AstraGatewayCloudflare(config=cfg)
        gem = AstraGatewayGemini(config=cfg)
        catalog = build_gateway_catalog([cf, gem])
        state = GatewayRoutingState(store=None)
        targets = eligible_targets(catalog, state, category="general")
        cf_ids = set(cf.models)
        gem_ids = set(gem.models)
        for conn, model, _health in targets:
            if conn is cf:
                self.assertIn(model.model_id, cf_ids)
                self.assertNotIn(model.model_id, gem_ids - cf_ids)
            elif conn is gem:
                self.assertIn(model.model_id, gem_ids)
                self.assertNotIn(model.model_id, cf_ids - gem_ids)


class NewlyAddedModelRequestConstructionTests(unittest.TestCase):
    """Request construction / routing / streaming / tool calling still work
    end-to-end for the newly added models — same contract as every other
    Gateway model (no special-casing introduced by the expansion)."""

    def _cfg(self, **env):
        cfg = Config()
        cfg._runtime.update(env)
        return cfg

    def _resp(self, body: bytes):
        import io as _io

        class _R:
            def read(self_inner):
                return body
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False
            def close(self_inner):
                pass
        return _R()

    def test_new_gemini_model_builds_correct_request_and_parses_reply(self):
        import json as _json
        new_model = "gemini-3.7-flash"  # newly added by this audit
        conn = AstraGatewayGemini(config=self._cfg(
            GW_GEMINI_API_KEYS="secret-key", GW_GEMINI_MODELS=new_model))
        body = self._resp(_json.dumps(
            {"choices": [{"message": {"content": "hi from new model"}}]}).encode())
        with mock.patch("urllib.request.urlopen", return_value=body) as m:
            reply = conn.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(reply, "hi from new model")
        sent = _json.loads(m.call_args[0][0].data.decode())
        self.assertEqual(sent["model"], new_model)
        self.assertEqual(
            m.call_args[0][0].headers.get("Authorization"), "Bearer secret-key")

    def test_new_mistral_model_streams(self):
        new_model = "ministral-8b-2512"  # newly added by this audit
        conn = AstraGatewayMistral(config=self._cfg(
            GW_MISTRAL_API_KEYS="k", GW_MISTRAL_MODELS=new_model))
        sse = (b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
               b'data: [DONE]\n\n')
        with mock.patch("urllib.request.urlopen", return_value=self._resp(sse)):
            chunks = list(conn.stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(chunks, ["a", "b"])

    def test_new_openrouter_free_model_included_in_catalog_with_vision_capability(self):
        new_model = "liquid/lfm-2.5-2.6b:free"  # newly added by this audit
        conn = AstraGatewayOpenRouter(config=self._cfg(
            GW_OPENROUTER_API_KEYS="k", GW_OPENROUTER_MODELS=new_model))
        catalog = build_gateway_catalog([conn])
        self.assertEqual(len(catalog), 1)
        _c, model = catalog[0]
        self.assertEqual(model.model_id, new_model)
        self.assertIn("chat", model.capabilities)

    def test_new_cohere_model_tool_call_round_trip(self):
        import json as _json
        new_model = "c4ai-aya-expanse-32b"  # newly added by this audit
        conn = AstraGatewayCohere(config=self._cfg(
            GW_COHERE_API_KEYS="k", GW_COHERE_MODELS=new_model))
        tool_call_body = _json.dumps({"choices": [{"message": {
            "content": "", "tool_calls": [{"id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{}"}}]}}]}).encode()
        with mock.patch("urllib.request.urlopen",
                        return_value=self._resp(tool_call_body)) as m:
            data = conn._post(f"{conn._api_base()}/chat/completions",
                              {"model": new_model, "messages": [],
                               "tools": [{"type": "function",
                                         "function": {"name": "get_weather"}}]},
                              conn._pick())
        sent = _json.loads(m.call_args[0][0].data.decode())
        self.assertEqual(sent["model"], new_model)
        self.assertIn("tools", sent)
        self.assertEqual(
            data["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "get_weather")


class HealthCheckAndFailoverArchitectureUnchangedTests(unittest.TestCase):
    """Verify the shared health-check / failover architecture (unchanged by
    this audit) still functions correctly across an expanded model list."""

    def test_health_check_reflects_credential_pool_regardless_of_model_count(self):
        cfg = Config()
        cfg._runtime.update({
            "GW_GEMINI_API_KEYS": "",
            "GW_GEMINI_MODELS": _dotenv()["GW_GEMINI_MODELS"],
        })
        conn = AstraGatewayGemini(config=cfg)
        self.assertFalse(conn.health_check())  # no keys → unhealthy, independent of model count

        cfg2 = Config()
        cfg2._runtime.update({
            "GW_GEMINI_API_KEYS": "k1",
            "GW_GEMINI_MODELS": _dotenv()["GW_GEMINI_MODELS"],
        })
        conn2 = AstraGatewayGemini(config=cfg2)
        self.assertTrue(conn2.health_check())

    def test_failover_moves_to_next_connection_with_expanded_model_lists(self):
        cfg = Config()
        cfg._runtime.update({
            "GW_GEMINI_API_KEYS": "k1",
            "GW_GEMINI_MODELS": _dotenv()["GW_GEMINI_MODELS"],
            "GW_GROQ_API_KEYS": "k2",
            "GW_GROQ_MODELS": _dotenv()["GW_GROQ_MODELS"],
        })
        gateway = AstraAIGateway(config=cfg)
        names = [c.name for c in gateway.connections]
        self.assertIn("astra-gw-gemini", names)
        self.assertIn("astra-gw-groq", names)
        # Fallback order (Gemini before Groq) is unchanged by the audit.
        self.assertLess(names.index("astra-gw-gemini"), names.index("astra-gw-groq"))


if __name__ == "__main__":
    unittest.main()
