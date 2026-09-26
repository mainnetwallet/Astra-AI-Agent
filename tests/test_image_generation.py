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

# The single user-requested FREE candidate still force-included in the pool:
# Gemini's dedicated image model.
GEMINI_IMG = "gemini-2.5-flash-image"

# OpenRouter's live image catalog contains ZERO `:free` image-output models
# (verified 2026-09-26 against GET https://openrouter.ai/api/v1/images/models),
# so its static FREE pool is EMPTY. OR_SYNTH is a synthetic FREE-shaped id used
# ONLY to exercise the OpenRouter adapter / live-discovery contract
# mechanically -- it is NOT a real catalog id and must never be treated as one.
OR_SYNTH = "example/synthetic-free-image:free"

# Ids removed from the active pool on 2026-09-26: live verification proved none
# of them exists as a free image model (see REJECTED_IMAGE_MODELS).
OR_DEAD_FREE_IDS = (
    "google/gemini-2.5-flash-image-preview:free",
    "black-forest-labs/flux-1-schnell:free",
    "sourceful/riverflow-v2.5-pro:free",
)


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

    def generate_image(self, prompt, model=None, size="1024x1024", n=1,
                       source_image=None):
        self.image_calls.append((model, prompt, source_image))
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


def _artifact_path(artifact_dir, art):
    """On-disk path of a stored artifact (store_artifact names files
    ``{id}_{filename}``)."""
    return os.path.join(artifact_dir, "%s_%s" % (art["id"], art["filename"]))


def _assert_readable_png_artifact(test, art, artifact_dir):
    """Assert the artifact metadata is real: the file exists on disk, is
    readable, has the expected image MIME and holds the exact PNG bytes the
    provider returned -- never just a metadata claim."""
    path = _artifact_path(artifact_dir, art)
    test.assertTrue(os.path.isfile(path), path)
    test.assertEqual(art["mime_type"], "image/png")
    test.assertGreater(art["size"], 0)
    with open(path, "rb") as f:
        raw = f.read()
    test.assertEqual(raw, PNG)
    return path


# ═══════════════════════════════════════════════════════════════════════════
# 1. FREE image pool — evidence, not model-name guessing
# ═══════════════════════════════════════════════════════════════════════════
class TestFreeImagePool(unittest.TestCase):
    def test_pool_is_the_verified_free_providers(self):
        # Cloudflare (documented free Neurons) plus the single user-requested
        # force-added Gemini candidate. OpenRouter remains a KNOWN free-image
        # provider (its live-discovery path is retained) but contributes no
        # static model: its catalog has zero `:free` image models.
        self.assertEqual(set(FREE_IMAGE_PROVIDERS),
                         {"cloudflare", "gemini", "openrouter"})
        self.assertEqual({p for p, _m in image_pool()},
                         {"cloudflare", "gemini"})

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

    def test_only_capable_models_advertise_image_editing(self):
        for provider, mid in image_pool():
            spec = image_spec(provider, mid)
            if provider == "gemini" and mid == GEMINI_IMG:
                self.assertTrue(is_image_editing_model(provider, mid), mid)
                self.assertIn(IMAGE_EDITING, spec.capabilities, mid)
            else:
                self.assertFalse(is_image_editing_model(provider, mid), mid)
                self.assertNotIn(IMAGE_EDITING, spec.capabilities, mid)

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

    def test_openrouter_dead_free_ids_are_rejected_not_force_added(self):
        # Live-verified 2026-09-26: none of these is a free image model in
        # OpenRouter's catalog, so every one is rejected (never selectable).
        for mid in OR_DEAD_FREE_IDS:
            self.assertFalse(is_image_model("openrouter", mid), mid)
            self.assertFalse(is_free_image_model("openrouter", mid), mid)
            self.assertIn(("openrouter", mid), REJECTED_IMAGE_MODELS, mid)
            self.assertTrue(rejected_image_reason("openrouter", mid), mid)

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
        for provider in ("cloudflare", "gemini"):
            self.assertTrue(provider_supports_image_generation(provider),
                            provider)
        # openrouter keeps its adapter + live-discovery path but has no kept
        # model right now, so it is not (yet) an image-capable provider.
        for provider in ("openrouter", "bedrock", "zai", "groq", "cerebras"):
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
        from astra.ai.image_models import IMAGE_PRIORITY
        self.assertEqual([m.model_id for _c, m, _h in ranked],
                         [mid for mid in IMAGE_PRIORITY if mid in set(CF_POOL)])

    def test_configured_priority_reorders_the_serial_list(self):
        # DREAM/INPAINT are last in the curated order, so moving them to the
        # front proves the override is honoured (and, being reorder-only, does
        # not drop the other eligible models).
        state = GatewayRoutingState(None)
        targets = eligible_image_generation_targets(self._catalog(), state)
        ranked = rank_image_targets(targets, preferred_ids=[DREAM, INPAINT])
        self.assertEqual([m.model_id for _c, m, _h in ranked][:2],
                         [DREAM, INPAINT])
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

    def test_editing_targets_require_an_edit_capable_model(self):
        state = GatewayRoutingState(None)
        cf_only = self._catalog()
        self.assertEqual(
            eligible_image_generation_targets(cf_only, state, editing=True), [])
        gem = _FakeConn("astra-gw-gemini", "gemini",
                        image_models=[GEMINI_IMG])
        targets = eligible_image_generation_targets(
            self._catalog(gem), state, editing=True)
        self.assertEqual([m.model_id for _c, m, _h in targets], [GEMINI_IMG])


