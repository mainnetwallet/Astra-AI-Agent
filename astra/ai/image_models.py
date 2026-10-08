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

Audited 2026-09-25, RE-AUDITED 2026-09-26 against the providers' CURRENT
official sources:

USER-REQUESTED OVERRIDE (2026-09-26, FINALIZED): ``gemini-2.5-flash-image``
is the ONE remaining force-included candidate, kept as the dedicated Gemini
provider model. Its exact id is preserved and its spec is marked
free_tier=TRUE as a recorded, deliberate exception -- NOT a claim that the
provider prices it at zero. The three OpenRouter ``:free`` ids that used to
be force-included were REMOVED on 2026-09-26 after live re-verification of
``https://openrouter.ai/api/v1/images/models`` proved none of them exists as
a free image model (see the OpenRouter section and REJECTED_IMAGE_MODELS),
so OpenRouter's active FREE pool is now empty. A genuine upstream failure is
reported and the serial fallback continues to the next FREE model.

* Cloudflare Workers AI -- https://developers.cloudflare.com/workers-ai/models/
  The free allocation is documented on
  https://developers.cloudflare.com/workers-ai/platform/pricing/ . As of the
  2026-09-26 re-audit, Cloudflare replaced the old blanket "10,000
  Neurons/day" figure with a per-task-type table:
  "Images -- Sum of 250 steps, up to 1024x1024 resolution" (LLM text and
  embeddings get their own separate 10,000-tokens/day allocations; this
  does not shrink the image allocation, it just states it directly in
  steps instead of via the Neurons conversion). flux-1-schnell's low
  default step count means this covers dozens of free images/day; an
  SDXL-class model at ~20-50 steps covers roughly 5-12/day. Image models
  are not on any paid-only exception list, so text-to-image stays
  free-tier usable. Kept models are the ones whose published input schema
  accepts a JSON ``{"prompt": ...}`` body (schema-input.json), i.e. the
  ones Astra's Workers AI adapter can actually invoke. Re-verified LIVE on
  2026-09-26 against https://developers.cloudflare.com/workers-ai/models/:
  all seven kept ids are still listed in Cloudflare's own catalog, and each
  one's schema-input.json resolves and accepts a JSON ``prompt`` body (the
  flux-2-* successors publish a multipart/form-data schema and stay out).

* Google Gemini -- https://ai.google.dev/gemini-api/docs/pricing
  Every Gemini image model (``gemini-3.1-flash-image``,
  ``gemini-3.1-flash-lite-image``, ``gemini-3-pro-image``,
  ``gemini-2.5-flash-image``) lists **Free Tier = Not available** for image
  output, and ``gemini-2.5-flash-image`` is additionally deprecated
  (shutdown 2026-10-02). Under the strict rule they are paid-only.
  EXCEPTION: ``gemini-2.5-flash-image`` is force-included below on explicit
  user request; the 3.x image models stay out.
  (Gemini chat/vision stays fully intact.)

* Amazon Bedrock -- Nova Canvas / Titan Image / Stability via ``InvokeModel``
  are billed per image with no free tier -> removed from the FREE pool.

* Z.AI -- https://docs.z.ai/guides/overview/pricing prices GLM-Image at
  $0.015/image and CogView-4 at $0.01/image (no free tier) -> removed.

* OpenRouter -- https://openrouter.ai/api/v1/images/models
  Re-verified LIVE on 2026-09-26 (GET /api/v1/images/models returned 55
  image models): the catalog contains ZERO ``:free`` image models, and a
  scan of the main ``/api/v1/models`` catalog found 17 ``:free`` models of
  which NONE declares an image output modality. The active FREE pool is
  therefore EMPTY. The three previously force-included ``:free`` ids are now
  all recorded as rejected (see REJECTED_IMAGE_MODELS): two do not exist in
  the live catalog at all, and the third's ``:free`` variant does not exist
  while its paid base id (``sourceful/riverflow-v2.5-pro``) is priced at
  $0.13/image. No paid model was substituted. OpenRouter's live-discovery
  path is retained, so if OpenRouter ever publishes a genuinely free
  image-output model it is picked up automatically; nothing is invented.

* Groq, Cerebras, SambaNova, Cohere, Mistral -- no image-generation API path
  in this repository at all -> never image-capable.

2026-09-26 re-audit -- additional candidates investigated per the brief.
None cleared the strict FREE bar (evidence recorded on each REJECTED_IMAGE_MODELS
entry below); every one is either paid-only or offers a one-time/negligible
promotional allowance rather than a recurring, genuinely usable free tier:

