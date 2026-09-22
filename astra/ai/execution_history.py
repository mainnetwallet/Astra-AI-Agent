"""Agent/tool execution history — what the AI already did, kept separate
from the conversation transcript.

Three histories exist in Astra, deliberately distinct:

A. **Conversation history** — user/assistant messages. Owned by `ChatLog`
   and `ConversationContextBuilder`; never touched by this module.
B. **Agent/tool execution history** — this module. Which tools ran, with
   what arguments, and how they turned out. Never mixed into ChatLog.
C. **Terminal session state** — owned by `astra.terminal.TerminalSession`
   (cwd, shell, processes, command history).

Before every Gateway/Provider AI call the pipeline composes a bounded,
deterministic view of B and C so the model knows what has already been
attempted and what the terminal currently looks like — without ever
replaying unlimited output.

Scope isolation: entries are keyed by a scope id (the conversation id for
chat, or an explicit run id). Two unrelated conversations can never read
each other's execution history.
"""
from __future__ import annotations

import threading
from collections import OrderedDict, deque

# Memory/resource bounds on how many entries and scopes are RETAINED (a
# long-lived server must not grow without limit) — these are not AI context
# limits. Content itself is no longer truncated: the full tool result is kept
# so the model can reason about it, and provider-aware fitting to the
# selected model's real context window happens later (astra.ai.context_budget).
DEFAULT_MAX_ENTRIES = 40
DEFAULT_MAX_SCOPES = 200
DEFAULT_CONTEXT_ENTRIES = 12
DEFAULT_CONTEXT_CHARS = None


def _summarize(value, limit: int | None = None) -> str:
    """Deterministic, JSON-safe view of a tool result. No artificial cap by
    default (limit=None); an explicit limit still truncates when a caller
    genuinely wants a short summary."""
    import json
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = " ".join(text.split())
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


class ExecutionEntry:
    __slots__ = ("tool", "ok", "summary", "status", "args", "step", "seq")

    def __init__(self, tool: str, ok: bool, summary: str = "",
                 status: str = "", args: dict | None = None, step: int = 0,
                 seq: int = 0):
        self.tool = tool
        self.ok = ok
        self.summary = summary
        self.status = status
        self.args = args or {}
        self.step = step
        self.seq = seq

    def to_dict(self) -> dict:
        return {"tool": self.tool, "ok": self.ok, "status": self.status,
                "summary": self.summary, "step": self.step, "seq": self.seq}


class AgentExecutionHistory:
    """Bounded, thread-safe, per-scope log of tool executions.

    `max_entries`/`max_scopes` bound the in-RAM hot deque per scope — a
    memory-safety limit, not an AI context limit (see module docstring).
    Everything recorded is ALSO appended to a per-scope persistent log
    (via `astra.core.blob_store`), so an entry that ages out of the hot
    deque is not lost — `read_log`/the `execution_history_read` tool can
    still page back to it.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_scopes: int = DEFAULT_MAX_SCOPES, blobs=None):
        self.max_entries = max(1, int(max_entries))
        # Ordered so the least-recently-used scope can be evicted, bounding
        # memory on a long-lived server that serves many conversations.
        self.max_scopes = max(1, int(max_scopes))
        self._scopes: OrderedDict[str, deque] = OrderedDict()
        self._seq: dict[str, int] = {}
        self._log_blob_ids: dict[str, str] = {}
        self._lock = threading.RLock()
        if blobs is None:
            from astra.core.blob_store import BlobStore
            blobs = BlobStore()
        self._blobs = blobs

    def _log_blob_id(self, key: str) -> str:
        blob_id = self._log_blob_ids.get(key)
        if blob_id is None:
            blob_id = self._blobs.open(f"execution_history_{key}")
            self._log_blob_ids[key] = blob_id
        return blob_id

    def record(self, scope: str, tool: str, *, ok: bool, status: str = "",
               result=None, args: dict | None = None, step: int = 0) -> None:
        key = str(scope or "default")
        with self._lock:
            seq = self._seq.get(key, 0) + 1
            self._seq[key] = seq
            entry = ExecutionEntry(tool, bool(ok), _summarize(result), status,
                                   args, step, seq)
            bucket = self._scopes.get(key)
            if bucket is None:
                while len(self._scopes) >= self.max_scopes:
                    self._scopes.popitem(last=False)
                bucket = deque(maxlen=self.max_entries)
                self._scopes[key] = bucket
            self._scopes.move_to_end(key)
            bucket.append(entry)
            blob_id = self._log_blob_id(key)
        try:
            import json
            self._blobs.append(blob_id, json.dumps(entry.to_dict(),
                                                    ensure_ascii=False,
                                                    default=str) + "\n")
        except Exception:
            pass

    def entries(self, scope: str, limit: int = DEFAULT_CONTEXT_ENTRIES) -> list[dict]:
        with self._lock:
            bucket = list(self._scopes.get(str(scope or "default")) or [])
        if limit and limit > 0:
            bucket = bucket[-limit:]
        return [e.to_dict() for e in bucket]

    def read_log(self, scope: str, offset: int = 0,
                 length: int = 6000) -> dict:
        """Page through the FULL, unbounded execution log for `scope` —
        every recorded entry, not just what the hot deque still holds."""
        key = str(scope or "default")
        with self._lock:
            blob_id = self._log_blob_ids.get(key)
        if blob_id is None:
            return {"status": "ok", "scope": key, "entries": [],
                    "offset": 0, "next_offset": None, "done": True,
                    "total_chars": 0}
        chunk = self._blobs.read(blob_id, offset=offset, length=length)
        if chunk.get("status") != "ok":
            return chunk
        import json
        text = chunk.get("text", "")
        off = chunk["offset"]
        total = chunk["total_chars"]
        nl = text.rfind("\n")
        if nl == -1:
            return {"status": "ok", "scope": key, "entries": [],
                    "offset": off, "next_offset": off, "done": off >= total,
                    "total_chars": total,
                    "note": ("no complete log line fit in this chunk; "
                            "retry with a larger length")}
        usable = text[:nl + 1]
        rows = []
        for line in usable.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        next_offset = off + len(usable)
        done = next_offset >= total
        return {"status": "ok", "scope": key, "entries": rows, "offset": off,
                "next_offset": None if done else next_offset, "done": done,
                "total_chars": total}

    def clear(self, scope: str | None = None) -> None:
        with self._lock:
            if scope is None:
                self._scopes.clear()
            else:
                self._scopes.pop(str(scope or "default"), None)

    def context_text(self, scope: str, *, max_entries: int = DEFAULT_CONTEXT_ENTRIES,
                     max_chars: int | None = DEFAULT_CONTEXT_CHARS,
                     exclude_step: int | None = None) -> str:
        rows = self.entries(scope, limit=max_entries)
        if exclude_step is not None:
            rows = [r for r in rows if r.get("step") != exclude_step]
        if not rows:
            return ""
        lines = ["Actions already taken (agent/tool execution history):"]
        for r in rows:
            mark = "ok" if r["ok"] else "FAILED"
            line = f"  - {r['tool']} [{mark}]"
            if r.get("status"):
                line += f" status={r['status']}"
            if r.get("summary"):
                line += f": {r['summary']}"
            lines.append(line)
        text = "\n".join(lines)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text