# ---------------------------------------------------------------------------
# 3b. Image-provider routing: the REAL FREE pool (Cloudflare + Gemini) and
#     the live-verified EMPTY OpenRouter static pool
# ---------------------------------------------------------------------------
class TestForceAddedGeminiImageModel(unittest.TestCase):
    """`gemini-2.5-flash-image` is the ONE remaining user-requested id
    force-included in the FREE pool, as the dedicated Gemini provider model
    (NOT an OpenRouter model). It must be really routable: exact id, FREE
    pool registration and provider image-API dispatch."""

    def _gw(self, *conns, config=None, events=None):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), config=config,
                              events=events)

    def _gemini(self, outcomes=None):
        return _FakeConn("astra-gw-gemini", "gemini",
                         image_models=[GEMINI_IMG], outcomes=outcomes)

    def test_exact_id_is_registered_and_free(self):
        self.assertTrue(is_image_model("gemini", GEMINI_IMG))
        self.assertTrue(is_free_image_model("gemini", GEMINI_IMG))
        spec = image_spec("gemini", GEMINI_IMG)
        self.assertEqual(spec.model, GEMINI_IMG)       # exact id kept
        self.assertEqual(spec.free_tier, FREE_TRUE)
        self.assertIn(IMAGE_GENERATION, spec.capabilities)
        self.assertIn("image", spec.output_modalities)

    def test_pool_contains_cloudflare_and_the_gemini_model(self):
        pool = set(image_pool())
        for mid in CF_POOL:
            self.assertIn(("cloudflare", mid), pool, mid)
        self.assertIn(("gemini", GEMINI_IMG), pool)

    def test_gateway_targets_include_the_gemini_model(self):
        gw = self._gw(self._gemini())
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertIn(GEMINI_IMG, ids)

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
        self.assertEqual([p for _m, p in gem.image_calls], ["a cat"])
        self.assertEqual(gem.chat_calls, 0)


class TestEmptyImageModelEnvSemantics(unittest.TestCase):
    """Regression test documenting the INTENTIONAL semantics when an
    ``*_IMAGE_MODELS`` env var is explicitly empty (``FOO_IMAGE_MODELS=``)
    -- indistinguishable, at the config layer, from leaving it unset
    entirely (``Config.getlist`` returns ``[]`` either way; see
    ``_GatewayCompatibleConnection._env_list``). ``ImageRouter._catalog``
    then falls back to this provider's documented FREE default models
    (semantics B), NOT to disabling the provider (semantics A) -- Cloudflare
    and Gemini both have a non-empty documented default, so an
    explicitly-empty env var still yields a usable pool. OpenRouter's
    documented default is itself intentionally empty (its FREE pool is
    live-discovery-only), so an explicitly-empty env var there correctly
    yields NO static models -- not an error, and not a fallback to a paid
    model."""

    def _gw(self, *conns, config=None, events=None):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), config=config,
                              events=events)

    def test_explicitly_empty_cloudflare_env_uses_documented_defaults(self):
        from astra.ai.gateway import AstraGatewayCloudflare
        conn = AstraGatewayCloudflare(_cfg(
            GW_CLOUDFLARE_API_KEYS="k", GW_CLOUDFLARE_ACCOUNT_IDS="acct",
            CLOUDFLARE_IMAGE_MODELS="",
            IMAGE_CLOUDFLARE_API_KEY="image-key",
            IMAGE_CLOUDFLARE_ACCOUNT_ID="image-acct"))
        # The env layer cannot tell "explicitly empty" from "unset".
        self.assertEqual(conn.image_models, [])
        gw = self._gw(conn)
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertTrue(ids)
        self.assertEqual(set(ids), set(documented_image_models("cloudflare")))

    def test_explicitly_empty_gemini_env_uses_documented_defaults(self):
        from astra.ai.gateway import AstraGatewayGemini
        conn = AstraGatewayGemini(_cfg(
            GW_GEMINI_API_KEYS="k", GEMINI_IMAGE_MODELS="",
            IMAGE_GEMINI_API_KEY="image-key"))
        self.assertEqual(conn.image_models, [])
        gw = self._gw(conn)
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertTrue(ids)
        self.assertEqual(set(ids), set(documented_image_models("gemini")))

    def test_explicitly_empty_openrouter_env_stays_empty_not_paid(self):
        from astra.ai.gateway import AstraGatewayOpenRouter
        conn = AstraGatewayOpenRouter(config=_cfg(
            GW_OPENROUTER_API_KEYS="k", OPENROUTER_IMAGE_MODELS="",
            IMAGE_OPENROUTER_API_KEY="image-key"))
        self.assertEqual(conn.image_models, [])
        # OpenRouter's own documented default is intentionally empty (its
        # FREE image pool is live-discovery-only) -- an explicitly-empty
        # env var must stay empty, never fall back to any paid model.
        self.assertEqual(documented_image_models("openrouter"), ())
        gw = self._gw(conn)
        self.assertEqual(gw.image_targets(discover=False), [])


