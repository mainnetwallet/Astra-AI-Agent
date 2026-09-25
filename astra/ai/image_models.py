"""Explicit FREE image-generation capability registry (per provider + model).

Astra's photo/image generation must use ONLY models that ALL of the
following hold for:

1. they really produce a new image (not merely understand one),
2. the model id is current in the provider's own catalog,
3. the provider still serves it,
4. Astra has a real, working adapter/API path for it,
5. it is FREE / free-tier usable, verified -- not assumed,
6. it supports the requested operation (generation vs editing),
7. it is healthy at runtime (tracked per (provider, model) elsewhere).

Anything that fails any of these is REMOVED from the image pool. Removing a
provider here NEVER touches its normal chat / reasoning / coding / vision
integration -- those live in the provider adapters and are unaffected.

Free status is verified, never hard-coded from memory. The evidence for
every kept model is recorded on its spec (`free_evidence`), and every
removed model is listed in `REJECTED_IMAGE_MODELS` with the reason, so the
audit is reproducible instead of being a claim in a commit message.

Audited 2026-09-25 against the providers' CURRENT official sources:

* Cloudflare Workers AI -- https://developers.cloudflare.com/workers-ai/models/
  The free allocation is documented on
  https://developers.cloudflare.com/workers-ai/platform/pricing/ :
  "Our free allocation allows anyone to use a total of 10,000 Neurons per
  day at no charge." Image models bill in Neurons and are NOT on the
  paid-billing exception list, so text-to-image models are free-tier usable.
  Kept models are the ones whose published input schema accepts a JSON
  ``{"prompt": ...}`` body (schema-input.json), i.e. the ones Astra's
  Workers AI adapter can actually invoke.

* Google Gemini -- https://ai.google.dev/gemini-api/docs/pricing
  Every Gemini image model (``gemini-3.1-flash-image``,
  ``gemini-3.1-flash-lite-image``, ``gemini-3-pro-image``,
  ``gemini-2.5-flash-image``) lists **Free Tier = Not available** for image
  output, and ``gemini-2.5-flash-image`` is additionally deprecated
  (shutdown 2026-10-02). Paid-only -> removed from the FREE pool.
  (Gemini chat/vision stays fully intact.)

* Amazon Bedrock -- Nova Canvas / Titan Image / Stability via ``InvokeModel``
  are billed per image with no free tier -> removed from the FREE pool.

* Z.AI -- https://docs.z.ai/guides/overview/pricing prices GLM-Image at
  $0.015/image and CogView-4 at $0.01/image (no free tier) -> removed.

* OpenRouter -- https://openrouter.ai/api/v1/models?output_modalities=image
  Live catalog contains ZERO free image-output models: all 18 ``:free``
  models output text only, and every image model is paid
  (``google/gemini-2.5-flash-image`` etc.). The ``:free`` candidates named
  in the audit brief (``google/gemini-2.5-flash-image-preview:free``,
  ``black-forest-labs/flux-1-schnell:free``,
  ``sourceful/riverflow-v2.5-pro:free``) do not exist in the current
  catalog -> OpenRouter contributes no FREE image model.

* Groq, Cerebras, SambaNova, Cohere, Mistral -- no image-generation API path
  in this repository at all -> never image-capable.

Image requests are served by a SIMPLE SERIAL FALLBACK over this FREE pool: the
eligible models are tried one after another in a deterministic, configurable
priority order (``IMAGE_PRIORITY`` / ``image_priority_index`` /
``ordered_image_pool``, overridable with ``IMAGE_GENERATION_PRIORITY`` or
``GW_IMAGE_GENERATION_PRIORITY``). The ACTUAL generation request is the only
availability signal -- there is NO proactive/preflight image health check and
no test image is ever generated. A failure is recorded only for that one
request's attempted-model set, never as a permanent unhealthy state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Capability / modality names (kept in sync with astra.ai.capabilities and
# astra.ai.models; duplicated as plain strings so this module stays a leaf
# with no import cycle).
IMAGE_GENERATION = "image_generation"
IMAGE_EDITING = "image_editing"
INPUT_IMAGE = "image"

# ── free-tier eligibility ──────────────────────────────────────────────────
# Three states, deliberately: a model may be capable but PAID (free_tier
# False) or simply UNVERIFIED (free_tier unknown). Only FREE_TRUE may enter
# the free image pool -- "unknown" is treated exactly like "not free" so an
# unverifiable model can never sneak in.
FREE_TRUE = "true"
FREE_FALSE = "false"
FREE_UNKNOWN = "unknown"

# API protocols this repository can actually speak for image generation.
PROTOCOL_OPENAI_IMAGES = "openai_images_generations"
PROTOCOL_GEMINI_CONTENT = "gemini_generate_content_image"
PROTOCOL_CLOUDFLARE_RUN = "cloudflare_workers_ai_run"
PROTOCOL_BEDROCK_INVOKE = "bedrock_invoke_model"

SUPPORTED_PROTOCOLS = frozenset({
    PROTOCOL_OPENAI_IMAGES, PROTOCOL_GEMINI_CONTENT,
    PROTOCOL_CLOUDFLARE_RUN, PROTOCOL_BEDROCK_INVOKE,
})

# Providers with a verified FREE image-generation tier AND a real adapter
# path in this repository. This is the FREE image pool; nothing else may be
# selected for image generation.
FREE_IMAGE_PROVIDERS = frozenset({"cloudflare"})

#: Backwards-compatible alias (the free pool is the supported image pool).
SUPPORTED_PROVIDERS = FREE_IMAGE_PROVIDERS

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
#: env var holding the optional, comma-separated serial-fallback order (the
#: in-repo ``IMAGE_PRIORITY`` order is the deterministic default).
IMAGE_PRIORITY_ENV = "IMAGE_GENERATION_PRIORITY"
GATEWAY_IMAGE_PRIORITY_ENV = "GW_IMAGE_GENERATION_PRIORITY"

GATEWAY_IMAGE_MODELS_ENV = {
    "astra-gw-gemini": "GW_GEMINI_IMAGE_MODELS",
    "astra-gw-cloudflare": "GW_CLOUDFLARE_IMAGE_MODELS",
    "astra-gw-openrouter": "GW_OPENROUTER_IMAGE_MODELS",
    "astra-gw-bedrock": "GW_BEDROCK_IMAGE_MODELS",
    "astra-gw-zai": "GW_ZAI_IMAGE_MODELS",
}


@dataclass(frozen=True)
class ImageSpec:
    """One (provider, model) FREE image-generation entry with real evidence."""
    provider: str
    model: str
    protocol: str
    source: str
    capabilities: tuple = (IMAGE_GENERATION,)
    input_modalities: tuple = ("text",)
    output_modalities: tuple = ("text", "image")
    sizes: tuple = ()
    free_tier: str = FREE_UNKNOWN
    #: request-body parameters the provider's published schema accepts, so the
    #: adapter never sends an unsupported field.
    params: tuple = ("prompt",)
    #: short, quotable evidence that the model is free-tier usable.
    free_evidence: str = ""
    # narrow, documented family tokens for versioned/variant ids
    variants: tuple = field(default_factory=tuple)

    @property
    def supports_editing(self) -> bool:
        return IMAGE_EDITING in self.capabilities

    @property
    def is_free(self) -> bool:
        return self.free_tier == FREE_TRUE

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


# ── the curated FREE pool ──────────────────────────────────────────────────
# Only models that passed the strict audit. `variants` are exact documented
# family tokens, never generic words like "image".
_CF_FREE = ("https://developers.cloudflare.com/workers-ai/platform/pricing/ "
            "\u2014 10,000 free Neurons/day; image models not paid-only")
_CF_SCHEMA = ("https://developers.cloudflare.com/workers-ai/models/{}/"
              "schema-input.json")

_SPECS: tuple = (
    # ── Cloudflare Workers AI (Workers AI /ai/run/<model>, JSON body) ─────
    # Every entry: official "Text-to-Image" task, JSON input schema requires
    # only `prompt`, and Workers AI's free allocation covers it.
    ImageSpec("cloudflare", "@cf/black-forest-labs/flux-1-schnell",
              PROTOCOL_CLOUDFLARE_RUN, _CF_SCHEMA.format("flux-1-schnell"),
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024",),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "steps")),
    ImageSpec("cloudflare", "@cf/stabilityai/stable-diffusion-xl-base-1.0",
              PROTOCOL_CLOUDFLARE_RUN,
              _CF_SCHEMA.format("stable-diffusion-xl-base-1.0"),
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height")),
    ImageSpec("cloudflare", "@cf/bytedance/stable-diffusion-xl-lightning",
              PROTOCOL_CLOUDFLARE_RUN,
              _CF_SCHEMA.format("stable-diffusion-xl-lightning"),
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height")),
    ImageSpec("cloudflare", "@cf/lykon/dreamshaper-8-lcm",
              PROTOCOL_CLOUDFLARE_RUN, _CF_SCHEMA.format("dreamshaper-8-lcm"),
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height")),
    ImageSpec("cloudflare", "@cf/runwayml/stable-diffusion-v1-5-inpainting",
              PROTOCOL_CLOUDFLARE_RUN,
              _CF_SCHEMA.format("stable-diffusion-v1-5-inpainting"),
              # Text-to-Image per Cloudflare's own task label; the inpainting
              # mode needs a source image+mask, which this adapter does not
              # send, so NO image_editing capability is advertised.
              capabilities=(IMAGE_GENERATION,),
              sizes=("512x512",),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height")),
    ImageSpec("cloudflare", "@cf/leonardo/lucid-origin",
              PROTOCOL_CLOUDFLARE_RUN, _CF_SCHEMA.format("lucid-origin"),
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height")),
    ImageSpec("cloudflare", "@cf/leonardo/phoenix-1.0",
              PROTOCOL_CLOUDFLARE_RUN, _CF_SCHEMA.format("phoenix-1.0"),
              capabilities=(IMAGE_GENERATION,),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height")),
)


# ── deterministic serial-fallback order ───────────────────────────────────
# The order the FREE models are tried in for one image request. This is a
# curated DEFAULT derived from the documented/verified characteristics of each
# model -- it is not a health signal, and it is not claimed to be objectively
# optimal:
#   1. flux-1-schnell      fastest, smallest required param set (`prompt`),
#                          lowest Neuron cost, strongest free availability.
#   2. sdxl-base-1.0       general-purpose high-quality SDXL; accepts
#                          width/height.
#   3. sd-xl-lightning     distilled few-step SDXL: quick, good prompt
#                          adherence, accepts width/height.
#   4. dreamshaper-8-lcm   LCM-tuned SDXL variant, accepts width/height.
#   5. sd-v1-5-inpainting  SD1.5 family (text-to-image mode), 512px class.
#   6. lucid-origin        Leonardo general model, accepts width/height.
#   7. phoenix-1.0         Leonardo model, accepts width/height.
# Reorder per deployment with IMAGE_GENERATION_PRIORITY (or
# GW_IMAGE_GENERATION_PRIORITY for the AI Gateway) -- only ids that already
# pass the FREE + image-generation eligibility rules can ever be selected.
IMAGE_PRIORITY = (
    "@cf/black-forest-labs/flux-1-schnell",
    "@cf/stabilityai/stable-diffusion-xl-base-1.0",
    "@cf/bytedance/stable-diffusion-xl-lightning",
    "@cf/lykon/dreamshaper-8-lcm",
    "@cf/runwayml/stable-diffusion-v1-5-inpainting",
    "@cf/leonardo/lucid-origin",
    "@cf/leonardo/phoenix-1.0",
)

_PRIORITY_INDEX = {mid: i for i, mid in enumerate(IMAGE_PRIORITY)}

#: Raised/returned when every eligible FREE image model was attempted for one
#: request and all of them failed. User-facing and credential-free.
IMAGE_EXHAUSTED_MESSAGE = (
    "All available FREE image-generation models failed to generate the image. "
    "Please try again later.")


# ── explicitly rejected models (audit trail, never selectable) ────────────
_REJECT_PAID_GEMINI = ("paid-only: Gemini image models list Free Tier = "
                       "'Not available' in the official pricing table")
_REJECT_PAID_BEDROCK = ("paid-only: Bedrock InvokeModel image models are "
                        "billed per image with no free tier")
_REJECT_PAID_ZAI = ("paid-only: Z.AI prices GLM-Image $0.015/image and "
                    "CogView-4 $0.01/image; no free tier")
_REJECT_OR_PAID = ("no free model: OpenRouter's live catalog has zero "
                   "':free' image-output models and every image model is paid")

REJECTED_IMAGE_MODELS: dict = {
    # Gemini — capable, but paid-only (and 2.5-flash-image deprecated).
    ("gemini", "gemini-3.1-flash-image"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3.1-flash-image-preview"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3.1-flash-lite-image"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3-pro-image"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3-pro-image-preview"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-2.5-flash-image"): (
        "deprecated (shutdown 2026-10-02) AND paid-only: Free Tier = "
        "'Not available'"),
    ("gemini", "gemini-2.5-flash-image-preview"): (
        "deprecated preview AND paid-only"),
    # Bedrock — capable, paid-only.
    ("bedrock", "amazon.nova-canvas-v1:0"): _REJECT_PAID_BEDROCK,
    ("bedrock", "amazon.titan-image-generator-v1"): _REJECT_PAID_BEDROCK,
    ("bedrock", "amazon.titan-image-generator-v2:0"): _REJECT_PAID_BEDROCK,
    ("bedrock", "stability.stable-diffusion-xl-v1"): _REJECT_PAID_BEDROCK,
    ("bedrock", "stability.stable-image-core-v1:1"): _REJECT_PAID_BEDROCK,
    ("bedrock", "stability.stable-image-ultra-v1:1"): _REJECT_PAID_BEDROCK,
    # Z.AI — capable, paid-only.
    ("zai", "glm-image"): _REJECT_PAID_ZAI,
    ("zai", "cogview-4"): _REJECT_PAID_ZAI,
    ("zai", "cogview-4-250304"): _REJECT_PAID_ZAI,
    # OpenRouter — no free image model exists in the live catalog.
    ("openrouter", "google/gemini-2.5-flash-image"): _REJECT_OR_PAID,
    ("openrouter", "google/gemini-2.5-flash-image-preview:free"): (
        "not in the current OpenRouter catalog"),
    ("openrouter", "google/gemini-3.1-flash-image"): _REJECT_OR_PAID,
    ("openrouter", "google/gemini-3.1-flash-lite-image"): _REJECT_OR_PAID,
    ("openrouter", "google/gemini-3-pro-image"): _REJECT_OR_PAID,
    ("openrouter", "openai/gpt-5-image"): _REJECT_OR_PAID,
    ("openrouter", "openai/gpt-5-image-mini"): _REJECT_OR_PAID,
    ("openrouter", "black-forest-labs/flux-1-schnell:free"): (
        "not in the current OpenRouter catalog"),
    ("openrouter", "sourceful/riverflow-v2.5-pro:free"): (
        "not in the current OpenRouter catalog"),
    ("openrouter", "sourceful/riverflow-v2.5-pro"): _REJECT_OR_PAID,
    # Cloudflare — stale / adapter-incompatible ids that must never sneak in.
    ("cloudflare", "@cf/runwayml/stable-diffusion-v1-5-img2img"): (
        "not in the current Workers AI catalog (provider removed it)"),
    ("cloudflare", "@cf/black-forest-labs/flux-2-dev"): (
        "adapter mismatch: current input schema requires multipart/form-data; "
        "Astra's Workers AI path sends JSON"),
    ("cloudflare", "@cf/black-forest-labs/flux-2-klein-4b"): (
        "adapter mismatch: current input schema requires multipart/form-data"),
    ("cloudflare", "@cf/black-forest-labs/flux-2-klein-9b"): (
        "adapter mismatch: current input schema requires multipart/form-data"),
}


# ── lookup ─────────────────────────────────────────────────────────────────
def image_spec(provider: str, model_id: str) -> ImageSpec | None:
    """Return the documented FREE ImageSpec for (provider, model_id), or None.

    None means "no evidence this model is a free image generator" — never a
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
    """True only for a model in the verified FREE image pool."""
    return image_spec(provider, model_id) is not None


