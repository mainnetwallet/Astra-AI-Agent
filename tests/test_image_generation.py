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
    meets_gateway_requirements, rank_image_targets, rank_targets)
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

# User-requested FREE candidates that are force-included in the pool.
GEMINI_IMG = "gemini-2.5-flash-image"
OR_FLUX = "black-forest-labs/flux-1-schnell:free"
OR_GEMINI = "google/gemini-2.5-flash-image-preview:free"
OR_RIVER = "sourceful/riverflow-v2.5-pro:free"
USER_REQ_POOL = (GEMINI_IMG, OR_FLUX, OR_GEMINI, OR_RIVER)


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
    def test_pool_is_the_verified_free_providers(self):
        # Cloudflare (documented free Neurons) plus the user-requested
        # force-added Gemini / OpenRouter candidates.
        self.assertEqual(set(FREE_IMAGE_PROVIDERS),
                         {"cloudflare", "gemini", "openrouter"})
        self.assertEqual({p for p, _m in image_pool()},
                         {"cloudflare", "gemini", "openrouter"})

    def test_every_pool_model_is_free_with_quotable_evidence(self):
        for provider, mid in image_pool():
            spec = image_spec(provider, mid)
            self.assertIsNotNone(spec, mid)
            self.assertEqual(spec.free_tier, FREE_TRUE, mid)
            self.assertTrue(spec.is_free, mid)
            self.assertTrue(spec.free_evidence, mid)
            if provider == "cloudflare":
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

    def test_gemini_2_5_flash_image_is_force_added_to_the_free_pool(self):
        mid = "gemini-2.5-flash-image"
        self.assertTrue(is_image_model("gemini", mid))
        self.assertTrue(is_free_image_model("gemini", mid))
        spec = image_spec("gemini", mid)
        self.assertEqual(spec.free_tier, FREE_TRUE)
        self.assertEqual(spec.model, mid)                 # exact id kept
        self.assertIn(IMAGE_GENERATION, spec.capabilities)
        self.assertIn("image", spec.output_modalities)
        self.assertNotIn(("gemini", mid), REJECTED_IMAGE_MODELS)

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

    def test_openrouter_free_candidates_are_force_added_exactly(self):
        for mid in ("google/gemini-2.5-flash-image-preview:free",
                    "black-forest-labs/flux-1-schnell:free",
                    "sourceful/riverflow-v2.5-pro:free"):
            self.assertTrue(is_image_model("openrouter", mid), mid)
            self.assertTrue(is_free_image_model("openrouter", mid), mid)
            spec = image_spec("openrouter", mid)
            self.assertEqual(spec.free_tier, FREE_TRUE, mid)
            self.assertEqual(spec.model, mid, mid)        # exact id kept
            self.assertIn(IMAGE_GENERATION, spec.capabilities, mid)
            self.assertNotIn(("openrouter", mid), REJECTED_IMAGE_MODELS, mid)

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
        for provider in ("cloudflare", "gemini", "openrouter"):
            self.assertTrue(provider_supports_image_generation(provider),
                            provider)
        for provider in ("bedrock", "zai", "groq", "cerebras"):
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
# 1b. 2026-09-26 re-audit — newly investigated FREE image-provider candidates
# ═══════════════════════════════════════════════════════════════════════════
class TestReauditedProviderCandidates(unittest.TestCase):
    """None of the additionally-investigated providers cleared the strict
    FREE bar; each rejection must be recorded with real evidence rather than
    silently absent from the registry."""

    def test_together_ai_flux_schnell_free_is_not_yet_live(self):
        self.assertFalse(is_image_model(
            "together", "black-forest-labs/FLUX.1-schnell-Free"))
        reason = rejected_image_reason(
            "together", "black-forest-labs/FLUX.1-schnell-Free")
        self.assertIn("not currently free", reason)
        self.assertIn("Launching soon", reason)

    def test_together_ai_paid_image_models_are_rejected(self):
        for mid in ("black-forest-labs/FLUX.1-schnell",
                    "black-forest-labs/FLUX.1.1-pro",
                    "stabilityai/stable-diffusion-xl-base-1.0"):
            self.assertFalse(is_image_model("together", mid), mid)
            self.assertIn("paid",
                          rejected_image_reason("together", mid).lower())

    def test_huggingface_inference_providers_rejected_for_negligible_credit(self):
        for mid in ("black-forest-labs/FLUX.1-schnell",
                    "black-forest-labs/FLUX.1-dev", "Qwen/Qwen-Image"):
            self.assertFalse(is_image_model("huggingface", mid), mid)
            reason = rejected_image_reason("huggingface", mid)
            self.assertIn("not a genuine free tier", reason)
            self.assertIn("$0.10/month", reason)

    def test_fal_ai_is_paid_only_no_standing_free_tier(self):
        for mid in ("fal-ai/flux/schnell", "fal-ai/flux/dev"):
            self.assertFalse(is_image_model("fal", mid), mid)
            self.assertIn("no standing free tier",
                          rejected_image_reason("fal", mid))

    def test_replicate_is_paid_only(self):
        for mid in ("black-forest-labs/flux-schnell", "stability-ai/sdxl"):
            self.assertFalse(is_image_model("replicate", mid), mid)
            self.assertIn("paid-only",
                          rejected_image_reason("replicate", mid))

    def test_fireworks_ai_is_billed_per_step_not_free(self):
        for mid in ("accounts/fireworks/models/flux-1-schnell-fp8",
                    "accounts/fireworks/models/flux-1-dev-fp8"):
            self.assertFalse(is_image_model("fireworks", mid), mid)
            reason = rejected_image_reason("fireworks", mid)
            self.assertIn("per diffusion step", reason)

    def test_nscale_has_no_documented_free_allocation(self):
        self.assertFalse(is_image_model(
            "nscale", "black-forest-labs/FLUX.1-schnell"))
        self.assertIn("no documented free allocation", rejected_image_reason(
            "nscale", "black-forest-labs/FLUX.1-schnell"))

    def test_novita_one_time_trial_allowance_is_not_a_recurring_free_tier(self):
        self.assertFalse(is_image_model("novita", "flux-1-schnell"))
        reason = rejected_image_reason("novita", "flux-1-schnell")
        self.assertIn("not a recurring free tier", reason)

    def test_wavespeed_is_paid_only(self):
        self.assertFalse(is_image_model("wavespeed", "wavespeed-ai/flux-schnell"))
        self.assertIn("paid-only", rejected_image_reason(
            "wavespeed", "wavespeed-ai/flux-schnell"))

    def test_none_of_the_reaudited_providers_entered_the_free_pool(self):
        # The strict audit found no additional genuinely-free provider, so
        # the pool must remain exactly what it was before this re-audit.
        investigated = {"together", "huggingface", "fal", "replicate",
                        "fireworks", "nscale", "novita", "wavespeed"}
        pool_providers = {p for p, _m in image_pool()}
        self.assertEqual(pool_providers & investigated, set())
        # The re-audit added no provider; the pool is Cloudflare plus the
        # separately force-added Gemini / OpenRouter candidates.
        self.assertEqual(FREE_IMAGE_PROVIDERS,
                         frozenset({"cloudflare", "gemini", "openrouter"}))

    def test_reaudit_evidence_never_invents_a_free_claim(self):
        # Every rejected (provider, model) pair investigated in the
        # re-audit must have a non-empty, specific reason -- never a blank
        # "unknown" rejection, which would defeat the audit-trail purpose.
        investigated_pairs = [
            ("together", "black-forest-labs/FLUX.1-schnell-Free"),
            ("huggingface", "black-forest-labs/FLUX.1-schnell"),
            ("fal", "fal-ai/flux/schnell"),
            ("replicate", "black-forest-labs/flux-schnell"),
            ("fireworks", "accounts/fireworks/models/flux-1-schnell-fp8"),
            ("nscale", "black-forest-labs/FLUX.1-schnell"),
            ("novita", "flux-1-schnell"),
            ("wavespeed", "wavespeed-ai/flux-schnell"),
        ]
        for provider, mid in investigated_pairs:
            reason = rejected_image_reason(provider, mid)
            self.assertTrue(reason, f"{provider}/{mid} has no recorded reason")
            self.assertGreater(len(reason), 20, f"{provider}/{mid} reason too thin")

    def test_cloudflare_free_evidence_reflects_the_current_per_task_table(self):
        # Re-audit 2026-09-26: Cloudflare's pricing docs now express the
        # image free allocation directly in steps/day rather than only via
        # the old blanket Neurons/day figure.
        spec = image_spec("cloudflare", FLUX)
        self.assertIn("250", spec.free_evidence)
        self.assertIn("steps", spec.free_evidence.lower())


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

    def test_ranking_is_the_deterministic_serial_order(self):
        from astra.ai.image_models import IMAGE_PRIORITY
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(
            self._catalog(self._text(), self._vision()), state)
        ranked = rank_targets(targets, category="image_generation")
        self.assertTrue(ranked)
        self.assertTrue(all(m.has("image_generation")
                            for _c, m, _h in ranked))
        # deterministic, curated order -- never health/latency based
        present = {m.model_id for _c, m, _h in ranked}
        self.assertEqual([m.model_id for _c, m, _h in ranked],
                         [mid for mid in IMAGE_PRIORITY if mid in present])
        self.assertEqual(present, set(CF_POOL))

    def test_serial_order_is_identical_across_calls(self):
        state = GatewayRoutingState(None)
        catalog = self._catalog()
        first = [m.model_id for _c, m, _h in rank_targets(
            eligible_image_generation_targets(catalog, state),
            category="image_generation")]
        second = [m.model_id for _c, m, _h in rank_targets(
            eligible_image_generation_targets(catalog, state),
            category="image_generation")]
        self.assertEqual(first, second)

    def test_health_state_never_excludes_an_image_target(self):
        # No proactive image health check: even a model the routing state has
        # on cooldown from an earlier request is still eligible -- only the
        # actual generation request decides, and only for that request.
        state = GatewayRoutingState(None)
        catalog = self._catalog()
        state.record_failure("cloudflare", FLUX, cooldown_s=999)
        self.assertFalse(state.get_health("cloudflare", FLUX).healthy)
        ids = {m.model_id for _c, m, _h in
               eligible_image_generation_targets(catalog, state)}
        self.assertIn(FLUX, ids)
        ranked = rank_targets(
            eligible_image_generation_targets(catalog, state),
            category="image_generation")
        self.assertEqual(ranked[0][1].model_id, FLUX)

    def test_configured_priority_reorders_the_serial_list(self):
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        ranked = rank_image_targets(targets, preferred_ids=[LUCID, PHOENIX])
        self.assertEqual([m.model_id for _c, m, _h in ranked][:2],
                         [LUCID, PHOENIX])
        self.assertEqual(len(ranked), len(CF_POOL))

    def test_configured_priority_cannot_add_an_ineligible_model(self):
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        ranked = rank_image_targets(
            targets, preferred_ids=["gemini-3.1-flash-image", LLAMA, FLUX])
        ids = [m.model_id for _c, m, _h in ranked]
        self.assertEqual(len(ids), len(CF_POOL))
        self.assertNotIn("gemini-3.1-flash-image", ids)
        self.assertNotIn(LLAMA, ids)
        self.assertEqual(ids[0], FLUX)

    def test_describe_image_targets_reports_priority_and_modalities(self):
        from astra.ai.image_models import image_priority_index
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        rows = describe_image_targets(targets)
        self.assertEqual(len(rows), len(CF_POOL))
        self.assertTrue(all("image" in r["output_modalities"] for r in rows))
        self.assertEqual(rows[0]["provider"], "cloudflare")
        self.assertEqual(rows[0]["model"], FLUX)
        for row in rows:
            self.assertEqual(
                row["priority"],
                image_priority_index(row["provider"], row["model"]))

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