* Together AI -- https://www.together.ai/models/flux-1-schnell (the "FLUX.1
  [schnell] Free" listing) states in its own UI "This model is not available
  on Together's Serverless API" / "Launching soon -- We'll email you when the
  endpoint goes live." The live serverless image-pricing table
  (https://www.together.ai/pricing) lists ONLY paid image models (SD XL
  $0.0019/mp, FLUX.2 family, Qwen Image, etc.) -- zero free rows. Rejected as
  not-yet-live, not "free but no adapter".

* Hugging Face Inference Providers --
  https://huggingface.co/docs/inference-providers/pricing : Free-tier users
  get **$0.10/month** in credits, spendable on Inference Providers, and that
  is the entirety of the free allocation (not a per-call free tier). The one
  provider that used to be free-of-charge, "hf-inference", "focuses mostly on
  CPU inference (e.g. embedding, text-ranking, text-classification, ...) --
  not image generation" per the same docs; every text-to-image call
  (https://huggingface.co/docs/inference-providers/tasks/text-to-image) is
  routed to and billed by a paid partner (fal, Together, Replicate, Nscale,
  Novita, WaveSpeedAI, ...) against that $0.10, which a single image can
  exhaust. Rejected as not a genuinely usable free image-generation path.

* Fal AI -- https://fal.ai/docs/documentation/model-apis/pricing : prepaid
  credit billing per image/megapixel, "no standing free tier" (promotional
  signup credits only, time-limited). Rejected: paid-only.

* Replicate -- billed per second of compute / per image with no published
  standing free tier for image models. Rejected: paid-only.

* Fireworks AI -- https://fireworks.ai/blog/flux-launch : FLUX.1 [schnell]
  and [dev] are billed per diffusion step ($0.0014 / $0.014 per default
  image); only a one-time signup credit exists, not a recurring free tier.
  Rejected: paid-only.

* Nscale -- OpenAI-compatible image-generation API
  (https://docs.nscale.com/docs/use-cases/image-generation) is priced
  per-request with no documented free allocation. Rejected: paid-only.

* Novita AI -- https://novita.ai/get-started : "We give each logged-in user
  10 free chances to generate a picture" -- a one-time, non-renewing signup
  allowance, then strictly pay-as-you-go (from $0.0015/image). Rejected: not
  a recurring free tier, so not selectable for ongoing FREE-pool service.

* WaveSpeedAI -- pay-as-you-go from ~$0.005/image with a one-time $1 signup
  credit; no recurring free tier. Rejected: paid-only.

Image requests are served by a SIMPLE SERIAL FALLBACK over this FREE pool:
the eligible models are tried one after another in ONE deterministic GLOBAL
priority order (``IMAGE_PRIORITY`` / ``image_priority_index`` /
``ordered_image_pool``, overridable with ``IMAGE_GENERATION_PRIORITY`` or
``GW_IMAGE_GENERATION_PRIORITY``). The list is model-by-model and GLOBAL --
providers are never tried as groups (no "all Cloudflare, then all Gemini,
then all OpenRouter"); a failure moves straight to the next model in the
list, whatever provider serves it. The ACTUAL generation request is the only
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
IMAGE_INPAINTING = "image_inpainting"
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
# Protocol ownership is explicit: a model is only ever dispatched through the
# protocol its provider actually documents.
#   * openrouter -> ``POST /api/v1/images`` (dedicated Images API)
#   * cloudflare -> Workers AI ``/ai/run/<model>``
#   * gemini     -> native ``models/<id>:generateContent``
#   * zai        -> OpenAI-style ``POST /images/generations``
#   * bedrock    -> ``InvokeModel``
#: OpenAI-style ``POST /images/generations`` (Z.AI GLM-Image/CogView). NOT
#: OpenRouter, which uses its own dedicated endpoint below.
PROTOCOL_OPENAI_IMAGES = "openai_images_generations"
#: OpenRouter dedicated Images API: ``POST /api/v1/images``.
PROTOCOL_OPENROUTER_IMAGES = "openrouter_images"
#: Gemini native image output: ``models/<id>:generateContent``.
PROTOCOL_GEMINI_CONTENT = "gemini_generate_content_image"
#: Cloudflare Workers AI image endpoint: ``/ai/run/<model>``.
PROTOCOL_CLOUDFLARE_RUN = "cloudflare_workers_ai_run"
#: Bedrock ``InvokeModel`` (Nova Canvas / Titan Image / Stability).
PROTOCOL_BEDROCK_INVOKE = "bedrock_invoke_model"

SUPPORTED_PROTOCOLS = frozenset({
    PROTOCOL_OPENAI_IMAGES, PROTOCOL_OPENROUTER_IMAGES,
    PROTOCOL_GEMINI_CONTENT,
    PROTOCOL_CLOUDFLARE_RUN, PROTOCOL_BEDROCK_INVOKE,
})

# Providers with a verified FREE image-generation tier AND a real adapter
# path in this repository. This is the FREE image pool; nothing else may be
# selected for image generation.
FREE_IMAGE_PROVIDERS = frozenset({"cloudflare", "gemini", "openrouter"})

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

# The Gateway's own connection classes (astra/ai/gateway.py) read these
# EXACT SAME canonical env vars via each connection's own ``image_models_env``
# attribute (e.g. ``AstraGatewayCloudflare.image_models_env =
# "CLOUDFLARE_IMAGE_MODELS"``) -- there is no separate ``GW_*_IMAGE_MODELS``
# variable any more. The two systems intentionally share one variable per
# provider rather than each defining its own, so a deployment configures the
# image-model list once. A provider-name -> env-var mapping is not
# duplicated here for the Gateway since each connection class already is the
# single source of truth for its own ``image_models_env``.
#
#: CANONICAL env var for the deterministic, comma-separated serial-fallback
#: priority order used by the Gateway's ImageRouter (the in-repo
#: ``IMAGE_PRIORITY`` tuple above is the default when unset).
GATEWAY_IMAGE_PRIORITY_ENV = "GW_IMAGE_GENERATION_PRIORITY"
#: Legacy fallback alias for ``GATEWAY_IMAGE_PRIORITY_ENV``, consulted only
#: when the canonical ``GW_*`` var is unset/empty (see
#: ``AstraAIGateway.image_targets``). Predates the ``GW_`` prefix convention;
#: kept for backward compatibility rather than removed, since a deployment
#: may already set it.
IMAGE_PRIORITY_ENV = "IMAGE_GENERATION_PRIORITY"


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
            "\u2014 free allocation table (re-confirmed live 2026-09-26): "
            "Images = sum of 250 free steps/day (up to 1024x1024); this "
            "superseded the old blanket 10,000 Neurons/day description but "
            "Images still bill in Neurons and are not on any paid-only "
            "exception list")
_CF_SCHEMA = ("https://developers.cloudflare.com/workers-ai/models/{}/"
              "schema-input.json")

#: Evidence recorded for the user-requested (force-added) FREE candidates.
_USER_REQ_EVIDENCE = (
    "user-requested FREE candidate (2026-09-26): force-included in the FREE "
    "image pool on explicit request; the real generation call decides runtime "
    "availability")

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
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING, IMAGE_INPAINTING),
              input_modalities=("text", "image"),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height", "image_b64", "mask", "strength")),
    ImageSpec("cloudflare", "@cf/bytedance/stable-diffusion-xl-lightning",
              PROTOCOL_CLOUDFLARE_RUN,
              _CF_SCHEMA.format("stable-diffusion-xl-lightning"),
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING, IMAGE_INPAINTING),
              input_modalities=("text", "image"),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height", "image_b64", "mask", "strength")),
    ImageSpec("cloudflare", "@cf/lykon/dreamshaper-8-lcm",
              PROTOCOL_CLOUDFLARE_RUN, _CF_SCHEMA.format("dreamshaper-8-lcm"),
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING, IMAGE_INPAINTING),
              input_modalities=("text", "image"),
              sizes=("1024x1024", "768x768", "512x512"),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height", "image_b64", "mask", "strength")),
    ImageSpec("cloudflare", "@cf/runwayml/stable-diffusion-v1-5-inpainting",
              PROTOCOL_CLOUDFLARE_RUN,
              _CF_SCHEMA.format("stable-diffusion-v1-5-inpainting"),
              # Text-to-Image per Cloudflare's own task label; the inpainting
              # mode needs a source image+mask, which this adapter does not
              # send, so NO image_editing capability is advertised.
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING, IMAGE_INPAINTING),
              input_modalities=("text", "image"),
              sizes=("512x512",),
              free_tier=FREE_TRUE, free_evidence=_CF_FREE,
              params=("prompt", "width", "height", "image_b64", "mask", "strength")),
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
    # ── Google Gemini (native models/<id>:generateContent) ────────────────
    # Text -> image and source-image editing both use the native content API.
    # The adapter forwards an uploaded/generated image as inlineData.
    ImageSpec("gemini", "gemini-2.5-flash-image",
              PROTOCOL_GEMINI_CONTENT,
              "user request 2026-09-26 (force-added FREE candidate)",
              capabilities=(IMAGE_GENERATION, IMAGE_EDITING),
              input_modalities=("text", "image"),
              output_modalities=("text", "image"),
              sizes=("1024x1024",),
              free_tier=FREE_TRUE, free_evidence=_USER_REQ_EVIDENCE,
              params=("prompt",)),
    # ── OpenRouter Image API (POST /api/v1/images) ────────────────────────
    # No static OpenRouter entry: the live catalog (re-verified 2026-09-26)
    # lists ZERO ``:free`` image-output models, so the active FREE pool is
    # intentionally empty. A genuinely free model would still be picked up
    # at runtime by ``AstraGatewayOpenRouter._discover_image_models`` -- the
    # dynamic path the registry documents rather than a hard-coded guess.
)


