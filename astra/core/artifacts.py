"""Artifact storage and validation for Astra multimodal output.

When a Provider generates a non-text artifact (image, audio, video,
document, spreadsheet, etc.), it is stored and validated here before being
returned to the user. An artifact is only reported as successful when it
actually exists, is non-empty, and passes type-specific validation.

This module never executes AI calls — it is a pure storage/validation layer.
"""
from __future__ import annotations

import json
import os
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime

ARTIFACT_TYPES = frozenset({
    "text", "image", "audio", "video", "document",
    "spreadsheet", "presentation", "data", "archive",
})

# ── Artifact dataclass ─────────────────────────────────────────────────────

@dataclass
class Artifact:
    id: str = ""
    filename: str = ""
    mime_type: str = ""
    artifact_type: str = ""
    size: int = 0
    storage_path: str = ""
    validated: bool = False
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "filename": self.filename,
            "mime_type": self.mime_type, "artifact_type": self.artifact_type,
            "size": self.size, "validated": self.validated,
            "created_at": self.created_at,
        }

    @property
    def exists(self) -> bool:
        return bool(self.storage_path) and os.path.isfile(self.storage_path)


# ── type-specific validators ───────────────────────────────────────────────

def _validate_json(path: str) -> tuple[bool, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True, ""
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"invalid JSON: {e}"


def _validate_pdf(path: str) -> tuple[bool, str]:
    with open(path, "rb") as f:
        header = f.read(5)
    if header[:4] != b"%PDF":
        return False, "not a valid PDF (missing %PDF header)"
    return True, ""


def _validate_image(path: str) -> tuple[bool, str]:
    with open(path, "rb") as f:
        header = f.read(12)
    if len(header) < 4:
        return False, "image file too small"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return True, ""
    if header[:3] == b"\xff\xd8\xff":
        return True, ""
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return True, ""
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True, ""
    return False, "unrecognized image format"


def _validate_office_zip(path: str, required_entry: str) -> tuple[bool, str]:
    """Validate a ZIP-based Office format (DOCX/XLSX/PPTX) by checking
    for the expected content type entry."""
    if not zipfile.is_zipfile(path):
        return False, "not a valid ZIP-based document"
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "[Content_Types].xml" not in zf.namelist():
                return False, "missing [Content_Types].xml"
            if required_entry and required_entry not in zf.namelist():
                ct = zf.read("[Content_Types].xml").decode("utf-8", errors="replace")
                if required_entry.split("/")[0] not in ct:
                    return False, f"missing expected content: {required_entry}"
        return True, ""
    except zipfile.BadZipFile:
        return False, "corrupted ZIP-based document"


def _validate_xlsx(path: str) -> tuple[bool, str]:
    return _validate_office_zip(path, "xl/workbook.xml")


def _validate_docx(path: str) -> tuple[bool, str]:
    return _validate_office_zip(path, "word/document.xml")


def _validate_pptx(path: str) -> tuple[bool, str]:
    return _validate_office_zip(path, "ppt/presentation.xml")


def _validate_audio(path: str) -> tuple[bool, str]:
    with open(path, "rb") as f:
        header = f.read(12)
    if len(header) < 4:
        return False, "audio file too small"
    if header[:3] == b"ID3" or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True, ""
    if header[:4] == b"fLaC":
        return True, ""
    if header[:4] == b"OggS":
        return True, ""
    if header[:4] == b"RIFF":
        return True, ""
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return True, ""
    return True, ""  # many audio formats; accept if non-empty


def _validate_video(path: str) -> tuple[bool, str]:
    with open(path, "rb") as f:
        header = f.read(12)
    if len(header) < 4:
        return False, "video file too small"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return True, ""
    if header[:4] == b"\x1a\x45\xdf\xa3":
        return True, ""
    if header[:4] == b"RIFF":
        return True, ""
    if header[:3] == b"\x00\x00\x01":
        return True, ""
    return True, ""  # many video formats; accept if non-empty


ARTIFACT_VALIDATORS: dict[str, callable] = {
    "image": _validate_image,
    "audio": _validate_audio,
    "video": _validate_video,
    "document": _validate_pdf,
    "spreadsheet": _validate_xlsx,
    "presentation": _validate_pptx,
    "data": _validate_json,
}

_EXT_VALIDATORS: dict[str, callable] = {
    ".pdf": _validate_pdf,
    ".docx": _validate_docx,
    ".xlsx": _validate_xlsx,
    ".pptx": _validate_pptx,
    ".json": _validate_json,
    ".png": _validate_image,
    ".jpg": _validate_image,
    ".jpeg": _validate_image,
    ".webp": _validate_image,
    ".gif": _validate_image,
}


# ── storage ────────────────────────────────────────────────────────────────

def make_artifact_dir(base_dir: str) -> str:
    """Ensure and return the artifacts directory path."""
    path = os.path.join(base_dir, "artifacts")
    os.makedirs(path, exist_ok=True)
    return path


def store_artifact(
    data: bytes, filename: str, artifact_type: str, dest_dir: str,
) -> Artifact:
    """Store artifact data to disk and return an Artifact with basic metadata.
    Does NOT validate — call validate_artifact() after storing."""
    art = Artifact(
        id=uuid.uuid4().hex[:12],
        filename=filename,
        artifact_type=artifact_type if artifact_type in ARTIFACT_TYPES else "text",
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if not data:
        art.validated = False
        return art

    os.makedirs(dest_dir, exist_ok=True)
    safe_name = f"{art.id}_{_safe_name(filename)}"
    dest = os.path.join(dest_dir, safe_name)
    if not os.path.abspath(dest).startswith(os.path.abspath(dest_dir)):
        return art
    with open(dest, "wb") as f:
        f.write(data)
    art.storage_path = dest
    art.size = len(data)
    _, ext = os.path.splitext(filename.lower())
    art.mime_type = _guess_mime(ext, artifact_type)
    return art


def validate_artifact(artifact: Artifact) -> tuple[bool, str]:
    """Validate that an artifact actually exists and matches its expected type.
    Returns (valid, error_message)."""
    if not artifact.storage_path:
        return False, "no storage path"
    if not os.path.isfile(artifact.storage_path):
        return False, "artifact file does not exist"
    if os.path.getsize(artifact.storage_path) == 0:
        return False, "artifact file is empty"

    _, ext = os.path.splitext(artifact.filename.lower())
    validator = _EXT_VALIDATORS.get(ext)
    if validator is None:
        validator = ARTIFACT_VALIDATORS.get(artifact.artifact_type)
    if validator is not None:
        valid, err = validator(artifact.storage_path)
        if not valid:
            return False, err

    artifact.validated = True
    artifact.size = os.path.getsize(artifact.storage_path)
    return True, ""


def _safe_name(filename: str) -> str:
    name = os.path.basename(str(filename or "artifact"))
    name = name.replace("..", "").replace("/", "_").replace("\\", "_")
    name = "".join(c for c in name if c.isalnum() or c in "._-")
    return name[:200] or "artifact"


def _guess_mime(ext: str, artifact_type: str) -> str:
    _map = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".json": "application/json",
        ".html": "text/html", ".htm": "text/html",
        ".txt": "text/plain", ".md": "text/markdown",
        ".csv": "text/csv",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".mp4": "video/mp4", ".webm": "video/webm",
    }
    if ext in _map:
        return _map[ext]
    _type_map = {
        "image": "image/png", "audio": "audio/mpeg", "video": "video/mp4",
        "document": "application/pdf", "spreadsheet": "application/octet-stream",
        "presentation": "application/octet-stream", "text": "text/plain",
        "data": "application/json",
    }
    return _type_map.get(artifact_type, "application/octet-stream")
