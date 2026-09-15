"""Real memory for Astra.

Layers (all local, SQLite-backed, no secrets ever stored):
  working   — the current execution context/scratch (in-process, session)
  short     — recent conversation/notes, high recall priority, prunable
  long      — persistent memories (astra_memories), recallable by keyword
  semantic  — keyword-scored recall over long-term (offline embeddings-free)
  episodic  — executed-task history (experiences)

Memory 2.0 fields: every memory carries importance, confidence and a
recency/access tracker so recall ranks what actually matters — and exact
duplicates are collapsed automatically (dedupe_hash). Scoring stays offline,
deterministic and token-based: no embeddings, no external models, no fakes.

The ExperienceStore learns what worked: before running a similar goal it
returns the highest-confidence successful strategy, and after each attempt it
records the outcome so future runs improve.
"""
from __future__ import annotations

import hashlib
import re
from collections import deque
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    category      TEXT DEFAULT 'note',
    tags          TEXT DEFAULT '',
    source        TEXT DEFAULT 'chat',
    layer         TEXT DEFAULT 'long',
    importance    REAL DEFAULT 0.5,
    confidence    REAL DEFAULT 0.8,
    last_accessed TEXT DEFAULT '',
    access_count  INTEGER DEFAULT 0,
    dedupe_hash   TEXT DEFAULT '',
    user_data     TEXT DEFAULT '{}',
    created_at    TEXT DEFAULT ''
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


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# columns added by Memory 2.0 — existing DBs get them via ALTER (self-heal)
_MEMORY2_COLUMNS = {
    "layer": "TEXT DEFAULT 'long'",
    "importance": "REAL DEFAULT 0.5",
    "confidence": "REAL DEFAULT 0.8",
    "last_accessed": "TEXT DEFAULT ''",
    "access_count": "INTEGER DEFAULT 0",
    "dedupe_hash": "TEXT DEFAULT ''",
    "user_data": "TEXT DEFAULT '{}'",
}


def _dedupe_hash(content: str) -> str:
    """Stable content hash from normalized tokens (never the raw text)."""
    norm = " ".join(sorted(_tokens(content)))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24] if norm else ""