class TestOpenRouterStaticPoolIsEmpty(unittest.TestCase):
    """LIVE-VERIFIED 2026-09-26: OpenRouter's image catalog
    (GET https://openrouter.ai/api/v1/images/models) contains ZERO `:free`
    image-output models, so its static FREE pool must be EMPTY and every
    previously force-included `:free` id must be recorded as rejected. A
    paid model must never be substituted in."""

    def _gw(self, *conns, config=None, events=None):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), config=config,
                              events=events)

    def test_no_openrouter_model_is_in_the_static_pool(self):
        self.assertEqual(documented_image_models("openrouter"), ())
        self.assertEqual([m for p, m in image_pool() if p == "openrouter"], [])

    def test_removed_ids_are_rejected_with_an_evidence_reason(self):
        for mid in OR_DEAD_FREE_IDS:
            self.assertFalse(is_image_model("openrouter", mid), mid)
            self.assertFalse(is_free_image_model("openrouter", mid), mid)
            self.assertIn(("openrouter", mid), REJECTED_IMAGE_MODELS, mid)
            reason = rejected_image_reason("openrouter", mid)
            self.assertTrue(reason, mid)
            self.assertIn("2026-09-26", reason, mid)

    def test_removed_ids_never_appear_in_image_priority(self):
        from astra.ai.image_models import IMAGE_PRIORITY
        for mid in OR_DEAD_FREE_IDS:
            self.assertNotIn(mid, IMAGE_PRIORITY, mid)

    def test_configured_dead_openrouter_ids_never_enter_the_pool(self):
        # Even if an operator still has the old ids in env, the registry's
        # eligibility filter must keep them out of the target list, and no
        # chat/text/paid call may be attempted instead.
        orc = _FakeConn("astra-gw-openrouter", "openrouter",
                        image_models=list(OR_DEAD_FREE_IDS))
        gw = self._gw(orc)
        self.assertEqual(gw.image_targets(discover=False), [])
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(orc.image_calls, [])
        self.assertEqual(orc.chat_calls, 0)

    def test_gateway_image_targets_use_only_the_real_free_pool(self):
        from astra.ai.image_models import IMAGE_PRIORITY
        gem = _FakeConn("astra-gw-gemini", "gemini",
                        image_models=[GEMINI_IMG])
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=list(CF_POOL))
        orc = _FakeConn("astra-gw-openrouter", "openrouter",
                        image_models=list(OR_DEAD_FREE_IDS))
        gw = self._gw(gem, cf, orc)
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids, list(IMAGE_PRIORITY))
        for mid in OR_DEAD_FREE_IDS:
            self.assertNotIn(mid, ids)


