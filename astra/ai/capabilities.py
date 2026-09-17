"""Multimodal capability system for Astra AI.

Maps (provider, model) pairs to their actual input/output capabilities so
the router can filter incompatible candidates BEFORE health/latency/preference
ranking. A model that cannot process images must never receive an image
attachment; a model that cannot generate audio must never be selected for
an audio-generation request.

This module is a pure data/filtering layer — it never executes AI calls,
never touches credentials, and never bypasses the Gateway.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astra.ai.models import Model

# ── capability constants ───────────────────────────────────────────────────

INPUT_TEXT = "text_input"
INPUT_IMAGE = "image_input"
INPUT_AUDIO = "audio_input"
INPUT_VIDEO = "video_input"
INPUT_DOCUMENT = "document_input"

OUTPUT_TEXT = "text_generation"
OUTPUT_IMAGE = "image_generation"
OUTPUT_AUDIO = "audio_generation"
OUTPUT_VIDEO = "video_generation"
OUTPUT_DOCUMENT = "document_generation"

ALL_INPUT_CAPS = (INPUT_TEXT, INPUT_IMAGE, INPUT_AUDIO, INPUT_VIDEO, INPUT_DOCUMENT)
ALL_OUTPUT_CAPS = (OUTPUT_TEXT, OUTPUT_IMAGE, OUTPUT_AUDIO, OUTPUT_VIDEO, OUTPUT_DOCUMENT)

# ── file type families (aligned with astra.core.attachments) ───────────────

FILE_FAMILY_CAPS = {
    "document": INPUT_DOCUMENT,
    "text": INPUT_TEXT,
    "data": INPUT_TEXT,
    "spreadsheet": INPUT_DOCUMENT,
    "presentation": INPUT_DOCUMENT,
    "structured": INPUT_TEXT,
    "image": INPUT_IMAGE,
    "audio": INPUT_AUDIO,
    "video": INPUT_VIDEO,
    "archive": INPUT_TEXT,
}

# ── known model capabilities ───────────────────────────────────────────────
# Seeded from the _FAMILIES table in astra.ai.models. Only capabilities that
# are genuinely supported by the exact provider+model are listed. When in
# doubt, conservative: text_input + text_generation only.

_BASE = frozenset({INPUT_TEXT, OUTPUT_TEXT})

KNOWN_CAPABILITIES: dict[str, frozenset[str]] = {
    "claude": _BASE | {INPUT_IMAGE, INPUT_DOCUMENT},
    "gemini": _BASE | {INPUT_IMAGE, INPUT_DOCUMENT},
    "openai": _BASE | {INPUT_IMAGE, INPUT_DOCUMENT},
    "gpt": _BASE | {INPUT_IMAGE},
    "command": _BASE | {INPUT_IMAGE},
    "pixtral": _BASE | {INPUT_IMAGE},
    "deepseek": _BASE,
    "llama": _BASE,
    "qwen": _BASE,
    "mistral": _BASE,
    "codestral": _BASE,
    "devstral": _BASE,
    "ministral": _BASE,
    "nova": _BASE,
    "nova-lite": _BASE,
    "gemma": _BASE,
    "aya": _BASE,
    "glm": _BASE,
    "minimax": _BASE,
    "kimi": _BASE,
    "kling": _BASE,
    "nemotron": _BASE,
    "grok": _BASE,
    "mercury": _BASE,
    "laguna": _BASE,
    "dots": _BASE,
    "ling": _BASE,
    "lfm": _BASE,
    "sea-lion": _BASE,
    "granite": _BASE,
}

# Models that support image generation (exact model_id substrings)
# Only Bedrock families have a real adapter with generate_image()
_IMAGE_GEN_MODELS = (
    "stable-diffusion",
    "stability",
    "titan-image",
)

# Models that support audio input/transcription
# Only Gemini families have a real adapter that passes audio to the API
_AUDIO_INPUT_MODELS = (
    "gemini-2.0",
    "gemini-1.5",
)

# Models that support audio generation
# No adapter currently has a real TTS execution path
_AUDIO_GEN_MODELS = (
)

# Models that support video input/analysis
_VIDEO_INPUT_MODELS = (
    "gemini-2.0",
    "gemini-1.5",
)


def _family_of(model_id: str) -> str:
    """Pick the capability family that best matches a model id string."""
    low = model_id.lower()
    for key in sorted(KNOWN_CAPABILITIES, key=len, reverse=True):
        if key in low:
            return key
    return ""


def capabilities_for(provider: str, model_id: str) -> frozenset[str]:
    """Derive the multimodal capability set for a (provider, model_id) pair."""
    fam = _family_of(model_id)
    caps = set(KNOWN_CAPABILITIES.get(fam, _BASE))
    low = model_id.lower()

    if any(m in low for m in _IMAGE_GEN_MODELS):
        caps.add(OUTPUT_IMAGE)
    if any(m in low for m in _AUDIO_INPUT_MODELS):
        caps.add(INPUT_AUDIO)
    if any(m in low for m in _AUDIO_GEN_MODELS):
        caps.add(OUTPUT_AUDIO)
    if any(m in low for m in _VIDEO_INPUT_MODELS):
        caps.add(INPUT_VIDEO)

    if re.search(r"(?:^|[-_/])vl(?:[-_]|$)", low) or "vision" in low:
        caps.add(INPUT_IMAGE)

    return frozenset(caps)


# ── detection from request content ─────────────────────────────────────────

def detect_required_input_capabilities(attachments: list) -> list[str]:
    """Given a list of Attachment objects (or dicts with 'family'/'detected_type'),
    return the input capabilities the request requires."""
    caps = set()
    for att in (attachments or []):
        family = att.family if hasattr(att, "family") else att.get("family", "")
        cap = FILE_FAMILY_CAPS.get(family)
        if cap:
            caps.add(cap)
    if not caps:
        caps.add(INPUT_TEXT)
    return sorted(caps)


_OUTPUT_PATTERNS = {
    OUTPUT_IMAGE: re.compile(
        r"\b(generate|create|draw|make|design)\s+(an?\s+)?(image|picture|photo|illustration|diagram|logo|icon|art)\b",
        re.IGNORECASE,
    ),
    OUTPUT_AUDIO: re.compile(
        r"\b(generate|create|make|produce)\s+(an?\s+)?(audio|sound|music|song|speech|voice|tts|narrat)\b",
        re.IGNORECASE,
    ),
    OUTPUT_VIDEO: re.compile(
        r"\b(generate|create|make|produce)\s+(an?\s+)?(video|animation|clip|movie)\b",
        re.IGNORECASE,
    ),
    OUTPUT_DOCUMENT: re.compile(
        r"\b(generate|create|make|produce|export)\s+(an?\s+)?(pdf|docx?|xlsx?|pptx?|spreadsheet|presentation|document|report)\b",
        re.IGNORECASE,
    ),
}


def detect_required_output_capabilities(message_text: str) -> list[str]:
    """Detect output capabilities required by the user's message text."""
    caps = set()
    text = str(message_text or "")
    for cap, pattern in _OUTPUT_PATTERNS.items():
        if pattern.search(text):
            caps.add(cap)
    if not caps:
        caps.add(OUTPUT_TEXT)
    return sorted(caps)