# ---------------------------------------------------------------------------
# 3b. User-requested FREE candidates (Gemini + OpenRouter), force-added
# ---------------------------------------------------------------------------
class TestUserRequestedFreeImageModels(unittest.TestCase):
    """The four force-added ids must be really routable: exact ids, FREE pool
    registration, provider image-API dispatch, and serial fallback across
    providers -- never falling back to a paid/text/vision model."""

    def _gw(self, *conns, config=None, events=None):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), config=config,
                              events=events)

    def _gemini(self, outcomes=None):
        return _FakeConn("astra-gw-gemini", "gemini",
                         image_models=[GEMINI_IMG], outcomes=outcomes)

    def _or(self, outcomes=None):
        return _FakeConn("astra-gw-openrouter", "openrouter",
                         image_models=[OR_FLUX, OR_GEMINI, OR_RIVER],
                         outcomes=outcomes)

    def test_exact_ids_are_registered_and_free(self):
        for mid in USER_REQ_POOL:
            provider = "gemini" if mid == GEMINI_IMG else "openrouter"
            self.assertTrue(is_image_model(provider, mid), mid)
            self.assertTrue(is_free_image_model(provider, mid), mid)
            spec = image_spec(provider, mid)
            self.assertEqual(spec.model, mid)          # exact id kept
            self.assertEqual(spec.free_tier, FREE_TRUE, mid)
            self.assertIn(IMAGE_GENERATION, spec.capabilities, mid)
            self.assertIn("image", spec.output_modalities, mid)

    def test_pool_contains_cloudflare_and_the_requested_models(self):
        pool = set(image_pool())
        for mid in CF_POOL:
            self.assertIn(("cloudflare", mid), pool, mid)
        self.assertIn(("gemini", GEMINI_IMG), pool)
        for mid in (OR_FLUX, OR_GEMINI, OR_RIVER):
            self.assertIn(("openrouter", mid), pool, mid)

    def test_gateway_targets_include_the_requested_models(self):
        gw = self._gw(self._gemini(), self._or())
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        for mid in USER_REQ_POOL:
            self.assertIn(mid, ids)

    def test_gateway_priority_order_is_the_deterministic_order(self):
        from astra.ai.image_models import IMAGE_PRIORITY
        gw = self._gw(self._gemini(), self._or())
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids, [m for m in IMAGE_PRIORITY if m in set(ids)])

    def test_gemini_target_reports_image_output_only(self):
        gw = self._gw(self._gemini())
        rows = [m for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual([m.model_id for m in rows], [GEMINI_IMG])
        self.assertIn("image_generation", rows[0].capabilities)
        self.assertIn("image", rows[0].output_modalities)
        # editing is NOT advertised: no adapter forwards a source image yet
        self.assertNotIn("image_editing", rows[0].capabilities)

    def test_gemini_dispatch_uses_the_image_api_not_chat(self):
        gem = self._gemini()
        gw = self._gw(gem)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual([m for m, _p in gem.image_calls], [GEMINI_IMG])
        self.assertEqual(gem.chat_calls, 0)

    def test_openrouter_dispatch_preserves_the_exact_model_id(self):
        orc = self._or()
        gw = self._gw(orc)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual([m for m, _p in orc.image_calls], [OR_FLUX])
        self.assertEqual([p for _m, p in orc.image_calls], ["a cat"])
        self.assertEqual(orc.chat_calls, 0)

    def test_serial_fallback_crosses_providers(self):
        # Cloudflare 429 -> Gemini timeout -> OpenRouter flux success.
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=[FLUX], outcomes=[_http_error("u", 429)])
        gem = self._gemini(outcomes=[TimeoutError("slow")])
        orc = self._or()
        gw = self._gw(cf, gem, orc)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, OR_FLUX)
        self.assertEqual(gw.last_attempts, 3)
        self.assertEqual([m for m, _p in cf.image_calls], [FLUX])
        self.assertEqual([m for m, _p in gem.image_calls], [GEMINI_IMG])
        self.assertEqual([m for m, _p in orc.image_calls], [OR_FLUX])
        # no proactive health/cooldown state is written for image generation:
        # every entry is still healthy with zero recorded failures.
        health = gw.routing_state.snapshot()["model_health"]
        self.assertTrue(health)
        for entry in health.values():
            self.assertTrue(entry["healthy"])
            self.assertEqual(entry["failure_count"], 0)
            self.assertEqual(entry["cooldown_until"], 0)

    def test_gemini_429_falls_over_to_openrouter(self):
        gem = self._gemini(outcomes=[_http_error("u", 429)])
        orc = self._or()
        gw = self._gw(gem, orc)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, OR_FLUX)

    def test_openrouter_5xx_moves_to_the_next_openrouter_model(self):
        orc = self._or(outcomes=[_http_error("u", 502)])
        gw = self._gw(orc)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, OR_GEMINI)
        self.assertEqual([m for m, _p in orc.image_calls],
                         [OR_FLUX, OR_GEMINI])

    def test_unavailable_model_moves_to_the_next(self):
        gem = self._gemini(outcomes=[_http_error("u", 404)])
        orc = self._or()
        gw = self._gw(gem, orc)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, OR_FLUX)

    def test_no_model_is_attempted_twice_in_one_request(self):
        gem = self._gemini(outcomes=[ProviderError("boom")])
        orc = self._or(outcomes=[ProviderError("boom")] * 3)
        gw = self._gw(gem, orc)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(len(gem.image_calls), 1)
        self.assertEqual([m for m, _p in orc.image_calls],
                         [OR_FLUX, OR_GEMINI, OR_RIVER])

    def test_all_requested_models_fail_gives_the_clear_error(self):
        gem = self._gemini(outcomes=[ProviderError("boom")])
        orc = self._or(outcomes=[ProviderError("boom")] * 3)
        gw = self._gw(gem, orc)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("All available FREE image-generation models failed",
                      str(ctx.exception))
        self.assertEqual(gw.last_attempts, 4)

    def test_never_falls_back_to_a_text_paid_or_vision_model(self):
        gem = self._gemini(outcomes=[ProviderError("boom")])
        orc = self._or(outcomes=[ProviderError("boom")] * 3)
        text = _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])
        vision = _FakeConn("astra-gw-cohere", "cohere",
                           models=["command-a-vision-07-2025"])
        paid = _FakeConn("astra-gw-zai", "zai", image_models=["glm-image"])
        paid_gemini = _FakeConn("astra-gw-gemini", "gemini",
                                image_models=["gemini-3.1-flash-image"])
        gw = self._gw(gem, orc, text, vision, paid, paid_gemini)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        for conn in (text, vision, paid, paid_gemini):
            self.assertEqual(conn.chat_calls, 0, conn.name)
            self.assertEqual(conn.image_calls, [], conn.name)

    def test_configured_priority_can_reorder_without_code_edits(self):
        gem = self._gemini()
        orc = self._or()
        gw = self._gw(gem, orc, config=_cfg(
            GW_IMAGE_GENERATION_PRIORITY=OR_RIVER + "," + GEMINI_IMG))
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids[:2], [OR_RIVER, GEMINI_IMG])

    def test_env_list_changes_the_pool_without_code_edits(self):
        conn = _FakeConn("astra-gw-openrouter", "openrouter",
                         image_models=[OR_RIVER])
        gw = self._gw(conn)
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids, [OR_RIVER])


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

    # B. 429 on the first model -> next model
    def test_b_rate_limit_fails_over_to_the_next_image_model(self):
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[_http_error("u", 429)])
        gw = self._gw(cf)
        out = gw.generate_image("photo generate koro", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, LIGHTNING)
        self.assertEqual(gw.last_attempts, 2)
        # per-request only: no cooldown/health state is written
        self.assertTrue(
            gw.routing_state.get_health("cloudflare", FLUX).healthy)

    # C. timeout on the first model -> next model
    def test_c_timeout_fails_over_to_the_next_image_model(self):
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[TimeoutError("timed out")])
        gw = self._gw(cf)
        out = gw.generate_image("ekta chobi baniye dao", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, LIGHTNING)

    # D. 5xx then 429 then success -> third model, three attempts
    def test_d_multi_failure_chain_reaches_the_third_target(self):
        cf = self._cf(models=(FLUX, LIGHTNING, LUCID),
                      outcomes=[_http_error("u", 500),
                                _http_error("u", 429)])
        gw = self._gw(cf)
        out = gw.generate_image("create a photo of a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, LUCID)
        self.assertEqual(gw.last_attempts, 3)

    def test_quota_failure_falls_over_to_the_next_model(self):
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[ProviderError("quota exhausted for account")])
        gw = self._gw(cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, LIGHTNING)

    def test_provider_api_error_falls_over_to_the_next_model(self):
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[ProviderError("provider error 502 bad gateway")])
        gw = self._gw(cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, LIGHTNING)

    def test_success_stops_the_chain_immediately(self):
        cf = self._cf(models=(FLUX, LIGHTNING, LUCID))
        gw = self._gw(cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual([m for m, _p in cf.image_calls], [FLUX])
        self.assertEqual(gw.last_attempts, 1)

    def test_no_model_is_attempted_twice_in_one_request(self):
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[ProviderError("boom"), ProviderError("boom")])
        gw = self._gw(cf)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        tried = [m for m, _p in cf.image_calls]
        self.assertEqual(tried, [FLUX, LIGHTNING])
        self.assertEqual(len(tried), len(set(tried)))

    # E. every image model fails -> the clear FREE-exhaustion error
    def test_e_all_targets_fail_raises_a_clear_error(self):
        from astra.ai.image_models import IMAGE_EXHAUSTED_MESSAGE
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[ProviderError("boom"), ProviderError("boom")])
        gw = self._gw(cf)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn(IMAGE_EXHAUSTED_MESSAGE, str(ctx.exception))
        self.assertEqual(gw.last_attempts, 2)

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

    # G. text model available but image model fails -> text model never used
    def test_g_text_model_is_never_a_fallback_for_a_failed_image_model(self):
        cf = self._cf(outcomes=[ProviderError("boom")])
        text = self._text()
        gw = self._gw(cf, text)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(text.image_calls, [])

    def test_vision_only_model_is_never_a_fallback(self):
        cf = self._cf(outcomes=[ProviderError("boom")])
        vision = self._vision()
        gw = self._gw(cf, vision)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(vision.chat_calls, 0)
        self.assertEqual(vision.image_calls, [])

    def test_paid_image_model_is_never_a_fallback(self):
        paid = _FakeConn("astra-gw-gemini", "gemini",
                         image_models=["gemini-3.1-flash-image"])
        cf = self._cf(outcomes=[ProviderError("boom")])
        gw = self._gw(paid, cf)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(paid.image_calls, [])
        self.assertEqual(paid.chat_calls, 0)

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

    # I. a model that failed in request #1 is tried first again in request #2
    def test_i_a_failed_model_is_retried_on_the_next_request(self):
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[ProviderError("boom")])
        gw = self._gw(cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, LIGHTNING)
        # request #2: the deterministic order is unchanged, so FLUX is first
        # again and -- with no failure scripted this time -- succeeds. A
        # failure is never a permanent unhealthy state.
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, FLUX)
        self.assertEqual([m for m, _p in cf.image_calls],
                         [FLUX, LIGHTNING, FLUX])
        self.assertTrue(
            gw.routing_state.get_health("cloudflare", FLUX).healthy)

    def test_each_attempt_is_recorded_in_the_activity_log(self):
        from astra.core.events import EventBus
        from astra.store import Store
        bus = EventBus(Store(":memory:"))
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[_http_error("u", 429)])
        self._gw(cf, events=bus).generate_image("a cat", discover=False)
        rows = bus.history(limit=80)
        kinds = [e["kind"] for e in rows]
        for kind in ("image.generation.start", "image.generation.attempt",
                     "image.generation.failure", "image.generation.fallback",
                     "image.generation.success"):
            self.assertIn(kind, kinds)
        attempts = sorted(
            (e for e in rows
             if e["kind"] == "image.generation.attempt"),
            key=lambda e: e["data"]["attempt"])
        self.assertEqual([e["data"]["model"] for e in attempts],
                         [FLUX, LIGHTNING])
        self.assertEqual([e["data"]["attempt"] for e in attempts], [1, 2])
        for e in attempts:
            self.assertIn("op", e["data"])
        fb = [e for e in rows if e["kind"] == "image.generation.fallback"][0]
        self.assertEqual(fb["data"]["reason"], "429 rate limit")
        self.assertEqual(fb["data"]["model"], FLUX)
        self.assertEqual(fb["data"]["next_model"], LIGHTNING)
        success = [e for e in rows
                   if e["kind"] == "image.generation.success"][0]
        self.assertIn("duration_ms", success["data"])
        self.assertEqual(success["data"]["model"], LIGHTNING)
        failure = [e for e in rows
                   if e["kind"] == "image.generation.failure"][0]
        self.assertIn("duration_ms", failure["data"])
        self.assertEqual(failure["data"]["failure_category"], "429 rate limit")

    def test_exhaustion_is_recorded_in_the_activity_log(self):
        from astra.core.events import EventBus
        from astra.store import Store
        bus = EventBus(Store(":memory:"))
        cf = self._cf(models=(FLUX, LIGHTNING),
                      outcomes=[ProviderError("boom"), ProviderError("boom")])
        gw = self._gw(cf, events=bus)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        rows = bus.history(limit=80)
        exhausted = [e for e in rows
                     if e["kind"] == "image.generation.exhausted"]
        self.assertEqual(len(exhausted), 1)
        self.assertEqual(exhausted[0]["data"]["attempts"], 2)

    def test_an_explicit_text_model_preference_is_soft(self):
        cf = self._cf(models=(FLUX, LIGHTNING))
        gw = self._gw(cf)
        gw.generate_image("a cat", model="llama-70b", discover=False)
        self.assertEqual(gw.last_model, FLUX)

    def test_editing_returns_a_clear_error_because_nothing_advertises_editing(self):
        gw = self._gw(self._cf())
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("ei photo ta edit kore dao", editing=True,
                              discover=False)
        self.assertIn("No image-generation model is currently configured",
                      str(ctx.exception))

    def test_gateway_priority_is_configurable(self):
        from astra.ai.gateway import AstraAIGateway
        cf = self._cf(models=(FLUX, LIGHTNING, LUCID))
        gw = AstraAIGateway(connections=[cf], config=_cfg(
            GW_IMAGE_GENERATION_PRIORITY=LUCID + "," + LIGHTNING))
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids[:2], [LUCID, LIGHTNING])

    # J. image generation NEVER performs a proactive health probe: the only
    # provider call is the real generation request itself.
    def test_no_health_probe_is_performed_for_image_generation(self):
        probe_calls = []

        class _ProbeCountingConn(_FakeConn):
            def health_check(self):
                probe_calls.append(self.name)
                return True

        cf = _ProbeCountingConn(
            "astra-gw-cloudflare", "cloudflare",
            image_models=[FLUX, LIGHTNING],
            outcomes=[_http_error("u", 429)])
        gw = self._gw(cf)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        # no health_check()/probe at any point in the generation path
        self.assertEqual(probe_calls, [])
        # the only two calls are the two REAL generation attempts, and each
        # carries the user's own prompt -- no synthetic probe image/prompt.
        self.assertEqual([m for m, _p in cf.image_calls], [FLUX, LIGHTNING])
        self.assertEqual([p for _m, p in cf.image_calls], ["a cat", "a cat"])

    def test_no_health_probe_on_the_all_failed_path(self):
        probe_calls = []

        class _ProbeCountingConn(_FakeConn):
            def health_check(self):
                probe_calls.append(self.name)
                return True

        cf = _ProbeCountingConn(
            "astra-gw-cloudflare", "cloudflare",
            image_models=[FLUX, LIGHTNING],
            outcomes=[ProviderError("boom"), ProviderError("boom")])
        gw = self._gw(cf)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(probe_calls, [])

    def test_image_targets_include_a_cooled_down_model(self):
        # A model in cooldown is still in the serial pool: there is no
        # proactive health gating for image generation.
        cf = self._cf(models=(FLUX, LIGHTNING))
        gw = self._gw(cf)
        gw.routing_state.record_failure("cloudflare", FLUX)
        gw.routing_state.record_failure("cloudflare", FLUX)
        gw.routing_state.record_failure("cloudflare", FLUX)
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertIn(FLUX, ids)
        self.assertEqual(ids[0], FLUX)


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
        # FLUX is first in the deterministic serial order and 429s, so the
        # next eligible FREE model -- LIGHTNING -- serves the request.
        self.assertEqual(rr.model, LIGHTNING)

    def test_router_image_failure_never_marks_the_provider_down(self):
        # The serial fallback keeps no permanent unhealthy state: a 429 on
        # one model must not take the whole provider out of rotation.
        img = _FakeConn("cloudflare", "cloudflare",
                        image_models=[FLUX, LIGHTNING],
                        outcomes=[_http_error("u", 429)])
        router = AstraRouter([img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": "photo banao"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertNotIn("cloudflare", router._down)

    def test_router_only_calls_generate_image_with_the_user_prompt(self):
        # No synthetic probe image/prompt is ever sent: every provider call is
        # one real generation attempt carrying the user's own prompt.
        img = _FakeConn("cloudflare", "cloudflare",
                        image_models=[FLUX, LIGHTNING],
                        outcomes=[_http_error("u", 429)])
        router = AstraRouter([img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user", "content": "akta cat photo banao"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(img.chat_calls, 0)
        self.assertEqual([m for m, _p in img.image_calls], [FLUX, LIGHTNING])
        self.assertEqual([p for _m, p in img.image_calls],
                         ["akta cat photo banao", "akta cat photo banao"])

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
        self.assertEqual(real.last_model, LIGHTNING)
        self.assertEqual([m for m, _p in cf.image_calls], [FLUX, LIGHTNING])
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["mime_type"].startswith("image/"))
        self.assertNotIn("base64,", out["reply"])
        health = real.routing_state.snapshot()["model_health"]
        self.assertIn("cloudflare:" + LIGHTNING, health)
        self.assertIn("cloudflare:" + FLUX, health)

    def test_gemini_and_openrouter_image_turn_yields_an_artifact(self):
        # Gemini 429s, the OpenRouter flux :free model succeeds -- the turn
        # must still produce a real image artifact with no base64 in the reply.
        gem = _FakeConn("astra-gw-gemini", "gemini",
                        image_models=[GEMINI_IMG],
                        outcomes=[_http_error("u", 429)])
        orc = _FakeConn("astra-gw-openrouter", "openrouter",
                        image_models=[OR_FLUX])
        out, gw, real, rt = self._run("akta cat photo create kore dao",
                                      gem, orc)
        self.assertTrue(out["ok"], out.get("reply"))
        self.assertEqual(rt.requests, [])              # router never used
        self.assertEqual(real.last_model, OR_FLUX)
        self.assertEqual([m for m, _p in gem.image_calls], [GEMINI_IMG])
        self.assertEqual([m for m, _p in orc.image_calls], [OR_FLUX])
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["mime_type"].startswith("image/"))
        self.assertTrue(arts[0]["id"])
        self.assertTrue(arts[0]["filename"])
        self.assertNotIn("base64,", out["reply"])


if __name__ == "__main__":
    unittest.main()