class TestSerialFallbackAndGlobalOrdering(unittest.TestCase):
    """Serial fallback over the REAL FREE pool, in ONE global model-by-model
    order that crosses provider boundaries -- never grouped per provider."""

    def _gw(self, *conns, config=None, events=None):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), config=config,
                              events=events)

    def _gemini(self, outcomes=None):
        return _FakeConn("astra-gw-gemini", "gemini",
                         image_models=[GEMINI_IMG], outcomes=outcomes)

    def _cf(self, models=None, outcomes=None):
        return _FakeConn("astra-gw-cloudflare", "cloudflare",
                         image_models=list(models if models is not None
                                           else CF_POOL),
                         outcomes=outcomes)

    def test_serial_fallback_crosses_providers(self):
        # GLOBAL order: Gemini (timeout) -> lucid-origin (429) -> phoenix
        # (success): two providers tried model-by-model, never as groups.
        cf = self._cf(models=[LUCID, PHOENIX],
                      outcomes=[_http_error("u", 429)])
        gem = self._gemini(outcomes=[TimeoutError("slow")])
        gw = self._gw(gem, cf)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(gw.last_model, PHOENIX)
        self.assertEqual(gw.last_attempts, 3)
        self.assertEqual([m for m, _p in gem.image_calls], [GEMINI_IMG])
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID, PHOENIX])
        # no proactive health/cooldown state is written for image generation:
        # every entry is still healthy with zero recorded failures.
        health = gw.routing_state.snapshot()["model_health"]
        self.assertTrue(health)
        for entry in health.values():
            self.assertTrue(entry["healthy"])
            self.assertEqual(entry["failure_count"], 0)
            self.assertEqual(entry["cooldown_until"], 0)

    def test_429_moves_to_the_next_model_with_no_same_model_retry(self):
        # A 429 on the Gemini model must advance to the next global model in
        # ONE step: no second attempt against gemini-2.5-flash-image.
        gem = self._gemini(outcomes=[_http_error("u", 429)])
        cf = self._cf(models=[LUCID])
        gw = self._gw(gem, cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, LUCID)
        self.assertEqual([m for m, _p in gem.image_calls], [GEMINI_IMG])
        self.assertEqual(gw.last_attempts, 2)

    def test_cloudflare_5xx_moves_to_the_next_cloudflare_model(self):
        cf = self._cf(models=[LUCID, PHOENIX],
                      outcomes=[_http_error("u", 502)])
        gw = self._gw(cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, PHOENIX)
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID, PHOENIX])

    def test_unavailable_model_moves_to_the_next(self):
        gem = self._gemini(outcomes=[_http_error("u", 404)])
        cf = self._cf(models=[LUCID])
        gw = self._gw(gem, cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual(gw.last_model, LUCID)

    def test_no_model_is_attempted_twice_in_one_request(self):
        gem = self._gemini(outcomes=[ProviderError("boom")])
        cf = self._cf(outcomes=[ProviderError("boom")] * len(CF_POOL))
        gw = self._gw(gem, cf)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        self.assertEqual(len(gem.image_calls), 1)
        seen = [m for m, _p in cf.image_calls]
        self.assertEqual(len(seen), len(set(seen)))

    def test_full_pool_failure_gives_the_clear_error(self):
        gem = self._gemini(outcomes=[ProviderError("boom")])
        cf = self._cf(outcomes=[ProviderError("boom")] * len(CF_POOL))
        gw = self._gw(gem, cf)
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("a cat", discover=False)
        self.assertIn("All available FREE image-generation models failed",
                      str(ctx.exception))
        self.assertEqual(gw.last_attempts, len(CF_POOL) + 1)

    def test_never_falls_back_to_a_text_paid_or_vision_model(self):
        gem = self._gemini(outcomes=[ProviderError("boom")])
        cf = self._cf(outcomes=[ProviderError("boom")] * len(CF_POOL))
        text = _FakeConn("astra-gw-groq", "groq", models=["llama-70b"])
        vision = _FakeConn("astra-gw-cohere", "cohere",
                           models=["command-a-vision-07-2025"])
        paid = _FakeConn("astra-gw-zai", "zai", image_models=["glm-image"])
        paid_gemini = _FakeConn("astra-gw-gemini", "gemini",
                                image_models=["gemini-3.1-flash-image"])
        dead_or = _FakeConn("astra-gw-openrouter", "openrouter",
                            image_models=list(OR_DEAD_FREE_IDS))
        gw = self._gw(gem, cf, text, vision, paid, paid_gemini, dead_or)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        for conn in (text, vision, paid, paid_gemini, dead_or):
            self.assertEqual(conn.chat_calls, 0, conn.name)
            self.assertEqual(conn.image_calls, [], conn.name)

    def test_configured_priority_can_reorder_without_code_edits(self):
        gem = self._gemini()
        cf = self._cf(models=[LUCID])
        gw = self._gw(gem, cf, config=_cfg(
            GW_IMAGE_GENERATION_PRIORITY=LUCID + "," + GEMINI_IMG))
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids[:2], [LUCID, GEMINI_IMG])

    def test_legacy_priority_var_is_a_fallback_when_canonical_is_unset(self):
        """IMAGE_GENERATION_PRIORITY (no GW_ prefix) is a documented legacy
        fallback alias for GW_IMAGE_GENERATION_PRIORITY, consulted only when
        the canonical var is unset/empty -- not a second, independently
        configurable priority list."""
        gem = self._gemini()
        cf = self._cf(models=[LUCID])
        gw = self._gw(gem, cf, config=_cfg(
            IMAGE_GENERATION_PRIORITY=LUCID + "," + GEMINI_IMG))
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids[:2], [LUCID, GEMINI_IMG])

    def test_canonical_priority_var_wins_over_legacy_when_both_set(self):
        gem = self._gemini()
        cf = self._cf(models=[LUCID])
        gw = self._gw(gem, cf, config=_cfg(
            GW_IMAGE_GENERATION_PRIORITY=GEMINI_IMG,
            IMAGE_GENERATION_PRIORITY=LUCID))
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids[0], GEMINI_IMG)

    def test_env_list_changes_the_pool_without_code_edits(self):
        conn = _FakeConn("astra-gw-cloudflare", "cloudflare",
                         image_models=[PHOENIX])
        gw = self._gw(conn)
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids, [PHOENIX])

    # -- GLOBAL, model-by-model ordering: provider boundaries are irrelevant --

    def test_image_priority_is_the_agreed_global_order(self):
        from astra.ai.image_models import IMAGE_PRIORITY
        self.assertEqual(list(IMAGE_PRIORITY), [
            GEMINI_IMG,
            LUCID,
            PHOENIX,
            FLUX,
            SDXL,
            LIGHTNING,
            DREAM,
            INPAINT,
        ])

    def test_full_pool_priority_is_exactly_image_priority(self):
        from astra.ai.image_models import IMAGE_PRIORITY
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=list(CF_POOL))
        gw = self._gw(cf, self._gemini())
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids, list(IMAGE_PRIORITY))

    def test_connection_registration_order_never_changes_priority(self):
        from astra.ai.image_models import IMAGE_PRIORITY

        def ordered(*conns):
            gw = self._gw(*conns)
            return [m.model_id for _c, m, _h in
                    gw.image_targets(discover=False)]

        a = ordered(
            _FakeConn("astra-gw-cloudflare", "cloudflare",
                      image_models=list(CF_POOL)),
            self._gemini())
        b = ordered(
            self._gemini(),
            _FakeConn("astra-gw-cloudflare", "cloudflare",
                      image_models=list(CF_POOL)))
        self.assertEqual(a, b)
        self.assertEqual(a, list(IMAGE_PRIORITY))

    def test_gemini_then_cloudflare_chain_is_model_by_model(self):
        # The exact global sequence, crossing the provider boundary:
        # gemini fails -> lucid fails -> phoenix fails -> flux succeeds.
        gem = self._gemini(outcomes=[ProviderError("boom")])
        cf = self._cf(models=[FLUX, LUCID, PHOENIX],
                      outcomes=[ProviderError("boom"),
                                ProviderError("boom")])
        gw = self._gw(gem, cf)
        out = gw.generate_image("akta cat photo create kore dao",
                                discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        attempted = ([m for m, _p in gem.image_calls]
                     + [m for m, _p in cf.image_calls])
        self.assertEqual(attempted, [GEMINI_IMG, LUCID, PHOENIX, FLUX])
        self.assertEqual(gw.last_model, FLUX)
        self.assertEqual(gw.last_attempts, 4)

    def test_cloudflare_pool_is_not_tried_before_higher_priority_models(self):
        # Gemini fails, then the NEXT model in IMAGE_PRIORITY (a Cloudflare
        # one) is tried and succeeds -- FLUX/PHOENIX are never reached, so no
        # "try the whole Cloudflare pool" pass happens.
        gem = self._gemini(outcomes=[ProviderError("boom")])
        cf = self._cf(models=[FLUX, LUCID, PHOENIX])
        gw = self._gw(gem, cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID])
        self.assertEqual(gw.last_model, LUCID)
        self.assertEqual(gw.last_attempts, 2)

    def test_attempt_sequence_is_the_global_list_not_provider_groups(self):
        # Every model fails: the attempt order must equal IMAGE_PRIORITY
        # exactly, so providers are interleaved as the list dictates.
        from astra.ai.image_models import IMAGE_PRIORITY
        gem = self._gemini(outcomes=[ProviderError("boom")])
        cf = self._cf(outcomes=[ProviderError("boom")] * len(CF_POOL))
        gw = self._gw(gem, cf)
        with self.assertRaises(ProviderError):
            gw.generate_image("a cat", discover=False)
        seen = ([m for m, _p in gem.image_calls]
                + [m for m, _p in cf.image_calls])
        self.assertEqual(seen, list(IMAGE_PRIORITY))
        self.assertEqual(len(seen), len(set(seen)))   # never retried

    def test_override_is_a_global_list_cloudflare_then_gemini(self):
        cf = self._cf(models=[LUCID], outcomes=[_http_error("u", 429)])
        gem = self._gemini()
        gw = self._gw(cf, gem, config=_cfg(
            GW_IMAGE_GENERATION_PRIORITY=LUCID + "," + GEMINI_IMG))
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID])
        self.assertEqual(gw.last_model, GEMINI_IMG)

    def test_override_cannot_add_a_text_vision_paid_or_dead_model(self):
        gem = self._gemini()
        gw = self._gw(gem, config=_cfg(
            GW_IMAGE_GENERATION_PRIORITY=(
                LLAMA + ",gemini-3.1-flash-image,"
                + "command-a-vision-07-2025," + OR_DEAD_FREE_IDS[0] + ","
                + GEMINI_IMG)))
        ids = [m.model_id for _c, m, _h in gw.image_targets(discover=False)]
        self.assertEqual(ids, [GEMINI_IMG])


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

    # D. 5xx then 429 then success -> third model in the GLOBAL order
    def test_d_multi_failure_chain_reaches_the_third_target(self):
        # Present models {LUCID, FLUX, LIGHTNING} rank LUCID < FLUX <
        # LIGHTNING in IMAGE_PRIORITY, so the serial order is exactly that.
        cf = self._cf(models=(FLUX, LIGHTNING, LUCID),
                      outcomes=[_http_error("u", 500),
                                _http_error("u", 429)])
        gw = self._gw(cf)
        out = gw.generate_image("create a photo of a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual([m for m, _p in cf.image_calls],
                         [LUCID, FLUX, LIGHTNING])
        self.assertEqual(gw.last_model, LIGHTNING)
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
        # LUCID is first in IMAGE_PRIORITY among these three, so it is the
        # only model called: success stops the chain at attempt #1.
        cf = self._cf(models=(FLUX, LIGHTNING, LUCID))
        gw = self._gw(cf)
        self.assertTrue(gw.generate_image("a cat", discover=False))
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID])
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

    # H. two credentials, same target: a 429 is NOT retried on the same model
    def test_h_rate_limited_model_is_not_retried_with_another_credential(self):
        # Image generation makes exactly ONE provider attempt per model, so a
        # 429 must never spend a second key on the same model. Recovery comes
        # from the serial fallback advancing to the NEXT image model (see
        # tests/test_image_provider_api_contract.py
        # ::TestImageGenerationNeverRetriesTheSameModel).
        from astra.ai.gateway import AstraGatewayCloudflare
        cfg = _cfg(GW_CLOUDFLARE_API_KEYS="key-one,key-two",
                   GW_CLOUDFLARE_ACCOUNT_IDS="acct1")
        conn = AstraGatewayCloudflare(config=cfg)
        calls = {"n": 0}

        def side(req, timeout=None):
            calls["n"] += 1
            raise _http_error(req.full_url, 429)

        with mock.patch("urllib.request.urlopen", side):
            with self.assertRaises(ProviderError):
                conn.generate_image("a cat", model=FLUX)
        self.assertEqual(calls["n"], 1)      # one attempt per model, no retry

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

    def test_editing_requires_a_source_image(self):
        gw = self._gw(self._gemini())
        with self.assertRaises(ProviderError) as ctx:
            gw.generate_image("ei photo ta edit kore dao", editing=True,
                              discover=False)
        self.assertIn("source image", str(ctx.exception).lower())

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
                                           ZAI_IMAGE_MODELS="glm-image"))
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
# 6. Provider router: image requests are REFUSED (ImageRouter owns execution)
# ═══════════════════════════════════════════════════════════════════════════
class TestProviderRouterRefusesImageExecution(unittest.TestCase):
    """AstraRouter must NOT be a second image execution owner.

    Image generation is owned EXCLUSIVELY by the Gateway's ImageRouter
    (astra/ai/image_router.py), so the Provider router refuses an
    image_generation/image_editing task instead of dispatching it. The old
    serial image loop (`_route_image_serial`) and the `_attempt()`-level
    `adapter.generate_image()` dispatch were a duplicate execution path and
    have been removed.
    """

    def test_router_refuses_an_image_task_without_calling_any_adapter(self):
        text = _FakeConn("groq", "groq", models=["llama-70b"])
        img = _FakeConn("cloudflare", "cloudflare", image_models=[FLUX])
        router = AstraRouter([text, img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_generation",
            messages=[{"role": "user",
                       "content": "akta cat photo create kore dao"}]))
        self.assertFalse(rr.ok)
        self.assertIn("ImageRouter", rr.error or "")
        # No provider was asked to generate an image, and no text model was
        # asked to describe one.
        self.assertEqual(img.image_calls, [])
        self.assertEqual(img.chat_calls, 0)
        self.assertEqual(text.chat_calls, 0)
        self.assertEqual(text.image_calls, [])

    def test_router_refuses_image_editing_too(self):
        img = _FakeConn("cloudflare", "cloudflare", image_models=[FLUX])
        router = AstraRouter([img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="image_editing",
            messages=[{"role": "user", "content": "ei photo ta edit koro"}]))
        self.assertFalse(rr.ok)
        self.assertIn("ImageRouter", rr.error or "")
        self.assertEqual(img.image_calls, [])

    def test_router_refuses_an_image_output_modality_requirement(self):
        # Defense in depth: even without the image task_type, a request that
        # REQUIRES an image output must never be executed by the Provider
        # router -- it has no image dispatch left to serve it.
        img = _FakeConn("cloudflare", "cloudflare", image_models=[FLUX])
        router = AstraRouter([img], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="simple_chat",
            required_output_modalities=["image"],
            messages=[{"role": "user", "content": "draw a cat"}]))
        self.assertFalse(rr.ok)
        self.assertEqual(img.image_calls, [])

    def test_provider_router_has_no_image_dispatch_left(self):
        # Static proof: the duplicate image execution path is gone.
        import inspect
        import astra.ai.router as router_mod
        src = inspect.getsource(router_mod)
        self.assertNotIn("generate_image", src)
        self.assertNotIn("_route_image_serial", src)
        self.assertNotIn("_image_failure_category", src)
        self.assertFalse(hasattr(AstraRouter, "_route_image_serial"))
        self.assertFalse(hasattr(AstraRouter, "_image_failure_category"))

    def test_router_still_serves_normal_chat(self):
        text = _FakeConn("groq", "groq", models=["llama-70b"])
        router = AstraRouter([text], max_retries=0)
        rr = router.route_request(RoutingRequest(
            task_type="simple_chat",
            messages=[{"role": "user", "content": "hello"}]))
        self.assertTrue(rr.ok, rr.error)
        self.assertEqual(text.chat_calls, 1)


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


