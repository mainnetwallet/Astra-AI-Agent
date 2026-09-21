"""Attachment processing for Astra multimodal support.

Handles file upload validation, MIME detection, safe storage, and archive
inspection. Uploaded files are untrusted data — this module enforces size
limits, blocks dangerous extensions, validates MIME types, and protects
against archive-based attacks (traversal, symlinks, zip bombs).

No uploaded file content is ever executed. No file overrides system
instructions, Gateway rules, routing rules, or security policy.
"""
from __future__ import annotations

import os
import tarfile
import uuid
import zipfile
from dataclasses import dataclass

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_EXTRACTED_SIZE = 200 * 1024 * 1024  # 200 MB
MAX_ARCHIVE_RATIO = 100  # compressed-to-extracted ratio limit (zip bomb guard)

# ── file families ──────────────────────────────────────────────────────────

FILE_FAMILIES: dict[str, str] = {
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".txt": "text", ".md": "text", ".html": "text", ".htm": "text",
    ".csv": "data", ".tsv": "data",
    ".xls": "spreadsheet", ".xlsx": "spreadsheet",
    ".ppt": "presentation", ".pptx": "presentation",
    ".json": "structured", ".xml": "structured", ".yaml": "structured",
    ".yml": "structured", ".toml": "structured",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".webp": "image", ".gif": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio",
    ".ogg": "audio", ".flac": "audio", ".opus": "audio",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".avi": "video",
    ".mkv": "video", ".m4v": "video",
    ".zip": "archive", ".tar": "archive", ".tgz": "archive", ".gz": "archive",
}

SUPPORTED_EXTENSIONS = frozenset(FILE_FAMILIES.keys())

DANGEROUS_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".com", ".scr", ".msi",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".pif", ".reg",
    ".dll", ".sys", ".cpl", ".inf", ".hta", ".lnk", ".app", ".action",
    ".command", ".workflow", ".bin", ".run", ".elf",
})

# ── MIME detection (magic bytes + extension fallback) ──────────────────────

_MAGIC_SIGNATURES: list[tuple[bytes, int, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"RIFF", 0, "image/webp"),        # RIFF....WEBP check below
    (b"%PDF", 0, "application/pdf"),
    (b"PK\x03\x04", 0, "application/zip"),
    (b"PK\x05\x06", 0, "application/zip"),
    (b"\x1f\x8b", 0, "application/gzip"),
    (b"ID3", 0, "audio/mpeg"),
    (b"\xff\xfb", 0, "audio/mpeg"),
    (b"\xff\xf3", 0, "audio/mpeg"),
    (b"\xff\xf2", 0, "audio/mpeg"),
    (b"fLaC", 0, "audio/flac"),
    (b"OggS", 0, "audio/ogg"),
]

_EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain", ".md": "text/markdown",
    ".html": "text/html", ".htm": "text/html",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json", ".xml": "application/xml",
    ".yaml": "application/x-yaml", ".yml": "application/x-yaml",
    ".toml": "application/toml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".opus": "audio/opus",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".m4v": "video/mp4",
    ".zip": "application/zip", ".tar": "application/x-tar",
    ".tgz": "application/gzip", ".gz": "application/gzip",
}


def detect_mime(data: bytes, filename: str) -> str:
    """Detect MIME type from magic bytes first, then fall back to extension."""
    if data and len(data) >= 12:
        for sig, offset, mime in _MAGIC_SIGNATURES:
            if data[offset:offset + len(sig)] == sig:
                if sig == b"RIFF" and data[8:12] != b"WEBP":
                    continue
                return mime
        if len(data) >= 8 and data[4:8] == b"ftyp":
            return "video/mp4"
        if data[:4] == b"\x00\x00\x00":
            if len(data) >= 8 and data[4:8] in (b"ftyp", b"mdat", b"moov"):
                return "video/mp4"
    ext = _ext(filename)
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


def _ext(filename: str) -> str:
    _, ext = os.path.splitext(str(filename or "").lower())
    return ext


# ── validation ─────────────────────────────────────────────────────────────