def is_free_image_model(provider: str, model_id: str) -> bool:
    """True only when image capability AND verified free status both hold."""
    spec = image_spec(provider, model_id)
    return bool(spec and spec.is_free)


def is_image_editing_model(provider: str, model_id: str) -> bool:
    spec = image_spec(provider, model_id)
    return bool(spec and spec.supports_editing)


def image_priority_index(provider: str, model_id: str) -> int:
    """Deterministic serial-fallback position for a model (lower runs first).

    Returns a large sentinel for anything not in the FREE pool, so an
    ineligible model can never rank ahead of an eligible one.
    """
    spec = image_spec(provider, model_id)
    if spec is None:
        return 10_000
    return _PRIORITY_INDEX.get(spec.model, 10_000)


def ordered_image_pool(preferred_ids=None) -> tuple:
    """The FREE pool in its deterministic serial order.

    ``preferred_ids`` (already-declared model ids, e.g. from
    IMAGE_GENERATION_PRIORITY) are moved to the front in the given order;
    everything else follows in the curated ``IMAGE_PRIORITY`` order. Only
    models already in the FREE pool are returned -- the override can reorder
    the pool but can never add a model to it.
    """
    specs = sorted(_SPECS, key=lambda s: (_PRIORITY_INDEX.get(s.model, 10_000),
                                          s.model))
    if not preferred_ids:
        return tuple(specs)
    head, chosen = [], set()
    for raw in preferred_ids:
        raw = str(raw or "").strip()
        if not raw:
            continue
        for spec in specs:
            if spec.model in chosen:
                continue
            if spec.matches(raw):
                head.append(spec)
                chosen.add(spec.model)
                break
    return tuple(head + [s for s in specs if s.model not in chosen])