# ═══════════════════════════════════════════════════════════════════════════
# 7. Artifacts + frontend rendering
# ═══════════════════════════════════════════════════════════════════════════
class TestOpenRouterImagesApi(unittest.TestCase):
    """OpenRouter's current documented Image API is the dedicated
    ``POST /api/v1/images`` endpoint with ``GET /api/v1/images/models``
    discovery -- NOT the OpenAI-style ``/images/generations``."""

    OR_URL = "https://openrouter.ai/api/v1/images"
    OR_DISCOVERY = "https://openrouter.ai/api/v1/images/models"

    def _gw(self):
        from astra.ai.gateway import AstraGatewayOpenRouter
        return AstraGatewayOpenRouter(config=_cfg(GW_OPENROUTER_API_KEYS="k"))

    def _adapter(self):
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        return OpenRouterAdapter(config=_cfg(OPENROUTER_API_KEYS="k"))

    def _gw_gateway(self, *conns, **env):
        from astra.ai.gateway import AstraAIGateway
        return AstraAIGateway(connections=list(conns), config=_cfg(**env))

    def test_discovery_url_is_the_dedicated_images_models_endpoint(self):
        from astra.ai.gateway import AstraGatewayOpenRouter
        from astra.ai.adapters.openrouter import OpenRouterAdapter
        self.assertEqual(AstraGatewayOpenRouter.IMAGE_MODELS_URL,
                         self.OR_DISCOVERY)
        self.assertEqual(OpenRouterAdapter.IMAGE_MODELS_URL,
                         self.OR_DISCOVERY)

    def test_discovery_requests_the_images_models_url(self):
        conn = self._gw()
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            return _resp(json.dumps({"data": [
                {"id": OR_SYNTH,
                 "architecture": {"output_modalities": ["image"]}}]}))

        with mock.patch("urllib.request.urlopen", side):
            ids = conn.list_image_models(discover=True)
        self.assertEqual(ids, [OR_SYNTH])
        self.assertEqual(seen["url"], self.OR_DISCOVERY)

    def test_gateway_generate_image_calls_the_exact_images_endpoint(self):
        conn = self._gw()
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_openai_images_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model=OR_SYNTH)
        self.assertEqual(seen["url"], self.OR_URL)
        self.assertTrue(out.startswith("data:image/png;base64,"))

    def test_adapter_generate_image_calls_the_exact_images_endpoint(self):
        conn = self._adapter()
        seen = {}

        def side(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return _resp(_openai_images_ok())

        with mock.patch("urllib.request.urlopen", side):
            out = conn.generate_image("a cat", model=OR_SYNTH)
        self.assertEqual(seen["url"], self.OR_URL)
        self.assertTrue(out.startswith("data:image/png;base64,"))

    def test_images_generations_is_never_called(self):
        # Regression: the stale OpenAI-style path must never be hit, for the
        # Gateway connection or the standalone adapter.
        urls = []
        for conn in (self._gw(), self._adapter()):
            def side(req, timeout=None):
                urls.append(req.full_url)
                return _resp(_openai_images_ok())
            with mock.patch("urllib.request.urlopen", side):
                conn.generate_image("a cat", model=OR_SYNTH)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("/images/generations" not in u for u in urls), urls)
        self.assertTrue(all(u.endswith("/images") for u in urls), urls)

    def test_request_body_omits_response_format(self):
        conn = self._gw()
        seen = {}

        def side(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return _resp(_openai_images_ok())

        with mock.patch("urllib.request.urlopen", side):
            conn.generate_image("a cat", model=OR_SYNTH, size="2048x2048")
        body = seen["body"]
        self.assertNotIn("response_format", body)
        self.assertEqual(body["model"], OR_SYNTH)
        self.assertEqual(body["prompt"], "a cat")
        self.assertEqual(body["size"], "2048x2048")
        # only documented Image API request fields are sent
        self.assertEqual(set(body) - {"model", "prompt", "n", "size"}, set())

    def test_b64_json_response_becomes_an_image_artifact(self):
        conn = self._gw()

        def side(req, timeout=None):
            return _resp(json.dumps({"data": [{"b64_json": B64,
                                              "media_type": "image/png"}]}))

        with mock.patch("urllib.request.urlopen", side):
            uri = conn.generate_image("a cat", model=OR_SYNTH)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), PNG)
        from astra.ai.artifact_extraction import extract_artifacts
        with tempfile.TemporaryDirectory() as d:
            arts = extract_artifacts(uri, d, "image")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["filename"].endswith(".png"))

    def test_error_statuses_propagate_as_provider_error(self):
        conn = self._gw()
        for code in (400, 401, 402, 403, 404, 413, 429, 500, 502, 524, 529):
            def side(req, timeout=None, _c=code):
                raise _http_error(self.OR_URL, _c)
            with mock.patch("urllib.request.urlopen", side):
                with self.assertRaises(ProviderError):
                    conn.generate_image("a cat", model=OR_SYNTH)

    def test_failure_falls_over_to_the_next_global_model(self):
        # Priority override puts one Cloudflare model first; a 429 on it must
        # advance to the next model in the ONE global list (Cloudflare FLUX),
        # and the dead OpenRouter ids must stay out of the pool entirely.
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=[FLUX, LUCID],
                       outcomes=[_http_error("u", 429)])
        orc = _FakeConn("astra-gw-openrouter", "openrouter",
                        image_models=list(OR_DEAD_FREE_IDS))
        gw = self._gw_gateway(cf, orc, GW_IMAGE_GENERATION_PRIORITY=LUCID
                              + "," + FLUX)
        out = gw.generate_image("a cat", discover=False)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID, FLUX])
        self.assertEqual(orc.image_calls, [])
        self.assertEqual(gw.last_model, FLUX)
        self.assertEqual(gw.last_attempts, 2)


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
class _TempArtifactDirMixin:
    """Give a TestCase a deterministic, writable artifact directory that is
    removed automatically after the test.

    Without this, ChatPipeline would fall back to a shared system temp path
    (``{tempdir}/astra/artifacts``) whose permissions this test does not own.
    """

    def setUp(self):
        super().setUp()
        self._artifacts_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._artifacts_tmp.cleanup)
        self.artifact_dir = self._artifacts_tmp.name

    def _pipeline(self, gw, rt):
        from astra.ai.chat_pipeline import ChatPipeline
        return ChatPipeline(gw, rt, artifact_dir=self.artifact_dir)


