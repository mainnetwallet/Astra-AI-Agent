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

Messages belong to a *conversation* (a chat thread), so the Assistant tab can
keep several separate chats around — a new chat doesn't erase the old one,
and any earlier chat can be reopened by id. Exactly one conversation is
"current" at a time: new messages are appended to it, and `/api/chat/history`
reads it by default. `astra_chat_conversations` holds one row per thread
(auto-titled from its first message); `astra_chat_state` remembers which one
is current.

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
CREATE TABLE IF NOT EXISTS astra_chat_conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT DEFAULT '',      -- auto-filled from the first message
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''       -- bumped on every message; sort key for the list
);
CREATE TABLE IF NOT EXISTS astra_chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL DEFAULT 1,
    role       TEXT NOT NULL,            -- 'user' | 'ai'
    text       TEXT DEFAULT '',
    action     TEXT DEFAULT '',
    meta       TEXT DEFAULT '{}',        -- reply["data"] (JSON)
    artifacts  TEXT DEFAULT '[]',        -- reply["artifacts"] (JSON)
    files      TEXT DEFAULT '[]',        -- attached file NAMES (JSON)
    attachments TEXT DEFAULT '[]',       -- internal attachment metadata for conversational reuse
    ok         INTEGER DEFAULT 1,
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS astra_chat_state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

MAX_KEEP = 1000            # newest messages kept in the table (across all chats)
MAX_CONVERSATIONS = 200    # oldest empty/idle chats get trimmed past this
MAX_JSON_BYTES = 400_000   # per-message cap for meta / artifacts JSON
TITLE_MAX_CHARS = 48
PENDING_MAX_AGE_S = 15 * 60  # a turn "running" longer than this is presumed dead


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cap_json(value, fallback):
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(fallback)
    return raw if len(raw) <= MAX_JSON_BYTES else json.dumps(fallback)


def _make_title(text: str) -> str:
    title = " ".join(str(text or "").split())
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS - 1].rstrip() + "…"
    return title


