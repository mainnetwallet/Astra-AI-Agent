"""Explicit image-generation capability registry (per provider + model).

Astra must never *infer* image generation from a model name that merely
contains "image", "vision", "omni" or "multimodal", and it must never treat
``vision_input`` (understanding an image) as ``image_generation`` (producing
one). Every entry here therefore comes from one of exactly two sources:

1. a provider's OFFICIAL documentation (the doc URL is recorded on the
   spec), or
2. the provider's own LIVE discovery API (e.g. OpenRouter's
   ``GET /api/v1/models?output_modalities=image``), whose *actual* output
   modalities are authoritative for the exact model id it returns.

The table is intentionally narrow: a model that is not listed (and is not
returned by live discovery) is NOT an image-generation model as far as Astra
is concerned, no matter what its name looks like. ``metadata_for()`` in
``astra.ai.models`` reads this registry, so a model gains the
``image_generation`` capability and the ``image`` output modality only when
this module can point at real evidence.

Verified against (2026-09-25):

* Google — https://ai.google.dev/gemini-api/docs/image-generation
  documented image models: ``gemini-3.1-flash-image``,
  ``gemini-3.1-flash-lite-image``, ``gemini-3-pro-image``,
  ``gemini-2.5-flash-image`` (native ``:generateContent`` with
  ``responseModalities: ["IMAGE"]``).
* Cloudflare Workers AI — https://developers.cloudflare.com/workers-ai/models/
  FLUX text-to-image models (``@cf/black-forest-labs/flux-*``) plus
  Leonardo/ByteDance/Lykon image models, served by ``/ai/run/<model>``.
* AWS Bedrock — Nova Canvas and Titan Image Generator (``InvokeModel``).
* Z.AI — https://docs.z.ai/api-reference/image/generate-image
  ``glm-image`` / ``cogview-4-250304`` via
  ``POST /api/paas/v4/images/generations``.
* OpenRouter — https://openrouter.ai/api/v1/images/models and
  ``GET /api/v1/models?output_modalities=image`` (live; the static list
  below is only a conservative fallback when discovery is unreachable).

Providers with NO image-generation API path in this repository (Groq,
Cerebras, SambaNova, Cohere, Mistral) are deliberately absent: their
official APIs do not expose an image-generation endpoint that this codebase
can call, so their models must never be marked as image-capable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Capability / modality names (kept in sync with astra.ai.capabilities and
# astra.ai.models; duplicated as plain strings so this module stays a leaf
# with no import cycle).
IMAGE_GENERATION = "image_generation"
IMAGE_EDITING = "image_editing"
INPUT_IMAGE = "image"

# API protocols this repository can actually speak for image generation.
PROTOCOL_OPENAI_IMAGES = "openai_images_generations"
PROTOCOL_GEMINI_CONTENT = "gemini_generate_content_image"
PROTOCOL_CLOUDFLARE_RUN = "cloudflare_workers_ai_run"
PROTOCOL_BEDROCK_INVOKE = "bedrock_invoke_model"

SUPPORTED_PROTOCOLS = frozenset({
    PROTOCOL_OPENAI_IMAGES, PROTOCOL_GEMINI_CONTENT,
    PROTOCOL_CLOUDFLARE_RUN, PROTOCOL_BEDROCK_INVOKE,
})

# Providers whose adapter in this repository has a real image-execution path.
# Anything else must be reported unavailable rather than pretended.
SUPPORTED_PROVIDERS = frozenset({
    "gemini", "cloudflare", "openrouter", "bedrock", "zai",
})

# env var holding each provider's optional image-model list
IMAGE_MODELS_ENV = {
    "gemini": "GEMINI_IMAGE_MODELS",
    "cloudflare": "CLOUDFLARE_IMAGE_MODELS",
    "openrouter": "OPENROUTER_IMAGE_MODELS",
    "bedrock": "BEDROCK_IMAGE_MODELS",
    "zai": "ZAI_IMAGE_MODELS",
}

# Gateway (GW_*) equivalents — the Gateway keeps its own, fully independent
# model lists (see astra/ai/gateway.py).
GATEWAY_IMAGE_MODELS_ENV = {
    "astra-gw-gemini": "GW_GEMINI_IMAGE_MODELS",
    "astra-gw-cloudflare": "GW_CLOUDFLARE_IMAGE_MODELS",
    "astra-gw-openrouter": "GW_OPENROUTER_IMAGE_MODELS",
    "astra-gw-bedrock": "GW_BEDROCK_IMAGE_MODELS",
    "astra-gw-zai": "GW_ZAI_IMAGE_MODELS",
}


@dataclass(frozen=True)
class ImageSpec:
    """One (provider, model) image-generation entry with real evidence."""
    provider: str
    model: str
    protocol: str
    source: str
    capabilities: tuple = (IMAGE_GENERATION,)
    input_modalities: tuple = ("text",)
    output_modalities: tuple = ("text", "image")
    sizes: tuple = ()
    # narrow, documented family tokens for versioned/variant ids
    variants: tuple = field(default_factory=tuple)

    @property
    def supports_editing(self) -> bool:
        return IMAGE_EDITING in self.capabilities

    def matches(self, model_id: str) -> bool:
        low = _normalize(model_id)
        if low == _normalize(self.model):
            return True
        for v in self.variants:
            if v and v in low:
                return True
        return False


def _normalize(model_id: str) -> str:
    low = str(model_id or "").strip().lower()
    # Bedrock inference-profile prefixes ("us.", "eu.", "apac.") are not part
    # of the model identity.
    for pref in ("us.", "eu.", "apac."):
        if low.startswith(pref):
            return low[len(pref):]
    return low


# ── the curated table ──────────────────────────────────────────────────────
# Only documented/live-confirmed entries. `variants` are exact documented
# family tokens, never generic words like "image".
_SPECS: tuple = (
    # ── Google Gemini (native :generateContent, responseModalities IMAGE) ──
    ImageSpec("gemini", "gemini-3.1-flash-image", PROTOCOL_GEMINI_CONTENT,
              "https://ai.google.dev/gemini-api/docs/image-generation",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"),
              sizes=("1024x1024", "1344x768", "768x1344"),
              variants=("gemini-3.1-flash-image-preview",)),
    ImageSpec("gemini", "gemini-3.1-flash-lite-image", PROTOCOL_GEMINI_CONTENT,
              "https://ai.google.dev/gemini-api/docs/image-generation",
              capabilities=(IMAGE_GENERATION,),
              input_modalities=("text", "image"),
              sizes=("1024x1024",)),
    ImageSpec("gemini", "gemini-3-pro-image", PROTOCOL_GEMINI_CONTENT,
              "https://ai.google.dev/gemini-api/docs/image-generation",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"),
              sizes=("1024x1024", "2048x2048"),
              variants=("gemini-3-pro-image-preview",)),
    ImageSpec("gemini", "gemini-2.5-flash-image", PROTOCOL_GEMINI_CONTENT,
              "https://ai.google.dev/gemini-api/docs/image-generation",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"),
              sizes=("1024x1024",),
              variants=("gemini-2.5-flash-image-preview",)),

    # ── AWS Bedrock (InvokeModel) ──────────────────────────────────────────
    ImageSpec("bedrock", "amazon.nova-canvas-v1:0", PROTOCOL_BEDROCK_INVOKE,
              "https://docs.aws.amazon.com/nova/latest/userguide/image-gen-req-resp-structure.html",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              sizes=("1024x1024", "1280x720", "768x768")),
    ImageSpec("bedrock", "amazon.titan-image-generator-v2:0", PROTOCOL_BEDROCK_INVOKE,
              "https://docs.aws.amazon.com/bedrock/latest/userguide/titan-image-models.html",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              sizes=("1024x1024", "768x768", "512x512")),
    ImageSpec("bedrock", "amazon.titan-image-generator-v1", PROTOCOL_BEDROCK_INVOKE,
              "https://docs.aws.amazon.com/bedrock/latest/userguide/titan-image-models.html",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              sizes=("1024x1024", "768x768", "512x512")),
    ImageSpec("bedrock", "stability.stable-diffusion-xl-v1", PROTOCOL_BEDROCK_INVOKE,
              "https://docs.aws.amazon.com/bedrock/latest/userguide/stable-diffusion-inference-api.html",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              sizes=("1024x1024", "512x512")),
    ImageSpec("bedrock", "stability.stable-image-core-v1:1", PROTOCOL_BEDROCK_INVOKE,
              "https://docs.aws.amazon.com/bedrock/latest/userguide/stable-image-services.html",
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024",)),
    ImageSpec("bedrock", "stability.stable-image-ultra-v1:1", PROTOCOL_BEDROCK_INVOKE,
              "https://docs.aws.amazon.com/bedrock/latest/userguide/stable-image-services.html",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              sizes=("1024x1024", "2048x2048")),

    # ── Cloudflare Workers AI (/accounts/<id>/ai/run/<model>) ─────────────
    ImageSpec("cloudflare", "@cf/black-forest-labs/flux-1-schnell", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/",
              capabilities=(IMAGE_GENERATION,), output_modalities=("image",),
              sizes=("1024x1024", "512x512")),
    ImageSpec("cloudflare", "@cf/black-forest-labs/flux-2-dev", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/black-forest-labs/flux-2-klein-9b", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION,), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/black-forest-labs/flux-2-klein-4b", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION,), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/leonardo/lucid-origin", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION,), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/leonardo/phoenix-1.0", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/bytedance/stable-diffusion-xl-lightning", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION,), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/lykon/dreamshaper-8-lcm", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION,), output_modalities=("image",),
              sizes=("1024x1024",)),
    ImageSpec("cloudflare", "@cf/runwayml/stable-diffusion-v1-5-img2img", PROTOCOL_CLOUDFLARE_RUN,
              "https://developers.cloudflare.com/workers-ai/models/",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"), output_modalities=("image",),
              sizes=("512x512",)),

    # ── Z.AI (/api/paas/v4/images/generations) ────────────────────────────
    ImageSpec("zai", "glm-image", PROTOCOL_OPENAI_IMAGES,
              "https://docs.z.ai/api-reference/image/generate-image",
              capabilities=(IMAGE_GENERATION,),
              sizes=("1280x1280", "1024x1024")),
    ImageSpec("zai", "cogview-4-250304", PROTOCOL_OPENAI_IMAGES,
              "https://docs.z.ai/api-reference/image/generate-image",
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024", "1344x768")),
    ImageSpec("zai", "cogview-4", PROTOCOL_OPENAI_IMAGES,
              "https://docs.z.ai/api-reference/image/generate-image",
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024",)),

    # ── OpenRouter (static fallback; live discovery is authoritative) ─────
    ImageSpec("openrouter", "google/gemini-3.1-flash-image", PROTOCOL_OPENAI_IMAGES,
              "https://openrouter.ai/api/v1/models?output_modalities=image",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image")),
    ImageSpec("openrouter", "google/gemini-3.1-flash-lite-image", PROTOCOL_OPENAI_IMAGES,
              "https://openrouter.ai/api/v1/models?output_modalities=image",
              capabilities=(IMAGE_GENERATION,), input_modalities=("text", "image")),
    ImageSpec("openrouter", "google/gemini-3-pro-image", PROTOCOL_OPENAI_IMAGES,
              "https://openrouter.ai/api/v1/models?output_modalities=image",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image")),
    ImageSpec("openrouter", "google/gemini-2.5-flash-image", PROTOCOL_OPENAI_IMAGES,
              "https://openrouter.ai/api/v1/models?output_modalities=image",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image")),
    ImageSpec("openrouter", "openai/gpt-5-image", PROTOCOL_OPENAI_IMAGES,
              "https://openrouter.ai/api/v1/models?output_modalities=image",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image")),
    ImageSpec("openrouter", "openai/gpt-5-image-mini", PROTOCOL_OPENAI_IMAGES,
              "https://openrouter.ai/api/v1/models?output_modalities=image",
              capabilities=(IMAGE_GENERATION,), input_modalities=("text", "image")),
)


# ── lookup ─────────────────────────────────────────────────────────────────
def image_spec(provider: str, model_id: str) -> ImageSpec | None:
    """Return the documented ImageSpec for (provider, model_id), or None.

    None means "no evidence this model can generate images" — never a
    guess. Callers must treat it as unsupported.
    """
    p = str(provider or "").strip().lower()
    if not model_id:
        return None
    for spec in _SPECS:
        if spec.provider != p:
            continue
        if spec.matches(model_id):
            return spec
    return None


def is_image_model(provider: str, model_id: str) -> bool:
    return image_spec(provider, model_id) is not None


def is_image_editing_model(provider: str, model_id: str) -> bool:
    spec = image_spec(provider, model_id)
    return bool(spec and spec.supports_editing)


def provider_supports_image_generation(provider: str) -> bool:
    return str(provider or "").strip().lower() in SUPPORTED_PROVIDERS


def documented_image_models(provider: str) -> tuple:
    """Every statically documented image model id for one provider."""
    p = str(provider or "").strip().lower()
    return tuple(s.model for s in _SPECS if s.provider == p)


def make_image_spec(provider: str, model_id: str, protocol: str, *,
                    source: str = "live discovery",
                    capabilities=(IMAGE_GENERATION,),
                    input_modalities=("text",),
                    output_modalities=("text", "image")) -> ImageSpec:
    """Build an ad-hoc spec for a model a provider's LIVE API reported as
    image-capable (e.g. an OpenRouter discovery result). Used only when the
    provider's own API states the output modality — never to invent one."""
    return ImageSpec(provider, model_id, protocol, source,
                     capabilities=tuple(capabilities),
                     input_modalities=tuple(input_modalities),
                     output_modalities=tuple(output_modalities))