class TestPipelineImageWiring(_TempArtifactDirMixin, unittest.TestCase):
    """Real ChatPipeline with a scripted Gateway + scripted router.

    The Gateway double deliberately has NO `generate_image` execution
    method of its own -- per the required architecture, ChatPipeline must
    talk to `gateway.image_router.generate(...)`, never to
    `gateway.generate_image(...)`. `_ImageRouterDouble` stands in for the
    dedicated Image Provider/Model Router (astra.ai.image_router).
    """

    class _ImageRouterDouble:
        def __init__(self, gw):
            self._gw = gw

        def generate(self, prompt, model=None, size="1024x1024", n=1, *,
                     editing=False, trace="", discover=True):
            self._gw.image_calls.append((prompt, model, editing))
            self._gw.last_model = FLUX
            return DATA_URI

    class _Gateway:
        def __init__(self, usable=True):
            self.usable = usable
            self.calls = []
            self.categories = []
            self.last_model = ""
            self.image_calls = []
            self.image_router = TestPipelineImageWiring._ImageRouterDouble(self)

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

        def generate_image(self, *a, **kw):  # pragma: no cover
            raise AssertionError(
                "ChatPipeline must call gateway.image_router.generate(), "
                "never gateway.generate_image()")

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
        gw, rt = self._Gateway(), self._Router()
        out = self._pipeline(gw, rt).run("akta cat photo create kore dao")
        self.assertTrue(out["ok"])
        self.assertEqual(gw.image_calls[0][0], "akta cat photo create kore dao")
        self.assertEqual(rt.requests, [])          # router not used
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        _assert_readable_png_artifact(self, arts[0], self.artifact_dir)
        self.assertNotIn("base64,", out["reply"])

    def test_edit_request_uses_editing_mode(self):
        gw, rt = self._Gateway(), self._Router()
        self._pipeline(gw, rt).run("ei photo ta edit kore dao")
        self.assertTrue(gw.image_calls[0][2], "editing flag must be set")

    def test_gateway_without_image_path_reports_no_model_never_uses_router(self):
        # Image generation is owned EXCLUSIVELY by ImageRouter: with no image
        # path at all the turn must report the honest "no image model" failure
        # and must NOT fall back to the Provider router's duplicate image
        # dispatch (and never to a text answer).
        gw, rt = self._Gateway(), self._Router()
        gw.image_router = None                      # no Image Router at all
        out = self._pipeline(gw, rt).run("photo generate koro")
        self.assertFalse(out["ok"])
        self.assertIn("No currently available FREE image-generation model",
                      out["reply"])
        self.assertEqual(rt.requests, [])

    def test_no_free_image_model_gives_a_clear_error_and_no_text_answer(self):
        gw, rt = self._Gateway(), self._Router()
        gw.image_router = None
        out = self._pipeline(gw, rt).run("akta cat photo create kore dao")
        self.assertFalse(out["ok"])
        self.assertIn("No currently available FREE image-generation model",
                      out["reply"])
        self.assertEqual(rt.requests, [])           # never a router/text retry

    def test_gateway_image_error_never_falls_back_to_router_or_text_chat(self):
        gw, rt = self._Gateway(), self._Router()

        def boom(*a, **kw):
            raise ProviderError("No image-generation model is currently "
                                "configured or available.")

        gw.image_router.generate = boom
        out = self._pipeline(gw, rt).run("akta cat photo create kore dao")
        self.assertFalse(out["ok"])
        self.assertEqual(rt.requests, [])


