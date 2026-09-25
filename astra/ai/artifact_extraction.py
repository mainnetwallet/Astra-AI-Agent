"""Extract generated artifacts from provider AI responses.

When a provider responds to an image/audio/document generation request,
the response may contain base64-encoded data, URLs, or structured content
that represents a generated artifact. This module detects and extracts
those artifacts so they can be stored and validated.

Pure extraction logic — never calls a provider, never bypasses the Gateway.
"""
from __future__ import annotations

import base64
import json
import re

from astra.core.artifacts import store_artifact, validate_artifact


def extract_artifacts(response_text: str, artifact_dir: str,
                      requested_output: str = "") -> list[dict]:
    """Extract artifacts from a provider's text response.

    Returns a list of artifact dicts (with id, filename, type, validated,
    url for serving). Empty list when no artifacts are detected.
    """
    artifacts = []

    img = _extract_base64_image(response_text, artifact_dir)
    if img:
        artifacts.append(img)

    audio = _extract_base64_audio(response_text, artifact_dir)
    if audio:
        artifacts.append(audio)

    code_blocks = _extract_code_artifacts(response_text, artifact_dir,
                                           requested_output)
    artifacts.extend(code_blocks)

    return artifacts


def _extract_base64_image(text: str, artifact_dir: str) -> dict | None:
    """Detect and extract a base64-encoded image from the response."""
    pattern = r'data:(image/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=\s]+)'
    match = re.search(pattern, text)
    if not match:
        return None
    mime = match.group(1)
    b64data = match.group(2).replace("\n", "").replace("\r", "").replace(" ", "")
    try:
        raw = base64.b64decode(b64data)
    except Exception:
        return None
    if len(raw) < 100:
        return None
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
           "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".png")
    filename = f"generated{ext}"
    art = store_artifact(raw, filename, "image", artifact_dir)
    ok, _ = validate_artifact(art)
    if not ok:
        return None
    return art.to_dict()


def _extract_base64_audio(text: str, artifact_dir: str) -> dict | None:
    """Detect and extract base64-encoded audio from the response."""
    pattern = r'data:(audio/(?:mpeg|mp3|wav|ogg|flac));base64,([A-Za-z0-9+/=\s]+)'
    match = re.search(pattern, text)
    if not match:
        return None
    mime = match.group(1)
    b64data = match.group(2).replace("\n", "").replace("\r", "").replace(" ", "")
    try:
        raw = base64.b64decode(b64data)
    except Exception:
        return None
    if len(raw) < 100:
        return None
    ext = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
           "audio/ogg": ".ogg", "audio/flac": ".flac"}.get(mime, ".mp3")
    filename = f"generated{ext}"
    art = store_artifact(raw, filename, "audio", artifact_dir)
    ok, _ = validate_artifact(art)
    if not ok:
        return None
    return art.to_dict()


def _extract_code_artifacts(text: str, artifact_dir: str,
                             requested_output: str) -> list[dict]:
    """Extract code blocks that represent generated files (JSON, CSV, etc.)."""
    artifacts = []
    if not requested_output:
        return artifacts

    pattern = r'```(\w+)?\n(.*?)```'
    for match in re.finditer(pattern, text, re.DOTALL):
        lang = (match.group(1) or "").lower()
        content = match.group(2).strip()
        if not content or len(content) < 10:
            continue

        if lang == "json" and requested_output in ("data", "json"):
            try:
                json.loads(content)
            except json.JSONDecodeError:
                continue
            art = store_artifact(content.encode("utf-8"), "generated.json",
                                  "data", artifact_dir)
            ok, _ = validate_artifact(art)
            if ok:
                artifacts.append(art.to_dict())

        elif lang == "csv" and requested_output in ("data", "csv"):
            art = store_artifact(content.encode("utf-8"), "generated.csv",
                                  "data", artifact_dir)
            if art.storage_path:
                art.validated = True
                artifacts.append(art.to_dict())

    return artifacts


def detect_output_type(message_text: str) -> str:
    """Detect what kind of output the user is requesting."""
    low = message_text.lower()
    if re.search(r"\b(generate|create|draw|make)\s+(an?\s+)?(image|picture|photo|illustration)", low):
        return "image"
    if re.search(r"\b(generate|create|make)\s+(an?\s+)?(audio|sound|music|speech)", low):
        return "audio"
    if re.search(r"\b(generate|create|make)\s+(an?\s+)?(video|animation|clip)", low):
        return "video"
    if re.search(r"\b(generate|create|export)\s+(an?\s+)?(pdf|document|report)", low):
        return "document"
    if re.search(r"\b(generate|create|export)\s+(an?\s+)?(xlsx?|spreadsheet)", low):
        return "spreadsheet"
    if re.search(r"\b(generate|create|export)\s+(an?\s+)?(pptx?|presentation|slides)", low):
        return "presentation"
    if re.search(r"\b(generate|create|export)\s+(an?\s+)?(json|csv|data)", low):
        return "data"
    return ""