def provider_supports_image_generation(provider: str) -> bool:
    """A provider is in the FREE image pool only if it has a kept model."""
    p = str(provider or "").strip().lower()
    if p not in FREE_IMAGE_PROVIDERS:
        return False
    return any(s.provider == p for s in _SPECS)


def documented_image_models(provider: str) -> tuple:
    """Every kept FREE image model id for one provider."""
    p = str(provider or "").strip().lower()
    return tuple(s.model for s in _SPECS if s.provider == p)


def image_pool() -> tuple:
    """Every (provider, model) currently in the FREE image pool."""
    return tuple((s.provider, s.model) for s in _SPECS)


def rejected_image_reason(provider: str, model_id: str) -> str:
    """Why (provider, model_id) is NOT in the free pool ('' when unknown)."""
    p = str(provider or "").strip().lower()
    if not model_id:
        return ""
    if image_spec(p, model_id) is not None:
        return ""
    if (p, model_id) in REJECTED_IMAGE_MODELS:
        return REJECTED_IMAGE_MODELS[(p, model_id)]
    low = _normalize(model_id)
    for (rp, rm), reason in REJECTED_IMAGE_MODELS.items():
        if rp == p and _normalize(rm) == low:
            return reason
    return ""


def make_image_spec(provider: str, model_id: str, protocol: str, *,
                    source: str = "live discovery",
                    capabilities=(IMAGE_GENERATION,),
                    input_modalities=("text",),
                    output_modalities=("text", "image"),
                    free_tier: str = FREE_UNKNOWN,
                    free_evidence: str = "",
                    params=("prompt",)) -> ImageSpec:
    """Build an ad-hoc spec for a model a provider's LIVE API reported as
    image-capable AND free (e.g. an OpenRouter ``:free`` discovery result).

    Used only when the provider's own API states BOTH the output modality and
    the free status — never to invent either. A spec built here with
    ``free_tier`` other than FREE_TRUE is not selectable.
    """
    return ImageSpec(provider, model_id, protocol, source,
                     capabilities=tuple(capabilities),
                     input_modalities=tuple(input_modalities),
                     output_modalities=tuple(output_modalities),
                     free_tier=free_tier, free_evidence=free_evidence,
                     params=tuple(params))