# ═══════════════════════════════════════════════════════════════════════════
# 8b. End-to-end turn: real ChatPipeline + real AstraAIGateway, provider
#     HTTP mocked at the adapter boundary (no image credentials here).
# ═══════════════════════════════════════════════════════════════════════════
class TestEndToEndImageTurn(_TempArtifactDirMixin, unittest.TestCase):
    _BRIEF = ('{"final_request": "x", "was_incomplete": false, '
              '"provider": "", "model": "", "criteria": [], "reason": "", '
              '"execution": {"required": false, "capability": "", '
              '"environment": "agent_runtime", "approval_required": false, '
              '"intent": ""}}')

    def _run(self, text, *conns):
        from astra.ai.gateway import AstraAIGateway
        real = AstraAIGateway(connections=list(conns))

        class Gw:
            """Entry/classification-only Gateway double: it exposes NO
            `generate_image` execution path of its own. The real image
            execution below goes through `real.image_router.generate(...)`
            -- the actual `ImageRouter` -- never through `real.generate_image`
            directly (which is now only a backward-compat wrapper) nor
            through this `Gw` double."""

            def __init__(self):
                self.image_calls = []
                self.last_model = ""

            def is_usable(self):
                return True

            def chat(self, messages, max_tokens=None, category=None, trace=""):
                return TestEndToEndImageTurn._BRIEF

            def supervise_task(self, *a, **kw):  # pragma: no cover
                raise AssertionError("image turns must not be verified")

            def generate_image(self, *a, **kw):  # pragma: no cover
                raise AssertionError(
                    "ChatPipeline must use gateway.image_router.generate(), "
                    "never gateway.generate_image()")

        gw = Gw()

        class ImageRouterProxy:
            """Stands in for `gw.image_router` and forwards to the REAL
            `ImageRouter` instance owned by the real Gateway (`real`), so
            this end-to-end test still exercises the actual provider-adapter
            dispatch / serial-fallback code, not a re-implementation."""

            def generate(self, prompt, model=None, size="1024x1024", n=1, *,
                         editing=False, trace="", discover=True):
                gw.image_calls.append((prompt, model, editing))
                uri = real.image_router.generate(
                    prompt, model=model, size=size, n=n, editing=editing,
                    trace=trace, discover=False)
                gw.last_model = real.last_model
                return uri

        gw.image_router = ImageRouterProxy()

        rt = TestPipelineImageWiring._Router()
        out = self._pipeline(gw, rt).run(text)
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
        _assert_readable_png_artifact(self, arts[0], self.artifact_dir)
        self.assertNotIn("base64,", out["reply"])
        health = real.routing_state.snapshot()["model_health"]
        self.assertIn("cloudflare:" + LIGHTNING, health)
        self.assertIn("cloudflare:" + FLUX, health)

    def test_gemini_then_cloudflare_image_turn_yields_an_artifact(self):
        # Gemini 429s, the next global FREE model (Cloudflare lucid-origin)
        # succeeds -- the turn must still produce a real image artifact with no
        # base64 in the reply. OpenRouter's dead ids stay out of the pool.
        gem = _FakeConn("astra-gw-gemini", "gemini",
                        image_models=[GEMINI_IMG],
                        outcomes=[_http_error("u", 429)])
        cf = _FakeConn("astra-gw-cloudflare", "cloudflare",
                       image_models=[LUCID])
        orc = _FakeConn("astra-gw-openrouter", "openrouter",
                        image_models=list(OR_DEAD_FREE_IDS))
        out, gw, real, rt = self._run("akta cat photo create kore dao",
                                      gem, cf, orc)
        self.assertTrue(out["ok"], out.get("reply"))
        self.assertEqual(rt.requests, [])              # router never used
        self.assertEqual(real.last_model, LUCID)
        self.assertEqual([m for m, _p in gem.image_calls], [GEMINI_IMG])
        self.assertEqual([m for m, _p in cf.image_calls], [LUCID])
        self.assertEqual(orc.image_calls, [])
        arts = out.get("artifacts") or []
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["artifact_type"], "image")
        self.assertTrue(arts[0]["mime_type"].startswith("image/"))
        self.assertTrue(arts[0]["id"])
        self.assertTrue(arts[0]["filename"])
        _assert_readable_png_artifact(self, arts[0], self.artifact_dir)
        self.assertNotIn("base64,", out["reply"])


if __name__ == "__main__":
    unittest.main()