def validate_file(
    data: bytes, filename: str, max_size: int = MAX_FILE_SIZE
) -> tuple[bool, str]:
    """Validate an uploaded file. Returns (valid, error_message)."""
    if not filename or not filename.strip():
        return False, "filename is required"
    ext = _ext(filename)
    if ext in DANGEROUS_EXTENSIONS:
        return False, f"dangerous file type: {ext}"
    if data is None:
        return False, "no file data"
    if len(data) == 0:
        return False, "empty file"
    if len(data) > max_size:
        mb = max_size / (1024 * 1024)
        return False, f"file exceeds {mb:.0f} MB limit"
    mime = detect_mime(data, filename)
    if ext and ext in SUPPORTED_EXTENSIONS:
        expected = _EXT_TO_MIME.get(ext, "")
        if expected and mime != expected:
            if not (mime == "application/zip" and ext in
                    (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp")):
                pass  # allow ZIP-based office formats
    return True, ""


# ── archive safety ─────────────────────────────────────────────────────────

def validate_archive(path: str) -> tuple[bool, str]:
    """Validate an archive file for safety. Checks traversal, symlinks,
    zip bombs, excessive file count, and excessive extraction size."""
    if not os.path.isfile(path):
        return False, "archive file not found"
    ext = _ext(path)
    try:
        if ext in (".zip",):
            return _validate_zip(path)
        if ext in (".tar", ".tgz", ".gz"):
            return _validate_tar(path)
    except Exception as e:
        return False, f"archive inspection failed: {e}"
    return False, f"unsupported archive format: {ext}"


def _validate_zip(path: str) -> tuple[bool, str]:
    if not zipfile.is_zipfile(path):
        return False, "not a valid ZIP file"
    archive_size = os.path.getsize(path)
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        if len(names) > MAX_ARCHIVE_FILES:
            return False, f"too many files: {len(names)} (max {MAX_ARCHIVE_FILES})"
        total_uncompressed = 0
        for info in zf.infolist():
            if info.filename.startswith("/") or ".." in info.filename:
                return False, f"path traversal detected: {info.filename}"
            if info.external_attr & 0xA0000000:
                return False, f"symlink detected: {info.filename}"
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_ARCHIVE_EXTRACTED_SIZE:
                return False, "extracted size exceeds limit"
        if archive_size > 0 and total_uncompressed / archive_size > MAX_ARCHIVE_RATIO:
            return False, "compression ratio too high (possible zip bomb)"
    return True, ""


def _validate_tar(path: str) -> tuple[bool, str]:
    try:
        tf = tarfile.open(path, "r:*")
    except tarfile.TarError as e:
        return False, f"not a valid tar archive: {e}"
    with tf:
        members = tf.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            return False, f"too many files: {len(members)} (max {MAX_ARCHIVE_FILES})"
        total_size = 0
        for member in members:
            if member.name.startswith("/") or ".." in member.name:
                return False, f"path traversal detected: {member.name}"
            if member.issym() or member.islnk():
                return False, f"symlink detected: {member.name}"
            total_size += member.size
            if total_size > MAX_ARCHIVE_EXTRACTED_SIZE:
                return False, "extracted size exceeds limit"
    return True, ""


# ── Attachment dataclass ───────────────────────────────────────────────────

@dataclass
class Attachment:
    id: str = ""
    filename: str = ""
    original_filename: str = ""
    extension: str = ""
    mime_type: str = ""
    detected_type: str = ""
    size: int = 0
    storage_path: str = ""
    family: str = ""
    processed: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "filename": self.filename,
            "original_filename": self.original_filename,
            "extension": self.extension, "mime_type": self.mime_type,
            "detected_type": self.detected_type, "size": self.size,
            "family": self.family, "processed": self.processed,
            "error": self.error,
        }

    @property
    def is_supported(self) -> bool:
        return self.extension in SUPPORTED_EXTENSIONS and not self.error

    @property
    def is_image(self) -> bool:
        return self.family == "image"

    @property
    def is_audio(self) -> bool:
        return self.family == "audio"

    @property
    def is_video(self) -> bool:
        return self.family == "video"

    @property
    def is_archive(self) -> bool:
        return self.family == "archive"


# ── upload processing ──────────────────────────────────────────────────────

def process_upload(
    data: bytes, filename: str, upload_dir: str,
    max_size: int = MAX_FILE_SIZE,
) -> Attachment:
    """Process an uploaded file: validate, detect MIME, store safely."""
    att = Attachment(
        id=uuid.uuid4().hex[:12],
        original_filename=str(filename or ""),
        extension=_ext(filename),
    )
    valid, err = validate_file(data, filename, max_size)
    if not valid:
        att.error = err
        return att

    att.mime_type = detect_mime(data, filename)
    att.detected_type = att.mime_type
    att.size = len(data)
    att.family = FILE_FAMILIES.get(att.extension, "")

    safe_name = f"{att.id}_{_safe_filename(filename)}"
    att.filename = safe_name
    os.makedirs(upload_dir, exist_ok=True)
    dest = os.path.join(upload_dir, safe_name)
    if not os.path.abspath(dest).startswith(os.path.abspath(upload_dir)):
        att.error = "path traversal in filename"
        return att
    with open(dest, "wb") as f:
        f.write(data)
    att.storage_path = dest
    att.processed = True

    if att.is_archive:
        safe, archive_err = validate_archive(dest)
        if not safe:
            att.error = f"unsafe archive: {archive_err}"
            att.processed = False

    if not att.family:
        att.error = "unsupported file type"
        att.processed = False

    return att


def _safe_filename(filename: str) -> str:
    """Sanitize a filename: strip path components, limit length, remove
    dangerous characters."""
    name = os.path.basename(str(filename or "unknown"))
    name = name.replace("..", "").replace("/", "_").replace("\\", "_")
    name = "".join(c for c in name if c.isalnum() or c in "._-")
    return name[:200] or "file"


# ── normalization ──────────────────────────────────────────────────────────

def normalize_attachments(raw_attachments: list[dict]) -> list[Attachment]:
    """Convert raw attachment dicts (from multipart upload or JSON) to
    Attachment objects. Does NOT re-process already-stored files."""
    out = []
    for raw in (raw_attachments or []):
        if isinstance(raw, Attachment):
            out.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        att = Attachment(
            id=raw.get("id", uuid.uuid4().hex[:12]),
            filename=raw.get("filename", ""),
            original_filename=raw.get("original_filename", raw.get("filename", "")),
            extension=raw.get("extension", _ext(raw.get("filename", ""))),
            mime_type=raw.get("mime_type", ""),
            detected_type=raw.get("detected_type", raw.get("mime_type", "")),
            size=int(raw.get("size", 0)),
            storage_path=raw.get("storage_path", ""),
            family=raw.get("family", FILE_FAMILIES.get(
                _ext(raw.get("filename", "")), "")),
            processed=bool(raw.get("processed", False)),
            error=raw.get("error", ""),
        )
        out.append(att)
    return out


def upload_dir(base_dir: str) -> str:
    """Ensure and return the uploads directory path."""
    path = os.path.join(base_dir, "uploads")
    os.makedirs(path, exist_ok=True)
    return path
