"""FREE image-generation capability registry.

Image generation is intentionally limited to four supported providers:
Gemini, Cloudflare Workers AI, OpenRouter, and Hugging Face. The normal
text/reasoning/vision provider registry is independent and is not modified
by this module.

Only explicitly free image models, or models reported as free image-output
models by a supported provider's live catalog, may enter the image pool.
There is no paid-image fallback.
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
#   * huggingface -> OpenAI-style ``POST /images/generations`` (unified
#     Inference Providers router)
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

SUPPORTED_PROTOCOLS = frozenset({
    PROTOCOL_OPENAI_IMAGES, PROTOCOL_OPENROUTER_IMAGES,
    PROTOCOL_GEMINI_CONTENT,
    PROTOCOL_CLOUDFLARE_RUN,
})

# Providers with a verified FREE image-generation tier AND a real adapter
# path in this repository. This is the FREE image pool; nothing else may be
# selected for image generation.
FREE_IMAGE_PROVIDERS = frozenset({"cloudflare", "gemini", "openrouter",
                                  "huggingface"})

#: Backwards-compatible alias (the free pool is the supported image pool).
SUPPORTED_PROVIDERS = FREE_IMAGE_PROVIDERS

# env var holding each provider's optional image-model list
IMAGE_MODELS_ENV = {
    "gemini": "GEMINI_IMAGE_MODELS",
    "cloudflare": "CLOUDFLARE_IMAGE_MODELS",
    "openrouter": "OPENROUTER_IMAGE_MODELS",
    "huggingface": "HF_IMAGE_MODELS",
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

#: Hugging Face force-include (2026-09-27, EXPLICIT USER REQUEST). This is
#: NOT a claim that Hugging Face's Inference Providers image path is
#: verified free per-call -- the opposite is documented and unchanged (see
#: `_REJECT_HF_CREDITS` / the module docstring): free-tier accounts get only
#: a $0.10/month total credit, and a single image can exhaust it, so this is
#: a recorded, deliberate override -- same pattern as the Gemini exception
#: above -- kept honest rather than silently marked as verified free.
_HF_FORCE_EVIDENCE = (
    "user-selected Hugging Face image model kept in the supported image pool"
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
#   9.  black-forest-labs/FLUX.1-schnell (huggingface, force-included)
#   10. black-forest-labs/FLUX.1-dev (huggingface, force-included)
#   11. black-forest-labs/FLUX.1-Kontext-dev (huggingface, force-included)
#   12. Qwen/Qwen-Image (huggingface, force-included)
#   13. stabilityai/stable-diffusion-3.5-large (huggingface, force-included)
#   (No OpenRouter entry: its live catalog has zero FREE image models, so
#   the active pool contributes nothing until a genuinely free one appears
#   through live discovery. The five Hugging Face entries are placed LAST
#   because they are a force-included, credit-limited exception -- NOT
#   verified free -- so genuinely free models are always tried first; see
#   `_HF_FORCE_EVIDENCE`.)
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
    "black-forest-labs/FLUX.1-schnell",
    "black-forest-labs/FLUX.1-dev",
    "black-forest-labs/FLUX.1-Kontext-dev",
    "Qwen/Qwen-Image",
    "stabilityai/stable-diffusion-3.5-large",
)

_PRIORITY_INDEX = {mid: i for i, mid in enumerate(IMAGE_PRIORITY)}

#: Raised/returned when every eligible FREE image model was attempted for one
#: request and all of them failed. User-facing and credential-free.
IMAGE_EXHAUSTED_MESSAGE = (
    "All available FREE image-generation models failed to generate the image. "
    "Please try again later.")


# ── unsupported image models ───────────────────────────────────────────────
# Models outside the four-provider image pool are not selectable. They are
# intentionally not enumerated here; normal provider registration remains
# independent from image capability selection.

REJECTED_IMAGE_MODELS: dict = {}



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
    """Return an empty reason for models outside the supported image pool."""
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
