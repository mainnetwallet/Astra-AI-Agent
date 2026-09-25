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

# Extensions worth pulling back out of the runtime automatically when a
# tool call's result mentions a path under /workspace ending in one of
# these. Kept to genuinely binary/non-text output — a provider can already
# read/quote text files (.txt, .md, .py, ...) straight out of
# runtime_command's own stdout, so there is no "invisible to the user"
# problem for those.
_RUNTIME_ARTIFACT_EXT: dict[str, str] = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio",
    ".ogg": "audio",
    ".mp4": "video", ".webm": "video", ".mov": "video",
    ".pdf": "document",
    ".xlsx": "spreadsheet",
    ".pptx": "presentation",
}

_RUNTIME_PATH_RE = re.compile(
    r"""(/workspace/[^\s"'\\]+?\.(?:png|jpe?g|gif|webp|mp3|wav|m4a|flac|
        ogg|mp4|webm|mov|pdf|xlsx|pptx))""",
    re.IGNORECASE | re.VERBOSE)

# Cap how many files one turn will auto-download, so a chatty tool loop
# with many command results can't turn one reply into dozens of downloads.
_MAX_RUNTIME_ARTIFACTS = 5


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


def find_runtime_output_paths(entries: list[dict]) -> list[str]:
    """Scan agent/tool execution history entries for guest paths under
    /workspace that look like generated binary output (image, audio,
    video, document, ...), in the order first seen.

    `entries` is whatever `AgentExecutionHistory.entries(scope)` returns —
    each entry's `summary` is a JSON-ish dump of that tool call's result
    (e.g. a `runtime_command` result echoes the command and any path it
    wrote to), so the path just needs to be pulled back out of that text.
    A path is only a candidate here; whether the file actually exists is
    decided later by trying to download it.
    """
    seen: list[str] = []
    for entry in entries or []:
        text = entry.get("summary") or ""
        if not text:
            continue
        for match in _RUNTIME_PATH_RE.finditer(text):
            path = match.group(1)
            if path not in seen:
                seen.append(path)
            if len(seen) >= _MAX_RUNTIME_ARTIFACTS:
                return seen
    return seen


def extract_runtime_artifacts(entries: list[dict], download_fn,
                              artifact_dir: str) -> list[dict]:
    """Pull binary files a tool call created inside the isolated runtime
    back out and store them as artifacts, so they can be attached to the
    chat reply instead of only being described in text.

    `download_fn(path) -> dict` must behave like
    `AgentRuntime.download_file`: return `{"content_base64": ..., "size":
    ...}` for a real file, or raise/return falsy for one that doesn't
    exist (e.g. because the model only *talked about* creating a file, or
    a later step deleted it). Any error for one path is skipped rather
    than failing the whole turn — a real reply the user can read is always
    better than losing it over one bad path.
    """
    artifacts: list[dict] = []
    if download_fn is None:
        return artifacts
    for path in find_runtime_output_paths(entries):
        ext = ""
        for candidate in _RUNTIME_ARTIFACT_EXT:
            if path.lower().endswith(candidate):
                ext = candidate
                break
        if not ext:
            continue
        try:
            result = download_fn(path)
        except Exception:
            continue
        if not result:
            continue
        b64 = result.get("content_base64") if isinstance(result, dict) else None
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        if not raw:
            continue
        filename = path.rsplit("/", 1)[-1]
        art = store_artifact(raw, filename, _RUNTIME_ARTIFACT_EXT[ext],
                             artifact_dir)
        ok, _ = validate_artifact(art)
        if ok:
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