class ChatLog:
    def __init__(self, store, redact=None):
        self.store = store
        self._redact = redact or (lambda x: x)
        self._lock = threading.Lock()
        # token -> (conversation_id, started). Keyed by conversation so a turn
        # running in one chat can't show a "typing…" indicator in another.
        self._running: dict[int, tuple[int, float]] = {}
        self._next = 0
        store.install(SCHEMA)
        store.ensure_column("astra_chat_messages", "conversation_id",
                             "INTEGER NOT NULL DEFAULT 1")
        self.store.ensure_column("astra_chat_messages", "attachments", "TEXT DEFAULT '[]'")
        self._bootstrap()

    def _bootstrap(self) -> None:
        """First launch, or an upgrade from the old single-thread schema:
        make sure conversation #1 and a `current` pointer both exist."""
        if not self.store.fetchone(
                "SELECT id FROM astra_chat_conversations WHERE id = 1"):
            self.store.exec(
                "INSERT INTO astra_chat_conversations (id, title, created_at, updated_at) "
                "VALUES (1, '', ?, ?)", (_now(), _now()))
        if not self.store.fetchone(
                "SELECT v FROM astra_chat_state WHERE k = 'current'"):
            self.store.exec(
                "INSERT INTO astra_chat_state (k, v) VALUES ('current', '1')")

    # -- current conversation pointer -------------------------------------------
    @property
    def current_id(self) -> int:
        row = self.store.fetchone("SELECT v FROM astra_chat_state WHERE k = 'current'")
        try:
            return int((row or {}).get("v") or 1)
        except Exception:
            return 1

    def _set_current(self, conv_id: int) -> None:
        self.store.exec(
            "INSERT INTO astra_chat_state (k, v) VALUES ('current', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(conv_id),))

    def _touch(self, conv_id: int, first_text: str | None = None) -> None:
        self.store.exec(
            "UPDATE astra_chat_conversations SET updated_at = ? WHERE id = ?",
            (_now(), conv_id))
        if first_text is None:
            return
        row = self.store.fetchone(
            "SELECT title FROM astra_chat_conversations WHERE id = ?", (conv_id,))
        if row is not None and not (row.get("title") or "").strip():
            title = _make_title(first_text)
            if title:
                self.store.exec(
                    "UPDATE astra_chat_conversations SET title = ? WHERE id = ?",
                    (title, conv_id))

    # -- conversations (chat threads) --------------------------------------------
    def list_conversations(self, limit: int = 50) -> list[dict]:
        cur = self.current_id
        rows = self.store.fetch(
            "SELECT c.id AS id, c.title AS title, c.created_at AS created_at, "
            "c.updated_at AS updated_at, "
            "(SELECT COUNT(*) FROM astra_chat_messages m WHERE m.conversation_id = c.id) AS n, "
            "(SELECT text FROM astra_chat_messages m WHERE m.conversation_id = c.id "
            " ORDER BY m.id ASC LIMIT 1) AS first_text "
            "FROM astra_chat_conversations c "
            "ORDER BY c.updated_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            n = r.get("n") or 0
            if not n and r["id"] != cur:
                continue   # an untouched empty chat is noise, unless it's the open one
            title = (r.get("title") or "").strip() or _make_title(r.get("first_text") or "") \
                or "New chat"
            out.append({
                "id": r["id"], "title": title, "count": n,
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "current": r["id"] == cur,
            })
        return out

    def new_conversation(self) -> int:
        """Used by the 🗑 New chat button. Reuses the current chat if it's
        still empty (no point piling up blank threads); otherwise opens a
        fresh one and makes it current."""
        cur = self.current_id
        row = self.store.fetchone(
            "SELECT COUNT(*) AS n FROM astra_chat_messages WHERE conversation_id = ?", (cur,))
        if not (row or {}).get("n"):
            return cur
        new_id = self.store.exec(
            "INSERT INTO astra_chat_conversations (title, created_at, updated_at) "
            "VALUES ('', ?, ?)", (_now(), _now()))
        self._set_current(new_id)
        self._prune_conversations()
        return new_id

    def switch(self, conv_id: int) -> bool:
        if not self.store.fetchone(
                "SELECT id FROM astra_chat_conversations WHERE id = ?", (conv_id,)):
            return False
        self._set_current(conv_id)
        return True

    def delete_conversation(self, conv_id: int) -> bool:
        if not self.store.fetchone(
                "SELECT id FROM astra_chat_conversations WHERE id = ?", (conv_id,)):
            return False
        self.store.exec(
            "DELETE FROM astra_chat_messages WHERE conversation_id = ?", (conv_id,))
        self.store.exec(
            "DELETE FROM astra_chat_conversations WHERE id = ?", (conv_id,))
        if self.current_id == conv_id:
            nxt = self.store.fetchone(
                "SELECT id FROM astra_chat_conversations ORDER BY updated_at DESC LIMIT 1")
            self._set_current(nxt["id"] if nxt else self.new_conversation())
        return True

    def _prune_conversations(self) -> None:
        try:
            self.store.exec(
                "DELETE FROM astra_chat_conversations WHERE id IN ("
                " SELECT id FROM astra_chat_conversations"
                " WHERE id != (SELECT v FROM astra_chat_state WHERE k = 'current')"
                " ORDER BY updated_at ASC"
                " LIMIT MAX(0, (SELECT COUNT(*) FROM astra_chat_conversations) - ?))",
                (MAX_CONVERSATIONS,))
        except Exception:
            pass

    # -- writes ---------------------------------------------------------------
    def _insert(self, conversation_id, role, text, action="", meta=None,
                artifacts=None, files=None, attachments=None, ok=True) -> int:
        rid = self.store.exec(
            "INSERT INTO astra_chat_messages "
            "(conversation_id, role, text, action, meta, artifacts, files, attachments, ok, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, role, str(text or ""), str(action or ""),
             _cap_json(meta or {}, {}), _cap_json(artifacts or [], []),
             _cap_json(files or [], []), _cap_json(attachments or [], []),
             1 if ok else 0, _now()))
        try:   # keep the table bounded (globally, across every chat)
            self.store.exec(
                "DELETE FROM astra_chat_messages WHERE id <= "
                "(SELECT MAX(id) FROM astra_chat_messages) - ?", (MAX_KEEP,))
        except Exception:
            pass
        return rid

    def add_user(self, text: str, files: list | None = None,
                 conversation_id: int | None = None,
                 attachments: list | None = None) -> int:
        """Returns the conversation id the message was actually written to,
        so the caller can pin the rest of the turn (begin/add_reply) to it —
        `current_id` can change while the agent is still working (the user
        opened or switched to another chat), and the reply must not follow
        it there.

        Retry/refresh guard: if this exact text is resubmitted for the same
        conversation while a turn there is still `pending` (e.g. a page
        refresh mid-turn fires the same POST /api/chat again, or a flaky
        connection causes the browser to retry), the message is NOT written
        a second time — the in-flight turn already covers it. Once the
        turn ends (`end()`), the guard lifts: sending the same text again on
        purpose creates a normal new message."""
        cid = conversation_id if conversation_id is not None else self.current_id
        redacted = self._redact(text or "")
        if self._is_duplicate_pending_submit(cid, redacted):
            return cid
        safe_attachments = []
        for att in (attachments or [])[:10]:
            if not isinstance(att, dict) or att.get("family") != "image":
                continue
            safe_attachments.append({
                "family": "image",
                "storage_path": str(att.get("storage_path") or ""),
                "mime_type": str(att.get("detected_type") or att.get("mime_type") or "image/png"),
                "original_filename": str(att.get("original_filename") or att.get("filename") or "image"),
            })
        self._insert(cid, "user", redacted,
                     files=[str(f) for f in (files or [])][:20],
                     attachments=safe_attachments)
        self._touch(cid, first_text=text)
        return cid

    def latest_image_attachment(self, conversation_id: int | None = None) -> dict | None:
        """Return the newest reusable image from this conversation.

        Priority follows conversation order: newest uploaded image first,
        otherwise the newest generated image artifact. Storage paths remain
        internal and are never returned by the public history API.
        """
        cid = conversation_id if conversation_id is not None else self.current_id
        rows = self.store.fetch(
            "SELECT attachments, artifacts FROM astra_chat_messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT 100",
            (cid,))
        for row in rows:
            try:
                attachments = json.loads(row.get("attachments") or "[]")
            except Exception:
                attachments = []
            for att in attachments:
                if (isinstance(att, dict) and att.get("family") == "image"
                        and att.get("storage_path")):
                    return att
            try:
                artifacts = json.loads(row.get("artifacts") or "[]")
            except Exception:
                artifacts = []
            for art in artifacts:
                if (isinstance(art, dict) and (art.get("type") == "image" or art.get("artifact_type") == "image")
                        and (art.get("storage_path") or art.get("_storage_path"))):
                    return {
                        "family": "image",
                        "storage_path": str(art.get("storage_path") or art.get("_storage_path")),
                        "mime_type": str(art.get("mime_type") or "image/png"),
                        "original_filename": str(art.get("filename") or "generated.png"),
                    }
        return None

    def _is_duplicate_pending_submit(self, conversation_id: int, text: str) -> bool:
        if not text.strip() or not self.pending_for(conversation_id):
            return False
        last = self.store.fetchone(
            "SELECT role, text FROM astra_chat_messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT 1", (conversation_id,))
        return bool(last) and last.get("role") == "user" and last.get("text") == text

    def is_duplicate_pending(self, conversation_id: int, text: str) -> bool:
        """Public check a caller (e.g. the `/api/chat` route) can make
        BEFORE doing any work: True if `text` looks like a retry/refresh of
        a message whose turn is still running for `conversation_id` — same
        text (after the same redaction `add_user` applies) as the most
        recent user message there, while that conversation still has a
        turn in flight. Callers should skip starting a second agent run
        entirely in that case, not just skip the duplicate log row."""
        return self._is_duplicate_pending_submit(conversation_id, self._redact(text or ""))

    def add_reply(self, reply: dict, conversation_id: int | None = None) -> int:
        cid = conversation_id if conversation_id is not None else self.current_id
        reply = self._redact(reply if isinstance(reply, dict) else {"reply": str(reply)})
        data = reply.get("data") or {}
        if isinstance(data, dict) and len(json.dumps(data, default=str)) > MAX_JSON_BYTES:
            data = {k: data[k] for k in ("execution_id",) if k in data}
        artifacts = list(reply.get("artifacts") or [])
        internal_attachments = []
        public_artifacts = []
        for art in artifacts:
            if not isinstance(art, dict):
                continue
            clean = dict(art)
            internal_path = clean.pop("_storage_path", "")
            if (internal_path and clean.get("artifact_type") == "image"
                    and clean.get("id")):
                internal_attachments.append({
                    "family": "image",
                    "storage_path": str(internal_path),
                    "mime_type": str(clean.get("mime_type") or "image/png"),
                    "original_filename": str(clean.get("filename") or "generated.png"),
                })
            public_artifacts.append(clean)
        # Never expose the internal artifact path in the response object.
        if isinstance(reply, dict):
            reply["artifacts"] = public_artifacts
        rid = self._insert(cid, "ai", reply.get("reply", ""), reply.get("action", ""),
                            data, public_artifacts,
                            attachments=internal_attachments,
                            ok=bool(reply.get("ok", True)))
        self._touch(cid)
        return rid

    def update_message_meta(self, message_id: int, meta: dict) -> bool:
        """Replace ONE message's meta JSON in place.

        Used by the host-fallback approval flow to write the final decision
        (approved / denied / expired / completed / failed) back into the card
        the user is looking at, so a reloaded chat shows the resolved state
        instead of a stale "pending" card. Only `approval` is ever touched —
        the caller cannot rewrite arbitrary stored state."""
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return False
        incoming = (meta or {}).get("approval")
        if not isinstance(incoming, dict) or not incoming:
            return False
        row = self.store.fetchone(
            "SELECT meta FROM astra_chat_messages WHERE id = ?", (mid,))
        if not row:
            return False
        try:
            current = json.loads(row.get("meta") or "{}")
        except (ValueError, TypeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current["approval"] = incoming
        self.store.exec("UPDATE astra_chat_messages SET meta = ? WHERE id = ?",
                        (_cap_json(current, {}), mid))
        return True

    def clear(self) -> int:
        """Hard reset: wipe every chat and every message, back to one empty
        thread. Kept for the full-wipe `/api/chat/history` DELETE route."""
        n = len(self.store.fetch("SELECT id FROM astra_chat_messages"))
        self.store.exec("DELETE FROM astra_chat_messages")
        self.store.exec("DELETE FROM astra_chat_conversations")
        self.store.exec(
            "INSERT INTO astra_chat_conversations (id, title, created_at, updated_at) "
            "VALUES (1, '', ?, ?)", (_now(), _now()))
        self._set_current(1)
        return n

    # -- in-flight tracking -----------------------------------------------------
    def begin(self, conversation_id: int | None = None) -> int:
        cid = conversation_id if conversation_id is not None else self.current_id
        with self._lock:
            self._next += 1
            self._running[self._next] = (cid, time.time())
            return self._next

    def end(self, token: int) -> None:
        with self._lock:
            self._running.pop(token, None)

    def _prune_stale(self) -> None:
        cutoff = time.time() - PENDING_MAX_AGE_S
        for t, (_cid, started) in list(self._running.items()):
            if started < cutoff:
                del self._running[t]

    @property
    def pending(self) -> bool:
        """True if ANY chat has a turn in flight. Kept for callers that don't
        care which chat; `history()` uses `pending_for` instead so the typing
        indicator doesn't leak into unrelated chats."""
        with self._lock:
            self._prune_stale()
            return bool(self._running)

    def pending_for(self, conversation_id: int) -> bool:
        with self._lock:
            self._prune_stale()
            return any(cid == conversation_id for cid, _started in self._running.values())

    # -- reads ------------------------------------------------------------------
    def history(self, after_id: int = 0, limit: int = 200,
                conversation_id: int | None = None) -> dict:
        cid = conversation_id if conversation_id is not None else self.current_id
        # `limit <= 0` means "no limit" — used when a caller wants the FULL
        # conversation (no Astra-imposed turn ceiling). SQLite has no
        # LIMIT-less-with-0 shorthand, so the WHERE/ORDER clause is shared and
        # only the LIMIT clause is dropped.
        unlimited = not limit or int(limit) <= 0
        if after_id > 0:
            sql = ("SELECT * FROM astra_chat_messages WHERE conversation_id = ? "
                   "AND id > ? ORDER BY id ASC")
            params = (cid, after_id)
        else:
            sql = ("SELECT * FROM astra_chat_messages WHERE conversation_id = ? "
                   "ORDER BY id DESC")
            params = (cid,)
        if not unlimited:
            sql += " LIMIT ?"
            params = params + (int(limit),)
        rows = self.store.fetch(sql, params)
        if after_id <= 0:
            rows = list(reversed(rows))

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
        last = self.store.fetchone(
            "SELECT MAX(id) AS m FROM astra_chat_messages WHERE conversation_id = ?", (cid,))
        return {"messages": msgs, "pending": self.pending_for(cid),
                "last_id": (last or {}).get("m") or 0, "conversation_id": cid}