# ── deterministic GLOBAL, MODEL-by-MODEL serial-fallback order ────────────
# ONE flat list: every FREE image model competes in this single priority
# order, regardless of which provider serves it. The provider is NEVER a
# grouping/batching unit -- a request tries model #1, then model #2, then
# model #3, ... in exactly this order, crossing provider boundaries as often
# as the list does (Gemini -> Cloudflare -> Cloudflare -> ...). With
# OpenRouter's active pool empty the list currently ends in Cloudflare, but
# the rule is unchanged: the order is a single global sequence, never
# grouped per provider ("try every Cloudflare model, then every Gemini
# model" is explicitly NOT the behaviour).
#
# The order is a curated DEFAULT (not a health signal, not a liveness probe):
#   1.  gemini-2.5-flash-image               native Google image output
#   2.  @cf/leonardo/lucid-origin            Leonardo general model
#   3.  @cf/leonardo/phoenix-1.0             Leonardo model
#   4.  @cf/black-forest-labs/flux-1-schnell fastest/lowest-cost CF model
#   5.  @cf/stabilityai/stable-diffusion-xl-base-1.0
#   6.  @cf/bytedance/stable-diffusion-xl-lightning
#   7.  @cf/lykon/dreamshaper-8-lcm
#   8.  @cf/runwayml/stable-diffusion-v1-5-inpainting
#   (No OpenRouter entry: its live catalog has zero FREE image models, so
#   the active pool contributes nothing until a genuinely free one appears
#   through live discovery.)
#
# Override the whole ordering with IMAGE_GENERATION_PRIORITY (or
# GW_IMAGE_GENERATION_PRIORITY for the AI Gateway). The override is ALSO a
# single global model list; it may reorder/re-select the already-eligible
# FREE image models but can never add a text, vision-only or paid model.
IMAGE_PRIORITY = (
    "gemini-2.5-flash-image",
    "@cf/leonardo/lucid-origin",
    "@cf/leonardo/phoenix-1.0",
    "@cf/black-forest-labs/flux-1-schnell",
    "@cf/stabilityai/stable-diffusion-xl-base-1.0",
    "@cf/bytedance/stable-diffusion-xl-lightning",
    "@cf/lykon/dreamshaper-8-lcm",
    "@cf/runwayml/stable-diffusion-v1-5-inpainting",
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
_REJECT_OR_PAID = ("no free model: OpenRouter's image catalog lists this "
                   "model as paid (its ``:free`` variants are text-output "
                   "only; live re-verification on 2026-09-26 found zero free "
                   "image-output models, so nothing is substituted)")
_REJECT_OR_ABSENT = (
    "not in OpenRouter's live image catalog: GET "
    "https://openrouter.ai/api/v1/images/models (re-verified 2026-09-26, 55 "
    "image models, zero ``:free``) does not list this id at all -- it exists "
    "neither as a free nor a paid image model")
_REJECT_OR_NO_FREE_VARIANT = (
    "no such free variant: OpenRouter lists this id only as a PAID image "
    "model (pricing $0.13/image for the 1K tier, re-verified 2026-09-26); no "
    "``:free`` image model exists in its live catalog, and a paid model is "
    "never substituted into the FREE pool")
_REJECT_TOGETHER_NOT_LIVE = (
    "not currently free: Together's own model page marks FLUX.1 [schnell] "
    "Free \"not available on Together's Serverless API\" / \"Launching "
    "soon\"; the live serverless image pricing table has zero free rows")
_REJECT_TOGETHER_PAID = "paid: listed on Together's live serverless image pricing table"
_REJECT_HF_CREDITS = (
    "not a genuine free tier: Free-tier HF users get $0.10/month total "
    "credit for Inference Providers, and text-to-image is routed to a paid "
    "partner provider billed against that $0.10 -- a single image can "
    "exhaust it; hf-inference itself no longer serves image generation")
_REJECT_FAL_PAID = "paid-only: prepaid per-image/megapixel credits, no standing free tier (signup promo credits only)"
_REJECT_REPLICATE_PAID = "paid-only: billed per second/image, no published standing free tier"
_REJECT_FIREWORKS_PAID = "paid-only: billed per diffusion step ($0.0014-$0.014/image); only a one-time signup credit"
_REJECT_NSCALE_PAID = "paid-only OpenAI-compatible image API; no documented free allocation"
_REJECT_NOVITA_ONE_TIME = (
    "not a recurring free tier: 10 free image \"chances\" once per signup, "
    "then strictly pay-as-you-go from $0.0015/image")
_REJECT_WAVESPEED_PAID = "paid-only, pay-as-you-go from ~$0.005/image; only a one-time $1 signup credit"

REJECTED_IMAGE_MODELS: dict = {
    # Gemini — capable, but paid-only (and 2.5-flash-image deprecated).
    ("gemini", "gemini-3.1-flash-image"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3.1-flash-image-preview"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3.1-flash-lite-image"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3-pro-image"): _REJECT_PAID_GEMINI,
    ("gemini", "gemini-3-pro-image-preview"): _REJECT_PAID_GEMINI,
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
    # OpenRouter — the live catalog (re-verified 2026-09-26 via GET
    # /api/v1/images/models) has ZERO free image-output models. The three
    # previously force-included ``:free`` ids were removed because none of
    # them exists as a free image model; every OpenRouter image model stays
    # paid/out, and the active FREE pool is empty (never a paid substitute).
    ("openrouter", "google/gemini-2.5-flash-image-preview:free"): _REJECT_OR_ABSENT,
    ("openrouter", "black-forest-labs/flux-1-schnell:free"): _REJECT_OR_ABSENT,
    ("openrouter", "sourceful/riverflow-v2.5-pro:free"): _REJECT_OR_NO_FREE_VARIANT,
    ("openrouter", "google/gemini-2.5-flash-image"): _REJECT_OR_PAID,
    ("openrouter", "google/gemini-3.1-flash-image"): _REJECT_OR_PAID,
    ("openrouter", "google/gemini-3.1-flash-lite-image"): _REJECT_OR_PAID,
    ("openrouter", "google/gemini-3-pro-image"): _REJECT_OR_PAID,
    ("openrouter", "openai/gpt-5-image"): _REJECT_OR_PAID,
    ("openrouter", "openai/gpt-5-image-mini"): _REJECT_OR_PAID,
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

    # ── 2026-09-26 re-audit: additional candidates investigated, none free ──
    ("together", "black-forest-labs/FLUX.1-schnell-Free"): _REJECT_TOGETHER_NOT_LIVE,
    ("together", "black-forest-labs/FLUX.1-schnell"): _REJECT_TOGETHER_PAID,
    ("together", "black-forest-labs/FLUX.1.1-pro"): _REJECT_TOGETHER_PAID,
    ("together", "stabilityai/stable-diffusion-xl-base-1.0"): _REJECT_TOGETHER_PAID,
    ("huggingface", "black-forest-labs/FLUX.1-schnell"): _REJECT_HF_CREDITS,
    ("huggingface", "black-forest-labs/FLUX.1-dev"): _REJECT_HF_CREDITS,
    ("huggingface", "Qwen/Qwen-Image"): _REJECT_HF_CREDITS,
    ("fal", "fal-ai/flux/schnell"): _REJECT_FAL_PAID,
    ("fal", "fal-ai/flux/dev"): _REJECT_FAL_PAID,
    ("replicate", "black-forest-labs/flux-schnell"): _REJECT_REPLICATE_PAID,
    ("replicate", "stability-ai/sdxl"): _REJECT_REPLICATE_PAID,
    ("fireworks", "accounts/fireworks/models/flux-1-schnell-fp8"): _REJECT_FIREWORKS_PAID,
    ("fireworks", "accounts/fireworks/models/flux-1-dev-fp8"): _REJECT_FIREWORKS_PAID,
    ("nscale", "black-forest-labs/FLUX.1-schnell"): _REJECT_NSCALE_PAID,
    ("novita", "flux-1-schnell"): _REJECT_NOVITA_ONE_TIME,
    ("wavespeed", "wavespeed-ai/flux-schnell"): _REJECT_WAVESPEED_PAID,
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
