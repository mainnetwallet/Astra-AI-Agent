"""Server-side chat transcript for the 🤖 Assistant tab.

Until now a chat lived only in the browser page: a refresh (or a second
device) showed an empty "first open" screen, and a reply that finished while
the page was reloading was simply lost because the response socket was gone.

ChatLog fixes both:
  * every user message is saved the moment it arrives, and the assistant
    reply is saved the moment it is produced — whether or not the browser is
    still connected to receive it;
  * `pending` says a turn is still being worked on, so a reloaded page can
    show the typing indicator and pick the reply up when it lands.

Only what the UI needs is kept: text, action, small reply metadata,
artifacts, and the *names* of attached files (never their content). Text is
passed through the API redactor before it is stored, so a secret can't be
written to disk through the transcript either.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,            -- 'user' | 'ai'
    text       TEXT DEFAULT '',
    action     TEXT DEFAULT '',
    meta       TEXT DEFAULT '{}',        -- reply["data"] (JSON)
    artifacts  TEXT DEFAULT '[]',        -- reply["artifacts"] (JSON)
    files      TEXT DEFAULT '[]',        -- attached file NAMES (JSON)
    ok         INTEGER DEFAULT 1,
    created_at TEXT DEFAULT ''
);
"""

MAX_KEEP = 1000            # newest messages kept in the table
MAX_JSON_BYTES = 400_000   # per-message cap for meta / artifacts JSON
PENDING_MAX_AGE_S = 15 * 60  # a turn "running" longer than this is presumed dead


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cap_json(value, fallback):
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(fallback)
    return raw if len(raw) <= MAX_JSON_BYTES else json.dumps(fallback)


class ChatLog:
    def __init__(self, store, redact=None):
        self.store = store
        self._redact = redact or (lambda x: x)
        self._lock = threading.Lock()
        self._running: dict[int, float] = {}   # token -> started (monotonic-ish)
        self._next = 0
        store.install(SCHEMA)

    # -- writes ---------------------------------------------------------------
    def _insert(self, role, text, action="", meta=None, artifacts=None,
                files=None, ok=True) -> int:
        rid = self.store.exec(
            "INSERT INTO astra_chat_messages "
            "(role, text, action, meta, artifacts, files, ok, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (role, str(text or ""), str(action or ""),
             _cap_json(meta or {}, {}), _cap_json(artifacts or [], []),
             _cap_json(files or [], []), 1 if ok else 0, _now()))
        try:   # keep the table bounded
            self.store.exec(
                "DELETE FROM astra_chat_messages WHERE id <= "
                "(SELECT MAX(id) FROM astra_chat_messages) - ?", (MAX_KEEP,))
        except Exception:
            pass
        return rid

    def add_user(self, text: str, files: list | None = None) -> int:
        return self._insert("user", self._redact(text or ""),
                            files=[str(f) for f in (files or [])][:20])

    def add_reply(self, reply: dict) -> int:
        reply = self._redact(reply if isinstance(reply, dict) else {"reply": str(reply)})
        data = reply.get("data") or {}
        if isinstance(data, dict) and len(json.dumps(data, default=str)) > MAX_JSON_BYTES:
            data = {k: data[k] for k in ("execution_id",) if k in data}
        return self._insert("ai", reply.get("reply", ""), reply.get("action", ""),
                            data, reply.get("artifacts") or [],
                            ok=bool(reply.get("ok", True)))

    def clear(self) -> int:
        n = len(self.store.fetch("SELECT id FROM astra_chat_messages"))
        self.store.exec("DELETE FROM astra_chat_messages")
        return n

    # -- in-flight tracking -----------------------------------------------------
    def begin(self) -> int:
        with self._lock:
            self._next += 1
            self._running[self._next] = time.time()
            return self._next

    def end(self, token: int) -> None:
        with self._lock:
            self._running.pop(token, None)

    @property
    def pending(self) -> bool:
        cutoff = time.time() - PENDING_MAX_AGE_S
        with self._lock:
            for t, started in list(self._running.items()):
                if started < cutoff:
                    del self._running[t]
            return bool(self._running)

    # -- reads ------------------------------------------------------------------
    def history(self, after_id: int = 0, limit: int = 200) -> dict:
        if after_id > 0:
            rows = self.store.fetch(
                "SELECT * FROM astra_chat_messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                (after_id, limit))
        else:
            rows = list(reversed(self.store.fetch(
                "SELECT * FROM astra_chat_messages ORDER BY id DESC LIMIT ?", (limit,))))

        def load(v, default):
            try:
                return json.loads(v) if v else default
            except Exception:
                return default

        msgs = [{"id": r["id"], "role": r["role"], "text": r["text"],
                 "action": r["action"], "data": load(r["meta"], {}),
                 "artifacts": load(r["artifacts"], []),
                 "files": load(r["files"], []), "ok": bool(r["ok"]),
                 "created_at": r["created_at"]} for r in rows]
        last = self.store.fetchone("SELECT MAX(id) AS m FROM astra_chat_messages")
        return {"messages": msgs, "pending": self.pending,
                "last_id": (last or {}).get("m") or 0}
