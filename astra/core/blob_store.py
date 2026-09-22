"""Bounded-memory, provider-aware retrieval for large Browser/Terminal/
Execution output.

The problem this solves: several subsystems (browser page observation,
terminal stdout/stderr, terminal command history, agent execution history)
used to enforce a *hard* character/entry cap and throw the remainder away
(`text[:max_chars] + "[...truncated]"`). That protects RAM and the model's
context window, but it also means the AI can never see the rest of a big
page or a long build log even when the task genuinely needs it later.

`BlobStore` replaces "cap and discard" with "cap what goes in the hot
result/context, but keep the full content retrievable":

* callers still get a small, bounded value back immediately (what used to
  be the truncated text) — nothing about existing hot-path behaviour or
  memory bounds changes;
* the *complete* text is persisted here under a short opaque id;
* a `*_read`/`*_chunk` tool (see `astra.browser`, `astra.terminal`,
  `astra.ai.execution_history`) lets the AI fetch further slices with
  `offset`/`length` when it actually needs them.

Storage backend: the shared SQLite `Store` when one is available (durable,
survives process restarts, already the app's one persistence layer — no
second database). When no `Store` is wired (e.g. a subsystem built
standalone in a unit test) it falls back to a small in-process ring that
evicts the oldest blobs once a fixed byte budget is exceeded, so even the
fallback path cannot grow without bound.

This module intentionally knows nothing about browsers, terminals, tokens
or models — it is just bounded, retrievable storage for large text.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict

DEFAULT_CHUNK_CHARS = 6000
# Fallback in-memory ring budget (bytes of text kept resident) when this
# BlobStore has no persistent Store backing it. Oldest blobs are evicted
# first — this is a memory-safety bound, not an AI context limit.
_FALLBACK_MAX_BYTES = 20_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS output_blobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    size INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_output_blobs_kind ON output_blobs(kind);
"""


def _new_id(kind: str) -> str:
    raw = f"{kind}:{time.time()}:{os.urandom(8).hex()}"
    return f"{kind[:12]}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


class BlobStore:
    """Persist large text blobs; serve them back in bounded chunks."""

    def __init__(self, store=None):
        self._store = store
        self._lock = threading.Lock()
        self._mem: "OrderedDict[str, str]" = OrderedDict()
        self._mem_bytes = 0
        if self._store is not None:
            try:
                self._store.install(_SCHEMA)
            except Exception:
                # A Store that cannot install our table is treated as no
                # Store at all — the in-memory fallback still works, it
                # just will not survive a restart.
                self._store = None

    # -- writing ---------------------------------------------------------
    def put(self, kind: str, text: str) -> dict:
        """Persist a complete blob in one call. Returns {id, size}."""
        blob_id = self.open(kind)
        size = self.append(blob_id, text or "")
        return {"id": blob_id, "size": size}

    def open(self, kind: str) -> str:
        """Create an empty blob and return its id, for incremental writes
        (e.g. streaming terminal output as it arrives)."""
        blob_id = _new_id(kind)
        if self._store is not None:
            try:
                self._store.exec(
                    "INSERT INTO output_blobs (id, kind, created_at, size, "
                    "text) VALUES (?, ?, ?, 0, '')",
                    (blob_id, kind, time.time()))
                return blob_id
            except Exception:
                self._store = None
        with self._lock:
            self._mem[blob_id] = ""
        return blob_id

    def append(self, blob_id: str, text: str) -> int:
        """Append `text` to an existing blob. Returns the new total size.
        Never truncates — every call to `append` is preserved in full."""
        if not text:
            return self.size(blob_id)
        if self._store is not None:
            try:
                self._store.exec(
                    "UPDATE output_blobs SET text = text || ?, "
                    "size = size + ? WHERE id = ?",
                    (text, len(text), blob_id))
                row = self._store.fetchone(
                    "SELECT size FROM output_blobs WHERE id = ?", (blob_id,))
                return int(row["size"]) if row else len(text)
            except Exception:
                self._store = None
        with self._lock:
            cur = self._mem.get(blob_id, "")
            cur = cur + text
            self._mem[blob_id] = cur
            self._mem.move_to_end(blob_id)
            self._mem_bytes += len(text)
            while self._mem_bytes > _FALLBACK_MAX_BYTES and len(self._mem) > 1:
                old_id, old_text = self._mem.popitem(last=False)
                if old_id == blob_id:
                    # Never evict the blob we're actively writing.
                    self._mem[old_id] = old_text
                    self._mem.move_to_end(old_id)
                    break
                self._mem_bytes -= len(old_text)
            return len(cur)

    # -- reading -----------------------------------------------------------
    def size(self, blob_id: str) -> int:
        text = self._get(blob_id)
        return len(text) if text is not None else 0

    def read(self, blob_id: str, offset: int = 0,
              length: int = DEFAULT_CHUNK_CHARS) -> dict:
        """Bounded, offset-addressable read of a blob. Always returns a
        SMALL chunk (never the whole blob) so retrieval itself cannot
        re-create the original context-window problem."""
        text = self._get(blob_id)
        if text is None:
            return {"status": "error", "error": f"unknown id: {blob_id}"}
        offset = max(0, int(offset or 0))
        length = max(1, min(int(length or DEFAULT_CHUNK_CHARS),
                            DEFAULT_CHUNK_CHARS * 4))
        chunk = text[offset:offset + length]
        total = len(text)
        next_offset = offset + len(chunk)
        return {"status": "ok", "id": blob_id, "offset": offset,
                "total_chars": total, "chars_returned": len(chunk),
                "text": chunk, "done": next_offset >= total,
                "next_offset": None if next_offset >= total else next_offset}

    def _get(self, blob_id: str):
        if not blob_id:
            return None
        if self._store is not None:
            try:
                row = self._store.fetchone(
                    "SELECT text FROM output_blobs WHERE id = ?", (blob_id,))
                return row["text"] if row else None
            except Exception:
                return None
        with self._lock:
            return self._mem.get(blob_id)