class MemorySystem:
    def __init__(self, store, events=None):
        self.store = store
        self.events = events
        self.short_term: deque[tuple] = deque(maxlen=30)
        if not store.table_exists("astra_memories"):
            store.install(SCHEMA)
        else:
            self._ensure_columns()   # self-heal old DBs to Memory 2.0

    def _ensure_columns(self) -> None:
        """ALTER a pre-Memory-2.0 table in place (idempotent, missing-columns
        only). Old rows fall back to schema defaults automatically."""
        cols = {r["name"] for r in self.store.fetch("PRAGMA table_info(astra_memories)")}
        for name, ddl in _MEMORY2_COLUMNS.items():
            if name not in cols:
                try:
                    self.store.exec(f"ALTER TABLE astra_memories ADD COLUMN {name} {ddl}")
                except Exception:
                    pass  # concurrent create race — safe to ignore

    # -- short-term ----------------------------------------------------------
    def remember_short(self, role: str, text: str) -> None:
        self.short_term.append((role, text))

    def transcript(self) -> list[dict]:
        return [{"role": r, "text": t} for r, t in self.short_term]

    # -- long-term -----------------------------------------------------------
    def save(self, content: str, category: str = "note", tags: str = "",
             source: str = "chat", layer: str = "long",
             importance: float = 0.5, confidence: float = 0.8,
             user_data: dict | None = None) -> dict:
        """Store a memory. importance/confidence guide recall ranking;
        layer declares which memory tier owns it. Same DB, same API."""
        content = str(content).strip()
        if not content:
            raise ValueError("memory content empty")
        if layer not in ("working", "short", "long", "semantic", "episodic"):
            layer = "long"
        importance = max(0.0, min(1.0, float(importance or 0.5)))
        confidence = max(0.0, min(1.0, float(confidence or 0.8)))
        dedupe = _dedupe_hash(content)
        if dedupe:
            existing = self.store.fetchone(
                "SELECT id FROM astra_memories WHERE dedupe_hash = ? LIMIT 1",
                (dedupe,))
            if existing:                       # exact duplicate — just refresh
                self.store.exec(
                    "UPDATE astra_memories SET created_at = ?, "
                    "importance = MAX(importance, ?), "
                    "confidence = MAX(confidence, ?), "
                    "category = ?, tags = ? WHERE id = ?",
                    (_now(), importance, confidence, category, tags,
                     existing["id"]))
                if self.events:
                    self.events.emit("memory.saved", agent="memory",
                                     id=existing["id"], category=category,
                                     deduplicated=True)
                return {"id": existing["id"], "content": content,
                        "category": category, "deduplicated": True}
        mid = self.store.insert(
            "astra_memories", content=content, category=category, tags=tags,
            source=source, layer=layer, importance=importance,
            confidence=confidence, dedupe_hash=dedupe,
            user_data=json_dumps(user_data or {}), created_at=_now())
        if self.events:
            self.events.emit("memory.saved", agent="memory", id=mid,
                             category=category, layer=layer)
        return {"id": mid, "content": content, "category": category,
                "layer": layer, "deduplicated": False}

    def touch(self, mem_id: int) -> None:
        """Mark a memory as accessed (recency + access_count for ranking)."""
        try:
            self.store.exec(
                "UPDATE astra_memories SET last_accessed = ?, "
                "access_count = access_count + 1 WHERE id = ?",
                (_now(), mem_id))
        except Exception:
            pass

    def search(self, query: str, k: int = 5, category: str | None = None,
               layer: str | None = None, min_importance: float | None = None,
               recall_touch: bool = True) -> list[dict]:
        """Semantic-ish recall: score memories by token overlap, exact-phrase
        bonus, then weight by importance, confidence and recency. Offline and
        deterministic — no embeddings, no fakes."""
        q_tokens = _tokens(query)
        if not q_tokens:
            return []
        cond, args = [], []
        if category:
            cond.append("category = ?"); args.append(category)
        if layer:
            cond.append("layer = ?"); args.append(layer)
        if min_importance is not None:
            cond.append("importance >= ?"); args.append(float(min_importance))
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        rows = self.store.fetch(
            f"SELECT * FROM astra_memories{where} "
            f"ORDER BY created_at DESC LIMIT 800", tuple(args))
        now = datetime.now()
        scored = []
        for r in rows:
            body_tokens = _tokens(r["content"])
            overlap = len(q_tokens & body_tokens)
            if overlap == 0:
                continue
            phrase = query.lower() in r["content"].lower()
            score = overlap + (2 if phrase else 0)
            try:
                age_days = (now - datetime.strptime(
                    r["created_at"], "%Y-%m-%d %H:%M:%S")).days
                score += 1.0 / (1 + age_days / 7)          # recency
            except ValueError:
                pass
            imp = float(r.get("importance") or 0.5)
            conf = float(r.get("confidence") or 0.8)
            score += imp * 2 + conf                         # quality weight
            scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
        top = [r for _, r in scored[:k]]
        for r in top:                                       # recency update
            self.touch(r["id"])
        if self.events and top:
            self.events.emit("memory.recalled", agent="memory",
                             query=query[:120], count=len(top))
        return top

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

    def deduplicate(self) -> int:
        """Collapse exact duplicates (same dedupe_hash) keeping the newest;
        legacy rows without a hash are back-filled first. Returns removed."""
        # give legacy rows their own hash
        for r in self.store.fetch("SELECT id, content FROM astra_memories "
                                  "WHERE dedupe_hash = '' LIMIT 500"):
            h = _dedupe_hash(r["content"])
            if h:
                self.store.exec("UPDATE astra_memories SET dedupe_hash = ? WHERE id = ?",
                                (h, r["id"]))
        # remove non-newest members of each duplicate group
        removed = self.store.exec(
            "DELETE FROM astra_memories WHERE id NOT IN ("
            " SELECT max(id) FROM astra_memories GROUP BY dedupe_hash)")
        return removed

    def by_layer(self, layer: str | None = None, limit: int = 100) -> list[dict]:
        if layer:
            return self.store.fetch(
                "SELECT * FROM astra_memories WHERE layer = ? "
                "ORDER BY id DESC LIMIT ?", (layer, limit))
        return self.store.fetch(
            "SELECT * FROM astra_memories ORDER BY id DESC LIMIT ?", (limit,))

    def stats(self) -> dict:
        per_layer = {}
        for r in self.store.fetch(
                "SELECT layer, COUNT(*) c FROM astra_memories GROUP BY layer"):
            per_layer[r["layer"]] = r["c"]
        return {"total": sum(per_layer.values()), "by_layer": per_layer}


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