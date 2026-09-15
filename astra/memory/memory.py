"""Real memory for Astra.

Layers (all local, SQLite-backed, no secrets ever stored):
  short-term  — the current conversation/session scratch
  long-term   — persistent memories (astra_memories), recallable by keyword
  semantic    — keyword-scored recall over long-term (offline embeddings-free)
  episodic    — executed-task history (experiences)

The ExperienceStore learns what worked: before running a similar goal it
returns the highest-confidence successful strategy, and after each attempt it
records the outcome so future runs improve.
"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    category   TEXT DEFAULT 'note',
    tags       TEXT DEFAULT '',
    source     TEXT DEFAULT 'chat',
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS astra_experiences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,          -- goal pattern, e.g. "website workflow"
    strategy    TEXT DEFAULT '',        -- what was tried
    result      TEXT DEFAULT '',        -- outcome text
    failure     TEXT DEFAULT '',
    solution    TEXT DEFAULT '',
    tool        TEXT DEFAULT '',
    website     TEXT DEFAULT '',
    project     TEXT DEFAULT '',
    success     INTEGER DEFAULT 0,      -- 0/1
    attempts    INTEGER DEFAULT 1,
    confidence  REAL DEFAULT 0.0,
    created_at  TEXT DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{2,}", text.lower())}


class MemorySystem:
    def __init__(self, store, events=None):
        self.store = store
        self.events = events
        self.short_term: deque[tuple] = deque(maxlen=30)
        if not store.table_exists("astra_memories"):
            store.install(SCHEMA)

    # -- short-term ----------------------------------------------------------
    def remember_short(self, role: str, text: str) -> None:
        self.short_term.append((role, text))

    def transcript(self) -> list[dict]:
        return [{"role": r, "text": t} for r, t in self.short_term]

    # -- long-term -----------------------------------------------------------
    def save(self, content: str, category: str = "note", tags: str = "",
             source: str = "chat") -> dict:
        content = str(content).strip()
        if not content:
            raise ValueError("memory content empty")
        mid = self.store.insert("astra_memories", content=content,
                                category=category, tags=tags, source=source,
                                created_at=_now())
        if self.events:
            self.events.emit("memory.saved", agent="memory", id=mid,
                             category=category)
        return {"id": mid, "content": content, "category": category}

    def search(self, query: str, k: int = 5, category: str | None = None) -> list[dict]:
        """Semantic-ish recall: score recent memories by token overlap,
        exact-phrase bonus, recency weight. Offline, deterministic."""
        q_tokens = _tokens(query)
        if not q_tokens:
            return []
        cond, args = "", []
        if category:
            cond, args = " WHERE category = ?", [category]
        rows = self.store.fetch(
            f"SELECT * FROM astra_memories{cond} ORDER BY created_at DESC LIMIT 800",
            tuple(args))
        scored = []
        for r in rows:
            body_tokens = _tokens(r["content"])
            overlap = len(q_tokens & body_tokens)
            if overlap == 0:
                continue
            phrase = query.lower() in r["content"].lower()
            score = overlap + (2 if phrase else 0)
            try:
                age_days = (datetime.now() - datetime.strptime(
                    r["created_at"], "%Y-%m-%d %H:%M:%S")).days
                score += 1.0 / (1 + age_days / 7)
            except ValueError:
                pass
            scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
        return [r for _, r in scored[:k]]

    def recall(self, query: str, k: int = 5) -> list[dict]:
        return self.search(query, k)

    def get(self, mem_id: int) -> dict | None:
        return self.store.fetchone("SELECT * FROM astra_memories WHERE id = ?",
                                   (mem_id,))

    def forget(self, mem_id: int) -> None:
        self.store.exec("DELETE FROM astra_memories WHERE id = ?", (mem_id,))

    def all(self, limit: int = 100, category: str | None = None) -> list[dict]:
        if category:
            return self.store.fetch(
                "SELECT * FROM astra_memories WHERE category = ? "
                "ORDER BY id DESC LIMIT ?", (category, limit))
        return self.store.fetch("SELECT * FROM astra_memories ORDER BY id DESC LIMIT ?",
                                (limit,))

    def count(self) -> int:
        r = self.store.fetchone("SELECT COUNT(*) c FROM astra_memories")
        return r["c"] if r else 0

    def categories(self) -> list[dict]:
        return self.store.fetch(
            "SELECT category, COUNT(*) c FROM astra_memories GROUP BY category")


class ExperienceStore:
    """Episodic + experience memory: what worked on similar goals before."""

    def __init__(self, store, events=None):
        self.store = store
        self.events = events
        if not store.table_exists("astra_experiences"):
            store.install(SCHEMA)

    def add(self, pattern: str, *, strategy: str = "", result: str = "",
            failure: str = "", solution: str = "", tool: str = "",
            website: str = "", project: str = "", success: bool = False,
            attempts: int = 1) -> dict:
        confidence = 1.0 if success else max(0.0, 1.0 - attempts * 0.25)
        eid = self.store.insert(
            "astra_experiences", pattern=pattern.strip().lower() or "general",
            strategy=strategy, result=result[:500], failure=failure[:500],
            solution=solution[:500], tool=tool, website=website, project=project,
            success=1 if success else 0, attempts=attempts,
            confidence=round(confidence, 2), created_at=_now())
        return self.get(eid)

    def get(self, eid: int) -> dict | None:
        return self.store.fetchone("SELECT * FROM astra_experiences WHERE id = ?",
                                   (eid,))

    def recall(self, pattern: str, k: int = 3) -> list[dict]:
        """Rank related experiences by token overlap, then success/confidence."""
        p_tokens = _tokens(pattern)
        rows = self.store.fetch("SELECT * FROM astra_experiences "
                                "ORDER BY id DESC LIMIT 300")
        scored = []
        for r in rows:
            x_tokens = _tokens(r["pattern"])
            overlap = len(p_tokens & x_tokens)
            if overlap == 0 and p_tokens:
                continue
            success_bias = 3 if r["success"] else -2
            score = overlap + success_bias + r.get("confidence", 0)
            scored.append((score, r))
        scored.sort(key=lambda s: -s[0])
        return [r for _, r in scored[:k]]

    def learn(self, eid: int, *, success: bool, attempts: int | None = None) -> dict:
        """Update an experience after its execution result is known."""
        x = self.get(eid)
        if not x:
            return {}
        attempts = attempts if attempts is not None else max(1, int(x["attempts"]))
        confidence = 1.0 if success else max(0.0, 1.0 - attempts * 0.25)
        self.store.exec(
            "UPDATE astra_experiences SET success = ?, attempts = ?, confidence = ? "
            "WHERE id = ?", (1 if success else 0, attempts, round(confidence, 2), eid))
        return self.get(eid)

    def stats(self) -> dict:
        r = self.store.fetchone(
            "SELECT COUNT(*) c, SUM(success) s FROM astra_experiences")
        total = r["c"] or 0
        return {"total": total, "successful": r["s"] or 0,
                "success_rate": round((r["s"] or 0) / total, 2) if total else 0}