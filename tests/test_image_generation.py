# -*- coding: utf-8 -*-
"""Image-generation architecture: the FREE-only capability registry, Gateway
eligible-target filtering, real provider image APIs, per-(provider, model)
failover and artifact output.

Every test here is either a pure decision-logic test or a mocked
provider-contract test (urllib is patched). No test performs a real network
call, and no test claims image generation works unless a provider image API
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

from astra.ai.gateway_routing import (
    CATEGORY_HARD_CAPS, CATEGORY_REQUIRED_OUTPUT_MODALITY, REQUEST_CATEGORIES,
    GatewayRoutingState, build_gateway_catalog, classify_gateway_request,
    describe_image_targets, eligible_image_generation_targets,
    meets_gateway_requirements, rank_targets)
from astra.ai.image_models import (
    FREE_FALSE, FREE_IMAGE_PROVIDERS, FREE_TRUE, FREE_UNKNOWN, IMAGE_EDITING,
    IMAGE_GENERATION, REJECTED_IMAGE_MODELS, documented_image_models,
    image_pool, image_spec, is_free_image_model, is_image_editing_model,
    is_image_model, provider_supports_image_generation, rejected_image_reason)
from astra.ai.models import Model, ModelRegistry, metadata_for
from astra.ai.router import AstraRouter, RoutingRequest, classify
from astra.core.config import Config
from astra.core.exceptions import ProviderError, TimeoutError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
B64 = base64.b64encode(PNG).decode()
DATA_URI = "data:image/png;base64," + B64

FLUX = "@cf/black-forest-labs/flux-1-schnell"
LIGHTNING = "@cf/bytedance/stable-diffusion-xl-lightning"
SDXL = "@cf/stabilityai/stable-diffusion-xl-base-1.0"
LUCID = "@cf/leonardo/lucid-origin"
PHOENIX = "@cf/leonardo/phoenix-1.0"
DREAM = "@cf/lykon/dreamshaper-8-lcm"
INPAINT = "@cf/runwayml/stable-diffusion-v1-5-inpainting"
CF_POOL = (FLUX, SDXL, LIGHTNING, DREAM, INPAINT, LUCID, PHOENIX)
LLAMA = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


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


def _http_error(url, code, reason="err"):
    return urllib.error.HTTPError(url, code, reason, {}, None)


class _FakeConn:
    """Minimal Gateway/adaptor double: counts calls, scripted outcomes."""

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

    def live_image_models(self, *, discover=True):
        return []

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
# 1. FREE image pool — evidence, not model-name guessing
# ═══════════════════════════════════════════════════════════════════════════
class TestFreeImagePool(unittest.TestCase):
    def test_pool_is_the_single_verified_free_provider(self):
        self.assertEqual(set(FREE_IMAGE_PROVIDERS), {"cloudflare"})
        self.assertEqual({p for p, _m in image_pool()}, {"cloudflare"})

    def test_every_pool_model_is_free_with_quotable_evidence(self):
        for provider, mid in image_pool():
            spec = image_spec(provider, mid)
            self.assertIsNotNone(spec, mid)
            self.assertEqual(spec.free_tier, FREE_TRUE, mid)
            self.assertTrue(spec.is_free, mid)
            self.assertTrue(spec.free_evidence, mid)
            self.assertIn("neurons", spec.free_evidence.lower(), mid)

    def test_every_pool_model_really_declares_image_generation(self):
        for provider, mid in image_pool():
            spec = image_spec(provider, mid)
            self.assertIn(IMAGE_GENERATION, spec.capabilities, mid)
            self.assertIn("image", spec.output_modalities, mid)
            self.assertIn(IMAGE_GENERATION,
                          metadata_for(mid, provider)["capabilities"], mid)
            self.assertIn("image",
                          metadata_for(mid, provider)["output_modalities"], mid)

    def test_no_pool_model_advertises_image_editing(self):
        # No Astra adapter forwards a source image to an image API, so
        # advertising editing would be a lie: every kept model is
        # generation-only.
        for provider, mid in image_pool():
            self.assertFalse(is_image_editing_model(provider, mid), mid)
            self.assertNotIn(IMAGE_EDITING,
                             image_spec(provider, mid).capabilities, mid)

    def test_cloudflare_pool_is_the_documented_text_to_image_set(self):
        self.assertEqual(set(documented_image_models("cloudflare")),
                         set(CF_POOL))
        for mid in CF_POOL:
            self.assertTrue(is_image_model("cloudflare", mid), mid)
        self.assertFalse(is_image_model("cloudflare", LLAMA))

    def test_gemini_image_models_are_paid_only_and_rejected(self):
        for mid in ("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image",
                    "gemini-3-pro-image"):
            self.assertFalse(is_image_model("gemini", mid), mid)
            self.assertNotIn("image",
                             metadata_for(mid, "gemini")["output_modalities"])
            self.assertIn("paid-only", rejected_image_reason("gemini", mid))

    def test_gemini_2_5_flash_image_is_deprecated_and_paid(self):
        self.assertFalse(is_image_model("gemini", "gemini-2.5-flash-image"))
        reason = rejected_image_reason("gemini", "gemini-2.5-flash-image")
        self.assertIn("deprecated", reason)
        self.assertIn("paid-only", reason)

    def test_gemini_vision_and_preview_ids_do_not_leak_into_the_pool(self):
        for mid in ("gemini-2.5-flash-image-preview", "gemini-2.5-flash",
                    "gemini-3-pro-image-preview", "gemini-3.7-flash"):
            self.assertFalse(is_image_model("gemini", mid), mid)

    def test_bedrock_image_models_are_paid_only_and_rejected(self):
        for mid in ("amazon.nova-canvas-v1:0",
                    "amazon.titan-image-generator-v2:0",
                    "stability.stable-diffusion-xl-v1",
                    "stability.stable-image-core-v1:1"):
            self.assertFalse(is_image_model("bedrock", mid), mid)
            self.assertIn("paid-only", rejected_image_reason("bedrock", mid))

    def test_zai_image_models_are_paid_only_and_rejected(self):
        for mid in ("glm-image", "cogview-4", "cogview-4-250304"):
            self.assertFalse(is_image_model("zai", mid), mid)
            self.assertIn("paid-only", rejected_image_reason("zai", mid))

    def test_openrouter_free_candidates_do_not_exist_and_are_rejected(self):
        for mid in ("google/gemini-2.5-flash-image-preview:free",
                    "black-forest-labs/flux-1-schnell:free",
                    "sourceful/riverflow-v2.5-pro:free"):
            self.assertFalse(is_image_model("openrouter", mid), mid)
            self.assertIn("not in the current",
                          rejected_image_reason("openrouter", mid))

    def test_openrouter_paid_image_models_are_rejected(self):
        for mid in ("google/gemini-2.5-flash-image", "openai/gpt-5-image"):
            self.assertFalse(is_image_model("openrouter", mid), mid)
            self.assertIn("no free model",
                          rejected_image_reason("openrouter", mid))

    def test_stale_or_adapter_incompatible_cloudflare_ids_are_rejected(self):
        self.assertIn("not in the current Workers AI catalog",
                      rejected_image_reason(
                          "cloudflare",
                          "@cf/runwayml/stable-diffusion-v1-5-img2img"))
        for mid in ("@cf/black-forest-labs/flux-2-dev",
                    "@cf/black-forest-labs/flux-2-klein-4b",
                    "@cf/black-forest-labs/flux-2-klein-9b"):
            self.assertFalse(is_image_model("cloudflare", mid), mid)
            self.assertIn("multipart",
                          rejected_image_reason("cloudflare", mid))

    def test_vision_models_are_not_image_generators(self):
        for provider, mid in (("bedrock", "us.anthropic.claude-opus-4-5"),
                              ("cohere", "command-a-vision-07-2025"),
                              ("gemini", "gemini-3.7-flash")):
            self.assertFalse(is_image_model(provider, mid), mid)
            meta = metadata_for(mid, provider)
            self.assertNotIn(IMAGE_GENERATION, meta["capabilities"], mid)
            self.assertNotIn("image", meta["output_modalities"], mid)

    def test_omni_or_multimodal_names_do_not_grant_image_generation(self):
        for provider, mid in (
                ("openrouter",
                 "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
                ("groq", "openai/gpt-oss-120b"),
                ("gemini", "gemini-3.7-flash")):
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
            self.assertFalse(provider_supports_image_generation(provider),
                             provider)

    def test_provider_supports_image_generation_only_with_a_kept_model(self):
        self.assertTrue(provider_supports_image_generation("cloudflare"))
        for provider in ("gemini", "bedrock", "zai", "openrouter"):
            self.assertFalse(provider_supports_image_generation(provider),
                             provider)

    def test_rejection_audit_has_no_entry_that_is_also_in_the_pool(self):
        for provider, mid in REJECTED_IMAGE_MODELS:
            self.assertFalse(is_image_model(provider, mid),
                             "%s/%s is both rejected and kept" % (provider, mid))

    def test_free_tier_tristate_treats_unknown_as_not_free(self):
        self.assertNotEqual(FREE_TRUE, FREE_UNKNOWN)
        self.assertNotEqual(FREE_TRUE, FREE_FALSE)
        spec = image_spec("cloudflare", FLUX)
        self.assertEqual(spec.free_tier, FREE_TRUE)
        self.assertTrue(spec.is_free)
        self.assertFalse(is_free_image_model("gemini",
                                             "gemini-3.1-flash-image"))
        self.assertTrue(is_free_image_model("cloudflare", FLUX))

    def test_registry_seeds_image_models_separately_from_chat(self):
        c = _cfg(CLOUDFLARE_MODELS=LLAMA,
                 CLOUDFLARE_IMAGE_MODELS=FLUX + "," + LIGHTNING)
        reg = ModelRegistry(c)
        ids = {m.model_id for m in reg.image_models()}
        self.assertEqual(ids, {FLUX, LIGHTNING})
        self.assertNotIn(LLAMA, ids)

    def test_registry_never_grants_image_capability_to_a_paid_id(self):
        c = _cfg(GEMINI_IMAGE_MODELS="gemini-3.1-flash-image")
        reg = ModelRegistry(c)
        self.assertEqual(reg.image_models(), [])
        self.assertEqual({m.model_id for m in reg.for_provider("gemini")},
                         {"gemini-3.1-flash-image"})


# ═══════════════════════════════════════════════════════════════════════════
# 2. Classification — English, Bangla, Banglish; vision vs image_generation
# ═══════════════════════════════════════════════════════════════════════════
class TestImageRequestClassification(unittest.TestCase):
    GEN = [
        "akta cat photo create kore dao",
        "ekta chobi baniye dao",
        "amar jonno logo bana",
        "photo generate koro",
        "ekta photo toiri koro",
        "generate an image of a sunset",
        "create a photo of a cat",
        "make a picture",
        "create a logo",
        "\u098f\u0995\u099f\u09be \u099b\u09ac\u09bf \u09ac\u09be\u09a8\u09be\u0993",
        "\u098f\u0995\u099f\u09be photo \u09a4\u09c8\u09b0\u09bf \u0995\u09b0\u09cb",
    ]
    EDIT = [
        "ei photo ta edit kore dao",
        "edit this photo",
        "retouch this image",
        "\u098f\u0987 \u099b\u09ac\u09bf\u099f\u09be edit kore dao",
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
            self.assertEqual(classify_gateway_request(text), "vision", text)
            self.assertNotIn(classify(text),
                             ("image_generation", "image_editing"), text)

    def test_image_production_never_becomes_simple_chat(self):
        for text in self.GEN + self.EDIT:
            self.assertNotEqual(classify(text), "simple_chat", text)
            self.assertNotIn(classify_gateway_request(text),
                             ("simple", "general"), text)

    def test_ordinary_text_is_not_image(self):
        for text in ("python code likhe dao - ekta reverse string function",
                     "what is the weather today",
                     "explain bitcoin halving"):
            self.assertNotIn(classify(text),
                             ("image_generation", "image_editing"))
            self.assertNotIn(classify_gateway_request(text),
                             ("image_generation", "image_editing"))

    def test_categories_and_hard_caps_are_first_class(self):
        self.assertIn("image_generation", REQUEST_CATEGORIES)
        self.assertIn("image_editing", REQUEST_CATEGORIES)
        self.assertEqual(CATEGORY_HARD_CAPS["image_generation"],
                         ("image_generation",))
        self.assertEqual(CATEGORY_HARD_CAPS["image_editing"],
                         ("image_editing",))
        self.assertEqual(
            CATEGORY_REQUIRED_OUTPUT_MODALITY["image_generation"], "image")
        self.assertEqual(
            CATEGORY_REQUIRED_OUTPUT_MODALITY["image_editing"], "image")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Eligible-target filtering (static, no execution)
# ═══════════════════════════════════════════════════════════════════════════
class TestEligibleImageTargets(unittest.TestCase):
    def _catalog(self, *extra, models=CF_POOL):
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=list(models))
        return build_gateway_catalog([cf, *extra], include_image_models=True)

    def _text(self):
        return _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])

    def _vision(self):
        return _FakeConn("astra-gw-cohere", "cohere",
                         models=["command-a-vision-07-2025"])

    def test_only_free_image_models_are_eligible(self):
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(
            self._catalog(self._text(), self._vision()), state)
        self.assertEqual({m.model_id for _c, m, _h in targets}, set(CF_POOL))

    def test_text_and_vision_models_fail_the_hard_gate(self):
        vision = Model("cohere", "command-a-vision-07-2025",
                       capabilities=["chat", "vision"],
                       output_modalities=["text"])
        text = Model("groq", "llama-70b", capabilities=["chat"],
                     output_modalities=["text"])
        for m in (vision, text):
            self.assertFalse(meets_gateway_requirements(
                m, category="image_generation"), m.model_id)

    def test_paid_image_capable_model_is_rejected_by_the_free_gate(self):
        paid = Model("gemini", "gemini-3.1-flash-image",
                     capabilities=["chat", "image_generation"],
                     output_modalities=["text", "image"])
        self.assertFalse(meets_gateway_requirements(
            paid, category="image_generation"))
        free = Model("cloudflare", FLUX,
                     capabilities=["chat", "image_generation"],
                     output_modalities=["text", "image"])
        self.assertTrue(meets_gateway_requirements(
            free, category="image_generation"))

    def test_image_capability_without_image_output_modality_is_rejected(self):
        m = Model("cloudflare", FLUX,
                  capabilities=["chat", "image_generation"],
                  output_modalities=["text"])
        self.assertFalse(meets_gateway_requirements(
            m, category="image_generation"))

    def test_image_output_without_the_capability_is_rejected(self):
        m = Model("cloudflare", FLUX, capabilities=["chat"],
                  output_modalities=["text", "image"])
        self.assertFalse(meets_gateway_requirements(
            m, category="image_generation"))

    def test_ranking_never_surfaces_a_text_model(self):
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(
            self._catalog(self._text(), self._vision()), state)
        ranked = rank_targets(targets, category="image_generation")
        self.assertTrue(ranked)
        self.assertTrue(all(m.has("image_generation")
                            for _c, m, _h in ranked))
        self.assertEqual(ranked[0][1].model_id, LIGHTNING)

    def test_describe_image_targets_reports_health_and_modalities(self):
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        rows = describe_image_targets(targets)
        self.assertEqual(len(rows), len(CF_POOL))
        self.assertTrue(all(r["healthy"] for r in rows))
        self.assertTrue(all("image" in r["output_modalities"] for r in rows))
        self.assertEqual(rows[0]["provider"], "cloudflare")

    def test_cooldown_excludes_one_target_and_expiry_restores_it(self):
        state = GatewayRoutingState(None)
        catalog = self._catalog()
        self.assertEqual(
            len(eligible_image_generation_targets(catalog, state)),
            len(CF_POOL))
        state.record_failure("cloudflare", FLUX)
        ids = {m.model_id for _c, m, _h in
               eligible_image_generation_targets(catalog, state)}
        self.assertNotIn(FLUX, ids)
        self.assertEqual(len(ids), len(CF_POOL) - 1)
        state.get_health("cloudflare", FLUX).cooldown_until = time.time() - 1
        ids = {m.model_id for _c, m, _h in
               eligible_image_generation_targets(catalog, state)}
        self.assertIn(FLUX, ids)

    def test_a_disabled_model_is_never_eligible(self):
        state = GatewayRoutingState(None)
        catalog = self._catalog()
        for _c, m in catalog:
            if m.model_id == FLUX:
                m.disabled = True
        ids = {m.model_id for _c, m, _h in
               eligible_image_generation_targets(catalog, state)}
        self.assertNotIn(FLUX, ids)

    def test_catalog_never_invents_an_unregistered_image_model(self):
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=[FLUX,
                                     "@cf/black-forest-labs/flux-2-dev"])
        cat = build_gateway_catalog([cf], include_image_models=True)
        self.assertEqual([m.model_id for _c, m in cat], [FLUX])

    def test_no_editing_target_exists_because_no_adapter_forwards_an_image(self):
        state = GatewayRoutingState(None)
        self.assertEqual(
            eligible_image_generation_targets(self._catalog(), state,
                                              editing=True), [])


# ═══════════════════════════════════════════════════════════════════════════
# 4. AstraAIGateway image execution: A-I failover matrix (mocked providers)
# ═══════════════════════════════════════════════════════════════════════════
class TestGatewayImageFailover(unittest.TestCase):
    def _gw(self, *conns, events=None):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), events=events)

    def _cf(self, models=(FLUX,), outcomes=None, name="astra-gw-cloudflare"):
        return _FakeConn(name, "cloudflare", image_models=list(models),
                         outcomes=outcomes)

    def _text(self):
        return _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])

    def _vision(self):
        return _FakeConn("astra-gw-cohere", "cohere",
                         models=["command-a-vision-07-2025"])

    # A. first target succeeds -> it is used
    def test_a_first_target_success(self):
        cf = self._cf()
        gw = self._gw(cf)
        out = gw.generate_image("akta cat photo create kore dao",
                                discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, FLUX)
        self.assertEqual(cf.image_calls[0][0], FLUX)
        self.assertEqual(cf.chat_calls, 0)

    # B. 429 on the fastest target -> cooldown + next target
    def test_b_rate_limit_fails_over_to_the_next_image_model(self):
        cf = self._cf(models=(LIGHTNING, FLUX),
                      outcomes=[_http_error("u", 429)])
        gw = self._gw(cf)
        out = gw.generate_image("photo generate koro", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, FLUX)
        self.assertFalse(
            gw.routing_state.get_health("cloudflare", LIGHTNING).healthy)
        self.assertTrue(
            gw.routing_state.get_health("cloudflare", FLUX).healthy)

    # C. timeout on the first target -> next target
    def test_c_timeout_fails_over_to_the_next_image_model(self):
        cf = self._cf(models=(LIGHTNING, FLUX),
                      outcomes=[TimeoutError("timed out")])
        gw = self._gw(cf)
        out = gw.generate_image("ekta chobi baniye dao", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, FLUX)

    # D. 5xx then 429 then success -> third target, three attempts
    def test_d_multi_failure_chain_reaches_the_third_target(self):
        cf = self._cf(models=(LIGHTNING, FLUX, LUCID),
                      outcomes=[_http_error("u", 500),
                                _http_error("u", 429)])
        gw = self._gw(cf)
        out = gw.generate_image("create a photo of a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, LUCID)
        self.assertEqual(gw.last_attempts, 3)

    # E. every image target fails -> clear error, each target cooled down
    def test_e_all_targets_fail_raises_a_clear_error(self):
        cf = self._cf(models=(LIGHTNING, FLUX),
                      outcomes=[ProviderError("boom"), ProviderError("boom")])
        gw = self._gw(cf)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("all image-generation targets failed",
                      str(ctx.exception))
        for mid in (LIGHTNING, FLUX):
            self.assertFalse(
                gw.routing_state.get_health("cloudflare", mid).healthy, mid)

    # F. only vision/text models exist -> no attempt, no text fallback
    def test_f_no_image_model_never_falls_back_to_text(self):
        text, vision = self._text(), self._vision()
        gw = self._gw(text, vision)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("No image-generation model is currently configured",
                      str(ctx.exception))
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(vision.chat_calls, 0)
        self.assertEqual(text.image_calls, [])
        self.assertEqual(vision.image_calls, [])

    # G. text model healthy but image model fails -> text model never used
    def test_g_text_model_is_never_a_fallback_for_a_failed_image_model(self):
        cf = self._cf(outcomes=[ProviderError("boom")])
        text = self._text()
        gw = self._gw(cf, text)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(text.image_calls, [])

    # H. two credentials, same target: first 429, second works
    def test_h_credential_rotation_within_one_target(self):
        from astra.ai.gateway import AstraGatewayCloudflare
        cfg = _cfg(GW_CLOUDFLARE_API_KEYS="key-one,key-two",
                   GW_CLOUDFLARE_ACCOUNT_IDS="acct1")
        conn = AstraGatewayCloudflare(config=cfg)
        calls = {"n": 0}

        def side(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(req.full_url, 429)
            return _resp(_cf_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model=FLUX)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertGreaterEqual(calls["n"], 2)

    # I. a cooled-down image model becomes eligible again after expiry
    def test_i_cooldown_expiry_makes_a_target_eligible_again(self):
        cf = self._cf(outcomes=[ProviderError("rate limit")])
        gw = self._gw(cf)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(gw.image_targets(discover=False), [])
        gw.routing_state.get_health(
            "cloudflare", FLUX).cooldown_until = time.time() - 1
        self.assertEqual(
            [m.model_id for _c, m, _h in gw.image_targets(discover=False)],
            [FLUX])

    def test_routing_details_are_emitted_only_to_the_activity_log(self):
        from astra.core.events import EventBus
        from astra.store import Store
        bus = EventBus(Store(":memory:"))
        cf = self._cf(models=(LIGHTNING, FLUX),
                      outcomes=[_http_error("u", 429)])
        self._gw(cf, events=bus).generate_image("a cat", discover=False)
        rows = bus.history(limit=50)
        kinds = [e["kind"] for e in rows]
        self.assertIn("astra_gateway.image_request", kinds)
        self.assertIn("astra_gateway.image_failure", kinds)
        self.assertIn("astra_gateway.image_fallback", kinds)
        self.assertIn("astra_gateway.image_success", kinds)
        fb = [e for e in rows
              if e["kind"] == "astra_gateway.image_fallback"][0]
        self.assertEqual(fb["data"]["reason"], "429 rate limit")
        self.assertEqual(fb["data"]["next_model"], FLUX)
        self.assertEqual(fb["data"]["model"], LIGHTNING)

    def test_an_explicit_text_model_preference_is_soft(self):
        cf = self._cf(models=(LIGHTNING, FLUX))
        gw = self._gw(cf)
        gw.generate_image("a cat", model="llama-70b", discover=False)
        self.assertIn(gw.last_model, {LIGHTNING, FLUX})

    def test_editing_returns_a_clear_error_because_nothing_advertises_editing(self):
        gw = self._gw(self._cf())
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("ei photo ta edit kore dao", editing=True,
                              discover=False)
        self.assertIn("No image-generation model is currently configured",
                      str(ctx.exception))

    def test_health_is_isolated_per_provider_model(self):
        # One model failing must not cool down its provider's other models:
        # LIGHTNING fails, FLUX is still attempted and succeeds.
        cf = self._cf(models=(LIGHTNING, FLUX),
                      outcomes=[ProviderError("boom")])
        gw = self._gw(cf)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, FLUX)
        self.assertFalse(
            gw.routing_state.get_health("cloudflare", LIGHTNING).healthy)
        self.assertTrue(
            gw.routing_state.get_health("cloudflare", FLUX).healthy)


# ═══════════════════════════════════════════════════════════════════════════
# 5. Real provider image-API contract mechanics (urllib patched)
# ═══════════════════════════════════════════════════════════════════════════
class TestProviderImageAdapterMechanics(unittest.TestCase):
    def _cf(self, **env):
        from astra.ai.adapters.cloudflare import CloudflareAdapter
        return CloudflareAdapter(config=_cfg(CLOUDFLARE_API_KEYS="k",
                                             CLOUDFLARE_ACCOUNT_IDS="acct1",
                                             **env))

    def test_flux_1_schnell_body_is_prompt_only(self):
        conn = self._cf(CLOUDFLARE_IMAGE_MODELS=FLUX)
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_cf_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model=FLUX)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(seen["body"], {"prompt": "a cat"})
        self.assertIn("/ai/run/", seen["url"])
        self.assertIn(FLUX, seen["url"])

    def test_a_model_whose_schema_accepts_size_gets_width_and_height(self):
        conn = self._cf(CLOUDFLARE_IMAGE_MODELS=LIGHTNING)
        seen = {}

        def side(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _resp(_cf_ok())

        with mock.patch("urllib.request.urlopen", side):
            conn.generate_image("a cat", model=LIGHTNING, size="768x768")
        self.assertEqual(seen["body"]["prompt"], "a cat")
        self.assertEqual(seen["body"]["width"], 768)
        self.assertEqual(seen["body"]["height"], 768)

    def test_raw_binary_response_becomes_a_data_uri(self):
        conn = self._cf(CLOUDFLARE_IMAGE_MODELS=FLUX)
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _resp(PNG, "image/png")):
            out = conn.generate_image("a cat", model=FLUX)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(out.split(",", 1)[1]), PNG)

    def test_gemini_native_generate_content_mechanics(self):
        from astra.ai.gateway import AstraGatewayGemini
        conn = AstraGatewayGemini(config=_cfg(GW_GEMINI_API_KEYS="k"))
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_gemini_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat",
                                      model="gemini-3.1-flash-image")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertIn(":generateContent", seen["url"])
        self.assertIn("IMAGE",
                      seen["body"]["generationConfig"]["responseModalities"])

    def test_openai_images_protocol_mechanics(self):
        from astra.ai.gateway import AstraGatewayZAI
        conn = AstraGatewayZAI(config=_cfg(GW_ZAI_API_KEYS="k",
                                           GW_ZAI_IMAGE_MODELS="glm-image"))
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_openai_images_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model="glm-image")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertTrue(seen["url"].endswith("/images/generations"))
        self.assertEqual(seen["body"]["model"], "glm-image")

    def test_bedrock_refuses_a_model_that_is_not_in_the_image_pool(self):
        from astra.ai.adapters.bedrock import BedrockAdapter
        conn = BedrockAdapter(config=_cfg(BEDROCK_API_KEYS="k"))
        # A Claude text model must never get a Titan/Stability image body.
        with self.assertRaises(ProviderError) as ctx:
            conn.generate_image("a cat",
                                model="us.anthropic.claude-opus-4-5")
        self.assertIn("not an image-generation model", str(ctx.exception))
        # Nova Canvas is a real image model but paid-only -> not in the free
        # pool, so it is refused too (Astra only offers the free pool).
        with self.assertRaises(ProviderError):
            conn.generate_image("a cat", model="amazon.nova-canvas-v1:0")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Provider router: image dispatch only, with failover
# ═══════════════════════════════════════════════════════════════════════════
class TestRouterImageDispatch(unittest.TestCase):
    def test_router_routes_an_image_request_to_a_free_image_model(self):
        text = _FakeConn("groq", "groq", models=["llama-70b"])
        img = _FakeConn("cloudflare", "cloudflare", image_models=[FLUX])
        router = AstraRouter([text, img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user",
                       "content": "akta cat photo create kore dao"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.provider, "cloudflare")
        self.assertEqual(rr.model, FLUX)
        self.assertTrue(rr.text.startswith("data:image/png;base64,"))
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(text.image_calls, [])

    def test_router_image_failover_to_the_next_free_image_model(self):
        img = _FakeConn("cloudflare", "cloudflare",
                        image_models=[LIGHTNING, FLUX],
                        outcomes=[_http_error("u", 429)])
        router = AstraRouter([img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": "photo generate koro"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.model, FLUX)

    def test_router_never_routes_an_image_request_to_a_paid_image_model(self):
        paid = _FakeConn("gemini", "gemini",
                         image_models=["gemini-3.1-flash-image"])
        router = AstraRouter([paid], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": "generate an image"}]))
        self.assertFalse(rr.ok)
        self.assertIn("no eligible", rr.error or "")
        self.assertEqual(paid.image_calls, [])

    def test_router_never_answers_an_image_request_with_a_text_model(self):
        text = _FakeConn("groq", "groq", models=["llama-70b"])
        router = AstraRouter([text], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": "create a photo"}]))
        self.assertFalse(rr.ok)
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(text.image_calls, [])


# ═══════════════════════════════════════════════════════════════════════════
# 6b. OpenRouter: LIVE discovery is authoritative -- but only for free models
# ═══════════════════════════════════════════════════════════════════════════
class TestOpenRouterLiveDiscovery(unittest.TestCase):
    FREE_ID = "sourceful/riverflow-v2.5-pro:free"
    PAID_ID = "google/gemini-2.5-flash-image"

    def _payload(self, ids):
        return json.dumps({"data": [
            {"id": mid, "architecture": {"output_modalities": ["image"]}}
            for mid in ids]})

    def test_discovery_accepts_only_free_image_ids(self):
        from astra.ai.gateway import AstraGatewayOpenRouter
        conn = AstraGatewayOpenRouter(config=_cfg(GW_OPENROUTER_API_KEYS="k"))
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _resp(
                            self._payload([self.FREE_ID, self.PAID_ID]))):
            self.assertEqual(conn.list_image_models(discover=True),
                             [self.FREE_ID])

    def test_discovery_failure_invents_nothing(self):
        from astra.ai.gateway import AstraGatewayOpenRouter
        conn = AstraGatewayOpenRouter(config=_cfg(GW_OPENROUTER_API_KEYS="k"))

        def boom(req, timeout=None):
            raise OSError("offline")

        with mock.patch("urllib.request.urlopen", boom):
            self.assertEqual(conn.list_image_models(discover=True), [])
            self.assertEqual(conn.live_image_models(discover=True), [])

    def test_gateway_catalog_includes_a_live_free_image_model(self):
        from astra.ai.gateway import AstraAIGateway, AstraGatewayOpenRouter
        conn = AstraGatewayOpenRouter(config=_cfg(GW_OPENROUTER_API_KEYS="k"))
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _resp(
                            self._payload([self.FREE_ID, self.PAID_ID]))):
            cat = AstraAIGateway(connections=[conn])._image_catalog(
                discover=True)
        self.assertEqual([m.model_id for _c, m in cat], [self.FREE_ID])
        self.assertIn("image_generation", cat[0][1].capabilities)
        self.assertIn("image", cat[0][1].output_modalities)

    def test_router_dispatches_to_a_live_free_image_model(self):
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        conn = OpenRouterAdapter(config=_cfg(OPENROUTER_API_KEYS="k"))
        seen = {}

        def side(req, timeout=None):
            url = req.full_url
            if "output_modalities=image" in url:
                return _resp(self._payload([self.FREE_ID, self.PAID_ID]))
            seen["url"] = url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_openai_images_ok())

        with mock.patch("urllib.request.urlopen", side):
            router = AstraRouter([conn], max_retries=0)
            rr = router.route_request(RoutingRequest(
                task_type="image_generation",
                messages=[{"role": "user", "content": "photo generate koro"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(rr.model, self.FREE_ID)
        self.assertEqual(seen["body"]["model"], self.FREE_ID)
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
    """Real ChatPipeline with a scripted Gateway + scripted router."""

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
            self.last_model = FLUX
            return DATA_URI

    class _Router:
        def __init__(self, text="should not be used"):
            self.requests = []
            self.text = text

        def available_targets(self):
            return [{"provider": "cloudflare", "model": FLUX}]

        def route_request(self, req):
            from astra.ai.router import RoutingResult
            self.requests.append(req)
            return RoutingResult(ok=True, text=self.text,
                                 provider="cloudflare", model=FLUX)

    def test_image_request_goes_through_the_gateway_image_path(self):
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

    def test_gateway_without_image_path_falls_back_to_the_router(self):
        from astra.ai.chat_pipeline import ChatPipeline
        gw, rt = self._Gateway(), self._Router()
        gw.generate_image = None                   # no Gateway image path
        out = ChatPipeline(gw, rt).run("photo generate koro")
        self.assertTrue(out["ok"])
        self.assertEqual(len(rt.requests), 1)
        self.assertEqual(rt.requests[0].task_type, "image_generation")
        self.assertEqual(rt.requests[0].required_output_modalities, ["image"])

    def test_no_free_image_model_gives_a_clear_error_and_no_text_answer(self):
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
        self.assertIn("No currently available FREE image-generation model",
                      out["reply"])
        self.assertEqual(len(rt.requests), 1)       # no simple_chat retry
        self.assertEqual(rt.requests[0].task_type, "image_generation")

    def test_gateway_image_error_falls_back_to_the_router_not_to_text_chat(self):
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
#     HTTP mocked at the adapter boundary (no image credentials here).
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
                return TestEndToEndImageTurn._BRIEF

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
        # The fastest free image model 429s; the next one succeeds. The turn
        # must still produce a real image artifact and never touch a text
        # model or the Provider router.
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=[LIGHTNING, FLUX],
                       outcomes=[_http_error("u", 429)])
        out, gw, real, rt = self._run("akta cat photo create kore dao", cf)
        self.assertTrue(out["ok"], out.get("reply"))
        self.assertEqual(rt.requests, [])              # router never used
        self.assertEqual(gw.image_calls[0][0], "akta cat photo create kore dao")
        self.assertEqual(real.last_model, FLUX)
        self.assertEqual([m for m, _p in cf.image_calls], [LIGHTNING, FLUX])
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["mime_type"].startswith("image/"))
        self.assertNotIn("base64,", out["reply"])
        health = real.routing_state.snapshot()["model_health"]
        self.assertIn("cloudflare:" + LIGHTNING, health)
        self.assertIn("cloudflare:" + FLUX, health)


if __name__ == "__main__":
    unittest.main()
