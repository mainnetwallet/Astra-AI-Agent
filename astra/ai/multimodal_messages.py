"""Build provider-compatible multimodal message content parts.

Converts Attachment objects (or dicts) into the OpenAI-compatible content
format that vision/multimodal models expect:

  {"role": "user", "content": [
      {"type": "text", "text": "What is in this image?"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]}

This module is a pure data transformation layer — it never calls a
provider, never touches credentials, and never bypasses the Gateway.
The built messages flow through the normal RoutingRequest → AstraRouter →
Provider adapter path.
"""
from __future__ import annotations

import base64
import os


MAX_INLINE_BYTES = 20 * 1024 * 1024  # 20 MB inline limit per attachment


def build_multimodal_content(text: str, attachments: list) -> list[dict]:
    """Build a list of content parts for a multimodal user message.

    Returns a content list suitable for the "content" field of an
    OpenAI-compatible message. When no attachments have inline-able content,
    returns a plain text string instead (backward compatible).
    """
    parts = []
    if text:
        parts.append({"type": "text", "text": text})

    for att in (attachments or []):
        family = att.get("family") if isinstance(att, dict) else getattr(att, "family", "")
        storage = att.get("storage_path") if isinstance(att, dict) else getattr(att, "storage_path", "")
        mime = att.get("detected_type") or att.get("mime_type", "")
        if isinstance(att, dict):
            mime = att.get("detected_type") or att.get("mime_type", "")
        else:
            mime = getattr(att, "detected_type", "") or getattr(att, "mime_type", "")

        if family == "image":
            part = _image_part(storage, mime)
            if part:
                parts.append(part)
                continue

        if family == "audio":
            part = _audio_part(storage, mime)
            if part:
                parts.append(part)
                continue

        if family in ("document", "text", "data", "structured", "spreadsheet"):
            part = _document_part(storage, mime, att)
            if part:
                parts.append(part)
                continue

        fname = att.get("original_filename") or att.get("filename", "file") if isinstance(att, dict) \
            else getattr(att, "original_filename", "") or getattr(att, "filename", "file")
        parts.append({"type": "text", "text": f"[Attached file: {fname} ({family})]"})

    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts if parts else text


def _read_file(path: str, max_bytes: int = MAX_INLINE_BYTES) -> bytes | None:
    if not path or not os.path.isfile(path):
        return None
    size = os.path.getsize(path)
    if size > max_bytes or size == 0:
        return None
    with open(path, "rb") as f:
        return f.read()


def _image_part(storage_path: str, mime: str) -> dict | None:
    data = _read_file(storage_path)
    if data is None:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    mime = mime or "image/png"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"}
    }


def _audio_part(storage_path: str, mime: str) -> dict | None:
    data = _read_file(storage_path)
    if data is None:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    mime = mime or "audio/mpeg"
    return {
        "type": "input_audio",
        "input_audio": {"data": b64, "format": _audio_format(mime)}
    }


def _audio_format(mime: str) -> str:
    mime = mime.lower()
    if "wav" in mime:
        return "wav"
    if "mp3" in mime or "mpeg" in mime:
        return "mp3"
    if "ogg" in mime:
        return "ogg"
    if "flac" in mime:
        return "flac"
    if "opus" in mime:
        return "opus"
    return "mp3"


def _document_part(storage_path: str, mime: str, att) -> dict | None:
    """For text-based documents, include their content as text."""
    if not storage_path or not os.path.isfile(storage_path):
        return None
    fname = att.get("original_filename") or att.get("filename", "file") if isinstance(att, dict) \
        else getattr(att, "original_filename", "") or getattr(att, "filename", "file")
    ext = att.get("extension") if isinstance(att, dict) else getattr(att, "extension", "")
    if ext in (".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".toml"):
        try:
            with open(storage_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(200_000)
            return {"type": "text", "text": f"--- Content of {fname} ---\n{content}\n--- End of {fname} ---"}
        except Exception:
            pass
    if ext in (".pdf",):
        data = _read_file(storage_path)
        if data:
            b64 = base64.b64encode(data).decode("ascii")
            return {
                "type": "file",
                "file": {"data": b64, "mime_type": mime or "application/pdf", "filename": fname}
            }
    return {"type": "text", "text": f"[Attached document: {fname}]"}


def has_inline_content(attachments: list) -> bool:
    """Check if any attachment has stored file content that can be inlined."""
    for att in (attachments or []):
        storage = att.get("storage_path") if isinstance(att, dict) else getattr(att, "storage_path", "")
        if storage and os.path.isfile(storage):
            return True
    return False