# ── candidate filtering ────────────────────────────────────────────────────

def _model_caps(model: "Model") -> frozenset[str]:
    """Resolve the multimodal capability set for a Model object."""
    base = capabilities_for(model.provider, model.model_id)
    caps = set(base)
    if "vision" in (model.capabilities or []):
        caps.add(INPUT_IMAGE)
    if "image" in (model.input_modalities or []):
        caps.add(INPUT_IMAGE)
    if "audio" in (model.input_modalities or []):
        caps.add(INPUT_AUDIO)
    if "video" in (model.input_modalities or []):
        caps.add(INPUT_VIDEO)
    if "image" in (model.output_modalities or []):
        caps.add(OUTPUT_IMAGE)
    if "audio" in (model.output_modalities or []):
        caps.add(OUTPUT_AUDIO)
    if "video" in (model.output_modalities or []):
        caps.add(OUTPUT_VIDEO)
    return frozenset(caps)


def check_model_compatibility(
    model: "Model",
    required_input_caps: list[str],
    required_output_caps: list[str],
) -> tuple[bool, str]:
    """Check if a model supports all required capabilities.
    Returns (compatible, reason). reason is empty when compatible."""
    caps = _model_caps(model)
    missing = []
    for cap in required_input_caps:
        if cap not in caps:
            missing.append(cap)
    for cap in required_output_caps:
        if cap not in caps:
            missing.append(cap)
    if missing:
        return False, (
            f"{model.provider}/{model.model_id} lacks: {', '.join(missing)}"
        )
    return True, ""


def filter_candidates_by_capabilities(
    candidates: list,
    required_input_caps: list[str],
    required_output_caps: list[str],
) -> list:
    """Filter (adapter, model) candidate pairs, keeping only those whose model
    supports every required input and output capability. Order is preserved."""
    required = set(required_input_caps) | set(required_output_caps)
    if not required or required == {INPUT_TEXT, OUTPUT_TEXT}:
        return candidates
    out = []
    for candidate in candidates:
        if isinstance(candidate, tuple) and len(candidate) >= 2:
            model = candidate[1]
        else:
            continue
        caps = _model_caps(model)
        if required.issubset(caps):
            out.append(candidate)
    return out
