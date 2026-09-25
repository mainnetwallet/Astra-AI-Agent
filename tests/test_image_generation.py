# -*- coding: utf-8 -*-
"""Image-generation architecture: capability registry, Gateway routing,
eligible-target filtering, real provider image APIs, failover and artifacts.

Every test here is either a pure decision-logic test or a mocked
provider-contract test (urllib is patched). No test performs a real network
call, and no test claims image generation works unless the provider image API
was actually invoked.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.ai.gateway_routing import (CATEGORY_HARD_CAPS, REQUEST_CATEGORIES,
                                      build_gateway_catalog,
                                      classify_gateway_request,
                                      eligible_image_generation_targets,
                                      meets_gateway_requirements,
                                      rank_targets, describe_image_targets)
from astra.ai.image_models import (IMAGE_GENERATION, is_image_model,
                                   is_image_editing_model)
from astra.ai.models import Model, ModelRegistry, metadata_for
from astra.ai.router import RoutingRequest, classify
from astra.core.config import Config
from astra.core.exceptions import ProviderError, TimeoutError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
B64 = base64.b64encode(PNG).decode()
DATA_URI = "data:image/png;base64," + B64


def _resp(body, ctype="application/json"):
    class _R:
        def __init__(self):
            self.headers = {"Content-Type": ctype}
        def read(self):
            return body if isinstance(body, bytes) else body.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def close(self):
            pass
    return _R()


def _gemini_ok():
    return json.dumps({"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": B64}}]}}]})


def _cf_ok():
    return json.dumps({"result": {"image": B64}, "success": True})


def _openai_images_ok():
    return json.dumps({"data": [{"b64_json": B64}]})


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "err", {}, None)


class _FakeConn:
    """Minimal Gateway connection double: counts calls, scripted outcomes."""

    def __init__(self, name, short, models=(), image_models=(),
                 outcomes=None):
        self.name = name
        self.short = short
        self.models = list(models)
        self.image_models = list(image_models)
        self.pool = True
        self._outcomes = list(outcomes or [])
        self.image_calls = []
        self.chat_calls = 0

    def health_check(self):
        return True

    def supports(self, cap):
        return True

    def list_image_models(self, *, discover=True):
        return list(self.image_models)

    def chat(self, messages, model=None, max_tokens=None):
        self.chat_calls += 1
        return "text answer"

    def generate_image(self, prompt, model=None, size="1024x1024", n=1):
        self.image_calls.append((model, prompt))
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(prompt, model)
        return DATA_URI


def _cfg(**env):
    c = Config()
    c._runtime.update(env)
    return c


# ═══════════════════════════════════════════════════════════════════════════
# 1. Explicit capability registry — evidence, not name-guessing
# ═══════════════════════════════════════════════════════════════════════════
class TestCapabilityRegistry(unittest.TestCase):
    def test_documented_gemini_image_models_are_image_capable(self):
        for mid in ("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image",
                    "gemini-3-pro-image", "gemini-2.5-flash-image"):
            self.assertTrue(is_image_model("gemini", mid), mid)
            meta = metadata_for(mid, "gemini")
            self.assertIn(IMAGE_GENERATION, meta["capabilities"])
            self.assertIn("image", meta["output_modalities"])

    def test_ordinary_gemini_vision_models_are_not_image_capable(self):
        for mid in ("gemini-3.7-flash", "gemini-3.5-flash-lite"):
            self.assertFalse(is_image_model("gemini", mid), mid)
            meta = metadata_for(mid, "gemini")
            self.assertNotIn(IMAGE_GENERATION, meta["capabilities"])
            self.assertNotIn("image", meta["output_modalities"])

    def test_bedrock_image_families_are_image_capable(self):
        for mid in ("amazon.nova-canvas-v1:0",
                    "amazon.titan-image-generator-v2:0",
                    "us.stability.stable-diffusion-xl-v1"):
            self.assertTrue(is_image_model("bedrock", mid), mid)
            self.assertIn("image", metadata_for(mid, "bedrock")["output_modalities"])

    def test_bedrock_chat_models_are_not_image_capable(self):
        for mid in ("us.anthropic.claude-opus-4-5", "amazon.nova-pro-v1:0",
                    "deepseek.v3.1", "mistral.pixtral-large-2502-v1:0"):
            self.assertFalse(is_image_model("bedrock", mid), mid)
            self.assertNotIn("image",
                             metadata_for(mid, "bedrock")["output_modalities"])

    def test_cloudflare_flux_is_image_capable_but_llama_is_not(self):
        self.assertTrue(is_image_model(
            "cloudflare", "@cf/black-forest-labs/flux-1-schnell"))
        self.assertFalse(is_image_model(
            "cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"))

    def test_zai_glm_image_documented(self):
        self.assertTrue(is_image_model("zai", "glm-image"))
        self.assertTrue(is_image_model("zai", "cogview-4-250304"))
        self.assertIn("image",
                      metadata_for("glm-image", "zai")["output_modalities"])

    def test_vision_input_is_not_image_generation(self):
        """The whole point: understanding an image is not producing one."""
        meta = metadata_for("us.anthropic.claude-opus-4-5", "bedrock")
        self.assertIn("vision", meta["capabilities"])
        self.assertNotIn(IMAGE_GENERATION, meta["capabilities"])
        self.assertNotIn("image", meta["output_modalities"])

    def test_multimodal_or_omni_names_do_not_grant_image_generation(self):
        for provider, mid in (
                ("openrouter",
                 "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
                ("gemini", "gemini-3.7-flash"),
                ("groq", "openai/gpt-oss-120b")):
            meta = metadata_for(mid, provider)
            self.assertNotIn(IMAGE_GENERATION, meta["capabilities"], mid)
            self.assertNotIn("image", meta["output_modalities"], mid)

    def test_providers_without_an_image_api_are_never_image_capable(self):
        for provider, mid in (("groq", "llama-3.1-70b"),
                              ("cerebras", "gemma-4-31b"),
                              ("sambanova", "DeepSeek-V3.1"),
                              ("cohere", "command-a-vision-07-2025"),
                              ("mistral", "mistral-small-2603"),
                              ("openai", "dall-e-3")):
            self.assertFalse(is_image_model(provider, mid), provider)

    def test_image_editing_distinct_from_generation(self):
        self.assertTrue(is_image_editing_model("gemini",
                                               "gemini-3.1-flash-image"))
        self.assertFalse(is_image_editing_model("gemini",
                                                "gemini-3.1-flash-lite-image"))

    def test_registry_configures_image_models_separately_from_chat(self):
        c = _cfg(GEMINI_MODELS="gemini-3.7-flash",
                 GEMINI_IMAGE_MODELS="gemini-3.1-flash-image,"
                                     "gemini-2.5-flash-image")
        reg = ModelRegistry(c)
        ids = {m.model_id for m in reg.image_models()}
        self.assertEqual(ids, {"gemini-3.1-flash-image",
                               "gemini-2.5-flash-image"})
        self.assertNotIn("gemini-3.7-flash", ids)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Classification — English, Bangla, Banglish; vision vs image_generation
# ═══════════════════════════════════════════════════════════════════════════
class TestImageRequestClassification(unittest.TestCase):
    GEN = [
        "akta cat photo create kore dao",
        "ekta chobi baniye dao",
        "amar jonno logo bana",
        "photo generate koro",
        "generate an image of a sunset",
        "create a photo of a cat",
        "make a picture",
        "create a logo",
        "\u098f\u0995\u099f\u09be \u099b\u09ac\u09bf \u09ac\u09be\u09a8\u09be\u0993",
    ]
    EDIT = [
        "ei photo ta edit kore dao",
        "edit this photo",
        "retouch this image",
    ]
    VISION = [
        "describe this screenshot",
        "what is in this image?",
        "analyze this photo",
    ]

    def test_classify_image_generation_both_paths(self):
        for text in self.GEN:
            self.assertEqual(classify(text), "image_generation", text)
            self.assertEqual(classify_gateway_request(text),
                             "image_generation", text)

    def test_classify_image_editing_both_paths(self):
        for text in self.EDIT:
            self.assertEqual(classify(text), "image_editing", text)
            self.assertEqual(classify_gateway_request(text), "image_editing",
                             text)

    def test_vision_requests_stay_vision(self):
        for text in self.VISION:
            self.assertNotIn(classify(text),
                             ("image_generation", "image_editing"), text)
            self.assertEqual(classify_gateway_request(text), "vision", text)

    def test_ordinary_text_is_not_image(self):
        for text in ("python code likhe dao - ekta reverse string function",
                     "what is the weather today",
                     "explain bitcoin halving"):
            self.assertNotIn(classify(text),
                             ("image_generation", "image_editing"))
            self.assertNotIn(classify_gateway_request(text),
                             ("image_generation", "image_editing"))

    def test_gateway_request_categories_include_image_first_class(self):
        self.assertIn("image_generation", REQUEST_CATEGORIES)
        self.assertIn("image_editing", REQUEST_CATEGORIES)
        self.assertEqual(CATEGORY_HARD_CAPS["image_generation"],
                         ("image_generation",))
        self.assertEqual(CATEGORY_HARD_CAPS["image_editing"],
                         ("image_editing",))


# ═══════════════════════════════════════════════════════════════════════════
# 3. Eligible-target filtering (static, no execution)
# ═══════════════════════════════════════════════════════════════════════════
class TestEligibleImageTargets(unittest.TestCase):
    def _catalog(self):
        conn = _FakeConn("astra-gw-gemini", "gemini",
                         image_models=["gemini-2.5-flash-image"])
        text_conn = _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])
        vision_conn = _FakeConn("astra-gw-cohere", "cohere",
                                models=["command-a-vision-07-2025"])
        return build_gateway_catalog([conn, text_conn, vision_conn],
                                     include_image_models=True)

    def test_only_image_models_are_eligible(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        ids = {m.model_id for _c, m, _h in targets}
        self.assertEqual(ids, {"gemini-2.5-flash-image"})

    def test_text_and_vision_models_fail_the_hard_gate(self):
        vision = Model("cohere", "command-a-vision-07-2025",
                       capabilities=["chat", "vision"],
                       output_modalities=["text"])
        text = Model("groq", "llama-70b", capabilities=["chat"],
                     output_modalities=["text"])
        for m in (vision, text):
            self.assertFalse(meets_gateway_requirements(
                m, category="image_generation"), m.model_id)

    def test_image_model_without_image_output_modality_is_rejected(self):
        m = Model("gemini", "gemini-3.1-flash-image",
                  capabilities=["chat", "image_generation"],
                  output_modalities=["text"])
        self.assertFalse(meets_gateway_requirements(
            m, category="image_generation"))

    def test_ranking_never_puts_a_text_model_above_an_image_model(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        ranked = rank_targets(targets, category="image_generation")
        self.assertTrue(ranked)
        self.assertTrue(all(m.has("image_generation")
                            for _c, m, _h in ranked))

    def test_describe_image_targets_reports_health_and_modalities(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        rows = describe_image_targets(targets)
        self.assertEqual(rows[0]["model"], "gemini-2.5-flash-image")
        self.assertIn("image", rows[0]["output_modalities"])
        self.assertTrue(rows[0]["healthy"])

    def test_cooldown_excludes_a_target_and_expiry_restores_it(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        state = GatewayRoutingState(None)
        catalog = self._catalog()
        self.assertEqual(
            len(eligible_image_generation_targets(catalog, state)), 1)
        state.record_failure("gemini", "gemini-2.5-flash-image")
        self.assertEqual(
            eligible_image_generation_targets(catalog, state), [])
        state.get_health(
            "gemini", "gemini-2.5-flash-image").cooldown_until = time.time() - 1
        self.assertEqual(
            len(eligible_image_generation_targets(catalog, state)), 1)

    def test_disabled_model_is_not_eligible(self):
        from astra.ai.gateway_routing import GatewayRoutingState
        conn = _FakeConn("astra-gw-gemini", "gemini",
                         image_models=["gemini-2.5-flash-image"])
        catalog = build_gateway_catalog([conn], include_image_models=True)
        catalog[0][1].disabled = True
        state = GatewayRoutingState(None)
        self.assertEqual(
            eligible_image_generation_targets(catalog, state), [])

    def test_catalog_skips_an_image_model_the_registry_does_not_recognize(self):
        conn = _FakeConn("astra-gw-groq", "groq",
                         image_models=["totally-fake-image-9"])
        catalog = build_gateway_catalog([conn], include_image_models=True)
        self.assertEqual(catalog, [])


# ═══════════════════════════════════════════════════════════════════════════
# 4. Gateway image execution + failover (A-I)
# ═══════════════════════════════════════════════════════════════════════════
class TestGatewayImageFailover(unittest.TestCase):
    def _gateway(self, *conns):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns))

    def _gem(self, outcomes=None, image_models=("gemini-3.1-flash-image",)):
        return _FakeConn("astra-gw-gemini", "gemini",
                         image_models=list(image_models), outcomes=outcomes)

    def _cf(self, outcomes=None):
        return _FakeConn("astra-gw-cloudflare", "cloudflare",
                         image_models=["@cf/black-forest-labs/flux-1-schnell"],
                         outcomes=outcomes)

    def _or(self, outcomes=None):
        return _FakeConn("astra-gw-openrouter", "openrouter",
                         image_models=["google/gemini-3.1-flash-image"],
                         outcomes=outcomes)

    # A. success -> the first target is used
    def test_a_first_target_success(self):
        gem = self._gem()
        gw = self._gateway(gem, self._cf())
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, "gemini-3.1-flash-image")
        self.assertEqual(gem.image_calls[0][0], "gemini-3.1-flash-image")
        self.assertEqual(gem.chat_calls, 0)

    # B. 429 -> cooldown A -> B used
    def test_b_rate_limit_fails_over(self):
        gem = self._gem(outcomes=[ProviderError("gemini rate limit reached")])
        cf = self._cf()
        gw = self._gateway(gem, cf)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:"))
        self.assertEqual(gw.last_model, "@cf/black-forest-labs/flux-1-schnell")
        h = gw.routing_state.get_health("gemini", "gemini-3.1-flash-image")
        self.assertFalse(h.healthy)
        self.assertGreaterEqual(h.consecutive_failures, 1)

    # C. timeout -> B used
    def test_c_timeout_fails_over(self):
        gem = self._gem(outcomes=[TimeoutError("gemini timed out")])
        gw = self._gateway(gem, self._cf())
        gw.generate_image("a cat", discover=False)
        self.assertEqual(gw.last_model,
                         "@cf/black-forest-labs/flux-1-schnell")

    # D. 500, then 429, then success -> the third RANKED target is used
    def test_d_multi_failure_chain_reaches_third_target(self):
        gem = self._gem(outcomes=[ProviderError("gemini provider error")])
        orr = self._or(outcomes=[ProviderError("openrouter rate limit")])
        cf = self._cf()
        gw = self._gateway(gem, cf, orr)
        # ranking is capability/quality driven: gemini, openrouter, cloudflare
        ranked = [t[1].model_id for t in gw.image_targets(discover=False)]
        self.assertEqual(ranked, ["gemini-3.1-flash-image",
                                  "google/gemini-3.1-flash-image",
                                  "@cf/black-forest-labs/flux-1-schnell"])
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:"))
        self.assertEqual(gw.last_model, "@cf/black-forest-labs/flux-1-schnell")
        self.assertEqual(gw.last_attempts, 3)
        self.assertEqual(cf.image_calls[0][0],
                         "@cf/black-forest-labs/flux-1-schnell")

    # E. every image target fails -> clear error
    def test_e_all_targets_fail_raises_clear_error(self):
        gem = self._gem(outcomes=[ProviderError("boom")])
        cf = self._cf(outcomes=[ProviderError("boom")])
        gw = self._gateway(gem, cf)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("all image-generation targets failed",
                      str(ctx.exception))
        for mid, prov in (
                ("gemini-3.1-flash-image", "gemini"),
                ("@cf/black-forest-labs/flux-1-schnell", "cloudflare")):
            self.assertFalse(gw.routing_state.get_health(prov, mid).healthy)

    # F. only vision/text models exist -> no attempt, no text fallback
    def test_f_no_image_model_never_falls_back_to_text(self):
        text = _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])
        vision = _FakeConn("astra-gw-cohere", "cohere",
                           models=["command-a-vision-07-2025"])
        gw = self._gateway(text, vision)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("No image-generation model is currently configured",
                      str(ctx.exception))
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(vision.chat_calls, 0)
        self.assertEqual(text.image_calls, [])

    # G. text model healthy, image model fails -> text model never used
    def test_g_text_model_is_never_a_fallback_for_a_failed_image_model(self):
        gem = self._gem(outcomes=[ProviderError("boom")])
        text = _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])
        gw = self._gateway(gem, text)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(text.image_calls, [])

    # H. two credentials, same provider: first 429, second works
    def test_h_credential_rotation_within_one_target(self):
        from astra.ai.gateway import AstraGatewayGemini
        cfg = _cfg(GW_GEMINI_API_KEYS="key-one,key-two",
                   GW_GEMINI_IMAGE_MODELS="gemini-3.1-flash-image")
        conn = AstraGatewayGemini(config=cfg)
        calls = {"n": 0}

        def side(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(req.full_url, 429)
            return _resp(_gemini_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model="gemini-3.1-flash-image")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertGreaterEqual(calls["n"], 2)

    # I. a cooled-down image model becomes eligible again after expiry
    def test_i_cooldown_expiry_makes_target_eligible_again(self):
        gem = self._gem(outcomes=[ProviderError("rate limit")])
        gw = self._gateway(gem)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(gw.image_targets(discover=False), [])
        gw.routing_state.get_health(
            "gemini", "gemini-3.1-flash-image").cooldown_until = time.time() - 1
        self.assertEqual(
            [m.model_id for _c, m, _h in gw.image_targets(discover=False)],
            ["gemini-3.1-flash-image"])

    def test_fallback_events_are_emitted_for_the_activity_log(self):
        from astra.core.events import EventBus
        from astra.store import Store
        bus = EventBus(Store(":memory:"))
        gem = self._gem(outcomes=[ProviderError("gemini rate limit reached")])
        cf = self._cf()
        from astra.ai.gateway import AstraAIGateway
        gw = AstraAIGateway(connections=[gem, cf], events=bus)
        gw.generate_image("a cat", discover=False)
        rows = bus.history(limit=50)
        kinds = [e["kind"] for e in rows]
        self.assertIn("astra_gateway.image_request", kinds)
        self.assertIn("astra_gateway.image_failure", kinds)
        self.assertIn("astra_gateway.image_fallback", kinds)
        self.assertIn("astra_gateway.image_success", kinds)
        fb = [e for e in rows
              if e["kind"] == "astra_gateway.image_fallback"][0]
        self.assertEqual(fb["data"]["reason"], "429 rate limit")
        self.assertEqual(fb["data"]["next_model"],
                         "@cf/black-forest-labs/flux-1-schnell")

    def test_explicit_model_preference_is_soft(self):
        gem = self._gem()
        cf = self._cf()
        gw = self._gateway(gem, cf)
        # asking for the text model must NOT send the image request to it
        gw.generate_image("a cat", model="llama-70b", discover=False)
        self.assertIn(gw.last_model, {"gemini-3.1-flash-image",
                                      "@cf/black-forest-labs/flux-1-schnell"})

    def test_editing_request_only_uses_editing_capable_models(self):
        gen_only = _FakeConn("astra-gw-gemini", "gemini",
                             image_models=["gemini-3.1-flash-lite-image"])
        editor = _FakeConn("astra-gw-openrouter", "openrouter",
                           image_models=["google/gemini-3.1-flash-image"])
        gw = self._gateway(gen_only, editor)
        gw.generate_image("edit this photo", editing=True, discover=False)
        self.assertEqual(gw.last_model, "google/gemini-3.1-flash-image")
        self.assertEqual(gen_only.image_calls, [])


# ═══════════════════════════════════════════════════════════════════════════
# 5. Real provider image APIs (mocked HTTP contracts)
# ═══════════════════════════════════════════════════════════════════════════
class TestProviderImageAPIContracts(unittest.TestCase):
    def test_gemini_image_uses_native_generate_content(self):
        from astra.ai.adapters.gemini import GeminiAdapter
        conn = GeminiAdapter(config=_cfg(
            GEMINI_API_KEYS="k",
            GEMINI_IMAGE_MODELS="gemini-3.1-flash-image"))
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            seen["key"] = req.headers.get("X-goog-api-key")
            return _resp(_gemini_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model="gemini-3.1-flash-image")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertIn(":generateContent", seen["url"])
        self.assertEqual(seen["body"]["generationConfig"]["responseModalities"],
                         ["IMAGE"])
        self.assertEqual(seen["key"], "k")

    def test_cloudflare_image_uses_ai_run_endpoint(self):
        from astra.ai.adapters.cloudflare import CloudflareAdapter
        conn = CloudflareAdapter(config=_cfg(
            CLOUDFLARE_API_KEYS="k", CLOUDFLARE_ACCOUNT_IDS="acct1",
            CLOUDFLARE_IMAGE_MODELS="@cf/black-forest-labs/flux-1-schnell"))
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_cf_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image(
                "a cat", model="@cf/black-forest-labs/flux-1-schnell")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertIn(
            "/accounts/acct1/ai/run/@cf/black-forest-labs/flux-1-schnell",
            seen["url"])
        self.assertEqual(seen["body"]["prompt"], "a cat")

    def test_openrouter_and_zai_use_images_generations(self):
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        from astra.ai.adapters.zai import ZAIAdapter
        for cls, key_env in ((OpenRouterAdapter, "OPENROUTER_API_KEYS"),
                             (ZAIAdapter, "ZAI_API_KEYS")):
            conn = cls(config=_cfg(**{key_env: "k"}))
            seen = {}

            def side(req, timeout=None, _s=seen):
                _s["url"] = req.full_url
                return _resp(_openai_images_ok())

            model = ("glm-image" if cls is ZAIAdapter
                     else "google/gemini-2.5-flash-image")
            with mock.patch("urllib.request.urlopen", side):
                out = conn.generate_image("a cat", model=model)
            self.assertTrue(out.startswith("data:image/png;base64,"))
            self.assertTrue(seen["url"].endswith("/images/generations"),
                            seen["url"])

    def test_bedrock_rejects_a_non_image_model(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        conn = BedrockAdapter(config=_cfg(
            BEDROCK_API_KEYS="k",
            BEDROCK_IMAGE_MODELS="amazon.nova-canvas-v1:0"))
        with self.assertRaises(ProviderError) as ctx:
            conn.generate_image("a cat", model="us.anthropic.claude-opus-4-5")
        self.assertIn("is not an image-generation model", str(ctx.exception))

    def test_bedrock_nova_canvas_body_and_invoke_url(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        conn = BedrockAdapter(config=_cfg(
            BEDROCK_API_KEYS="k",
            BEDROCK_IMAGE_MODELS="amazon.nova-canvas-v1:0"))
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(json.dumps({"images": [B64]}))

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model="amazon.nova-canvas-v1:0")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertIn("/model/amazon.nova-canvas-v1:0/invoke", seen["url"])
        self.assertEqual(seen["body"]["taskType"], "TEXT_IMAGE")
        self.assertEqual(seen["body"]["textToImageParams"]["text"], "a cat")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Provider router: image dispatch only, with failover
# ═══════════════════════════════════════════════════════════════════════════
class TestRouterImageDispatch(unittest.TestCase):
    def test_router_routes_image_request_to_image_model_only(self):
        from astra.ai.router import AstraRouter
        text = _FakeConn("groq", "groq", models=["llama-70b"])
        img = _FakeConn("gemini", "gemini",
                        image_models=["gemini-2.5-flash-image"])
        router = AstraRouter([text, img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user",
                       "content": "akta cat photo create kore dao"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "gemini")
        self.assertTrue(rr.text.startswith("data:image/png;base64,"))
        self.assertEqual(text.chat_calls, 0)

    def test_router_image_failover_to_the_next_image_model(self):
        from astra.ai.router import AstraRouter
        bad = _FakeConn("gemini", "gemini",
                        image_models=["gemini-2.5-flash-image"],
                        outcomes=[ProviderError("gemini rate limit reached")])
        good = _FakeConn("zai", "zai", image_models=["glm-image"])
        router = AstraRouter([bad, good], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": "photo generate koro"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.model, "glm-image")


    def test_bedrock_stable_image_uses_its_own_schema(self):
        """Stability Stable Image Core/Ultra must NOT get the SDXL body."""
        from astra.ai.adapters.bedrock import BedrockAdapter
        conn = BedrockAdapter(config=_cfg(
            BEDROCK_API_KEYS="k",
            BEDROCK_IMAGE_MODELS="stability.stable-image-core-v1:1"))
        seen = {}

        def side(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _resp(json.dumps({"images": [B64]}))

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image(
                "a cat", model="stability.stable-image-core-v1:1")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(seen["body"]["prompt"], "a cat")
        self.assertEqual(seen["body"]["mode"], "text-to-image")
        self.assertEqual(seen["body"]["aspect_ratio"], "1:1")
        self.assertNotIn("text_prompts", seen["body"])

# ═══════════════════════════════════════════════════════════════════════════
# 6b. OpenRouter: LIVE discovery is authoritative (spec section 11)
# ═══════════════════════════════════════════════════════════════════════════
class TestOpenRouterLiveDiscovery(unittest.TestCase):
    def _payload(self, ids):
        return json.dumps({"data": [
            {"id": mid, "architecture": {"output_modalities": ["image"]}}
            for mid in ids]})

    def test_gateway_discovery_adds_an_unlisted_image_model(self):
        from astra.ai.gateway import AstraAIGateway, AstraGatewayOpenRouter
        conn = AstraGatewayOpenRouter(
            config=_cfg(GW_OPENROUTER_API_KEYS="k"))
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _resp(
                            self._payload(["black-forest-labs/flux-2-pro"]))):
            self.assertIn("black-forest-labs/flux-2-pro",
                          conn.list_image_models(discover=True))
            cat = AstraAIGateway(connections=[conn])._image_catalog(
                discover=True)
        self.assertEqual([m.model_id for _c, m in cat],
                         ["black-forest-labs/flux-2-pro"])
        self.assertIn("image_generation", cat[0][1].capabilities)
        self.assertIn("image", cat[0][1].output_modalities)

    def test_gateway_discovery_failure_invents_nothing(self):
        from astra.ai.gateway import AstraGatewayOpenRouter
        conn = AstraGatewayOpenRouter(
            config=_cfg(GW_OPENROUTER_API_KEYS="k"))
        def boom(req, timeout=None):
            raise OSError("offline")
        with mock.patch("urllib.request.urlopen", boom):
            self.assertEqual(conn.list_image_models(discover=True), [])
            self.assertEqual(conn.live_image_models(discover=True), [])

    def test_router_dispatches_to_a_live_discovered_image_model(self):
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        from astra.ai.router import AstraRouter
        conn = OpenRouterAdapter(config=_cfg(OPENROUTER_API_KEYS="k"))
        seen = {}

        def side(req, timeout=None):
            url = req.full_url
            if "output_modalities=image" in url:
                return _resp(self._payload(["black-forest-labs/flux-2-pro"]))
            seen["url"] = url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_openai_images_ok())

        with mock.patch("urllib.request.urlopen", side):
            router = AstraRouter([conn], max_retries=0)
            rr = router.route_request(RoutingRequest(
                task_type="image_generation",
                messages=[{"role": "user", "content": "photo generate koro"}]))
            self.assertTrue(rr.ok, rr.error)
            self.assertEqual(rr.model, "black-forest-labs/flux-2-pro")
            self.assertEqual(seen["body"]["model"], "black-forest-labs/flux-2-pro")
            self.assertTrue(seen["url"].endswith("/images/generations"))


# ═══════════════════════════════════════════════════════════════════════════
# 7. Artifacts + frontend rendering
# ═══════════════════════════════════════════════════════════════════════════
class TestImageArtifactsAndFrontend(unittest.TestCase):
    def test_data_uri_becomes_a_validated_image_artifact(self):
        from astra.ai.artifact_extraction import extract_artifacts
        with tempfile.TemporaryDirectory() as d:
            arts = extract_artifacts(DATA_URI, d, "image")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["validated"])
        self.assertTrue(arts[0]["id"])
        self.assertTrue(arts[0]["filename"].endswith(".png"))

    def test_frontend_renders_preview_open_and_download(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js = open(os.path.join(root, "static", "js", "astra.js"),
                  encoding="utf-8").read()
        self.assertIn("renderArtifact", js)
        self.assertIn("artifact_type", js)
        self.assertIn("artifact-image", js)
        self.assertIn(">Open<", js)
        self.assertIn(">Download<", js)
        self.assertIn("a.mime_type", js)

    def test_artifact_mime_type_is_image_png(self):
        from astra.core.artifacts import store_artifact, validate_artifact
        with tempfile.TemporaryDirectory() as d:
            art = store_artifact(PNG, "generated.png", "image", d)
            ok, _ = validate_artifact(art)
            self.assertTrue(ok)
            self.assertEqual(art.mime_type, "image/png")
            self.assertEqual(art.to_dict()["artifact_type"], "image")


# ═══════════════════════════════════════════════════════════════════════════
# 8. Chat pipeline wiring: image in, artifact out, never simple_chat
# ═══════════════════════════════════════════════════════════════════════════
class TestPipelineImageWiring(unittest.TestCase):
    """Uses the real ChatPipeline with a scripted Gateway + scripted router."""

    class _Gateway:
        def __init__(self, usable=True):
            self.usable = usable
            self.calls = []
            self.categories = []
            self.last_model = ""
            self.image_calls = []

        def is_usable(self):
            return self.usable

        def chat(self, messages, max_tokens=None, category=None, trace=""):
            self.calls.append(messages)
            self.categories.append(category)
            return ('{"final_request": "x", "was_incomplete": false, '
                    '"provider": "", "model": "", "criteria": [], "reason": "", '
                    '"execution": {"required": false, "capability": "", '
                    '"environment": "agent_runtime", "approval_required": '
                    'false, "intent": ""}}')

        def supervise_task(self, *a, **kw):  # pragma: no cover
            raise AssertionError("image turns must not be semantically verified")

        def generate_image(self, prompt, model=None, size="1024x1024", n=1, *,
                           editing=False, trace=""):
            self.image_calls.append((prompt, model, editing))
            self.last_model = "gemini-2.5-flash-image"
            return DATA_URI

    class _Router:
        def __init__(self, text="should not be used"):
            self.requests = []
            self.text = text

        def available_targets(self):
            return [{"provider": "gemini", "model": "gemini-2.5-flash-image"}]

        def route_request(self, req):
            from astra.ai.router import RoutingResult
            self.requests.append(req)
            return RoutingResult(ok=True, text=self.text, provider="gemini",
                                 model="gemini-2.5-flash-image")

    def test_image_request_goes_through_gateway_image_path(self):
        from astra.ai.chat_pipeline import ChatPipeline
        gw, rt = self._Gateway(), self._Router()
        out = ChatPipeline(gw, rt).run("akta cat photo create kore dao")
        self.assertTrue(out["ok"])
        self.assertEqual(gw.image_calls[0][0], "akta cat photo create kore dao")
        self.assertEqual(rt.requests, [])          # router not used
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertNotIn("base64,", out["reply"])

    def test_edit_request_uses_editing_mode(self):
        from astra.ai.chat_pipeline import ChatPipeline
        gw, rt = self._Gateway(), self._Router()
        ChatPipeline(gw, rt).run("ei photo ta edit kore dao")
        self.assertTrue(gw.image_calls[0][2], "editing flag must be set")

    def test_gateway_without_image_path_falls_back_to_router(self):
        from astra.ai.chat_pipeline import ChatPipeline
        gw, rt = self._Gateway(), self._Router()
        gw.generate_image = None                   # no Gateway image path
        out = ChatPipeline(gw, rt).run("photo generate koro")
        self.assertTrue(out["ok"])
        self.assertEqual(len(rt.requests), 1)
        self.assertEqual(rt.requests[0].task_type, "image_generation")
        self.assertEqual(rt.requests[0].required_output_modalities, ["image"])

    def test_no_image_model_gives_clear_error_and_no_text_answer(self):
        from astra.ai.chat_pipeline import ChatPipeline
        from astra.ai.router import RoutingResult
        gw, rt = self._Gateway(), self._Router()

        def fail(req):
            rt.requests.append(req)
            return RoutingResult(ok=False,
                                 error="no eligible provider/model available")
        rt.route_request = fail
        gw.generate_image = None
        out = ChatPipeline(gw, rt).run("akta cat photo create kore dao")
        self.assertFalse(out["ok"])
        self.assertIn("No image-generation model is currently configured",
                      out["reply"])
        self.assertEqual(len(rt.requests), 1)       # no simple_chat retry
        self.assertEqual(rt.requests[0].task_type, "image_generation")

    def test_gateway_image_error_falls_back_to_router_not_to_text_chat(self):
        from astra.ai.chat_pipeline import ChatPipeline
        gw, rt = self._Gateway(), self._Router()

        def boom(*a, **kw):
            raise ProviderError("No image-generation model is currently "
                                "configured or available.")
        gw.generate_image = boom
        ChatPipeline(gw, rt).run("akta cat photo create kore dao")
        self.assertEqual(len(rt.requests), 1)
        self.assertEqual(rt.requests[0].task_type, "image_generation")


# ═══════════════════════════════════════════════════════════════════════════
# 8b. End-to-end turn: real ChatPipeline + real AstraAIGateway, provider
#     HTTP mocked at the adapter boundary (no image credentials in CI).
# ═══════════════════════════════════════════════════════════════════════════
class TestEndToEndImageTurn(unittest.TestCase):
    _BRIEF = ('{"final_request": "x", "was_incomplete": false, '
              '"provider": "", "model": "", "criteria": [], "reason": "", '
              '"execution": {"required": false, "capability": "", '
              '"environment": "agent_runtime", "approval_required": false, '
              '"intent": ""}}')

    def _run(self, text, *conns):
        from astra.ai.chat_pipeline import ChatPipeline
        from astra.ai.gateway import AstraAIGateway
        real = AstraAIGateway(connections=list(conns))

        class Gw:
            def __init__(self):
                self.image_calls = []
                self.last_model = ""

            def is_usable(self):
                return True

            def chat(self, messages, max_tokens=None, category=None, trace=""):
                return self._BRIEF

            def supervise_task(self, *a, **kw):  # pragma: no cover
                raise AssertionError("image turns must not be verified")

            def generate_image(self, prompt, model=None, size="1024x1024",
                               n=1, *, editing=False, trace=""):
                self.image_calls.append((prompt, model, editing))
                uri = real.generate_image(prompt, model=model, size=size, n=n,
                                          editing=editing, trace=trace,
                                          discover=False)
                self.last_model = real.last_model
                return uri

        rt = TestPipelineImageWiring._Router()
        gw = Gw()
        out = ChatPipeline(gw, rt).run(text)
        return out, gw, real, rt

    def test_banglish_cat_photo_fails_over_and_yields_an_artifact(self):
        # Gemini image model 429s; Cloudflare FLUX succeeds. The turn must
        # still produce a real image artifact and never touch a text model.
        gem = _FakeConn("astra-gw-gemini", "gemini",
                        image_models=["gemini-3.1-flash-image"],
                        outcomes=[ProviderError("gemini rate limit reached")])
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=["@cf/black-forest-labs/flux-1-schnell"])
        out, gw, real, rt = self._run("akta cat photo create kore dao", gem, cf)
        self.assertTrue(out["ok"], out.get("reply"))
        self.assertEqual(rt.requests, [])              # router never used
        self.assertEqual(gw.image_calls[0][0], "akta cat photo create kore dao")
        self.assertEqual(gw.last_model, "@cf/black-forest-labs/flux-1-schnell")
        self.assertEqual(gem.image_calls[0][0], "gemini-3.1-flash-image")
        self.assertEqual(cf.image_calls[0][0],
                         "@cf/black-forest-labs/flux-1-schnell")
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["mime_type"].startswith("image/"))
        self.assertNotIn("base64,", out["reply"])
        # the gateway only ever considered image-capable targets
        health = real.routing_state.snapshot()["model_health"]
        self.assertIn("gemini:gemini-3.1-flash-image", health)


if __name__ == "__main__":
    unittest.main()
