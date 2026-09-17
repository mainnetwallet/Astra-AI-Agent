"""Astra plugin: Airdrop Manager.

The first plugin on the Astra platform — ports the fully-tested "AirdropAgent"
feature set (airdrops, tasks, wallets, deadlines, Banglish NL chat) onto the
generic plugin core so it can coexist with future plugins.

Registering a new domain later just means another Plugin subclass — editing
this file or `astra/` is not required.

Contract: every `_handler(msg)` returns `(text, action, data)` and
`process()` wraps it as `(handled=True, text, action, data)`.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from astra.core import Plugin

ALLOWED_STATUS = ("new", "active", "farming", "done", "dropped", "claimable")

SCHEMA = """
CREATE TABLE IF NOT EXISTS airdrops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    project         TEXT DEFAULT '',
    status          TEXT DEFAULT 'active' CHECK(status IN ('new','active','farming','done','dropped','claimable')),
    reward_type     TEXT DEFAULT 'tbd',
    estimated_value TEXT DEFAULT '',
    network         TEXT DEFAULT 'TBD',
    link            TEXT DEFAULT '',
    deadline        TEXT DEFAULT '',
    phase           TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    created_at      TEXT DEFAULT '',
    updated_at      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    airdrop_id  INTEGER NOT NULL REFERENCES airdrops(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    category    TEXT DEFAULT 'other',
    status      TEXT DEFAULT 'pending' CHECK(status IN ('pending','done')),
    target_url  TEXT DEFAULT '',
    created_at  TEXT DEFAULT '',
    done_at     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wallets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT DEFAULT '',
    address    TEXT NOT NULL,
    network    TEXT DEFAULT 'TBD',
    note       TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""


# --------------------------------------------------------------------------
# parsing helpers (shared with NL agent + routes)
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


def parse_date(text: str) -> str | None:
    """ISO / DD/MM/YYYY / DD-MM-YYYY / 'tomorrow' / '31 dec' / Banglish."""
    t = text.strip().lower().replace("end of", "")
    if not t:
        return ""
    today = date.today()
    if t in ("today", "ajan", "aj"):
        return today.isoformat()
    if t in ("tomorrow", "agami kal", "kal"):
        return (today + timedelta(days=1)).isoformat()
    if t in ("next week", "7 days", "7d", "week"):
        return (today + timedelta(days=7)).isoformat()
    if t in ("month", "next month", "30 days", "30d"):
        return (today + timedelta(days=30)).isoformat()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", t)
    if m:
        d, mo, y = m.groups()
        y = int(y)
        y = y if y > 1000 else 2000 + y
        try:
            return date(y, int(mo), int(d)).isoformat()
        except ValueError:
            return None
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]{3,9}),?\s*(\d{2,4})?$", t)
    if m:
        d, mo_name, y = m.groups()
        mo = months.get(mo_name[:3])
        if mo:
            y = int(y) if y else (today.year if (int(mo), int(d)) >=
                                  (today.month, today.day) else today.year + 1)
            try:
                return date(y, mo, int(d)).isoformat()
            except ValueError:
                return None
    return None


_FIELD_TAGS = {
    "deadline": ["deadline", "end", "due", "expire", "time", "date"],
    "link":     ["link", "url", "site", "website", "http"],
    "network":  ["network", "chain", "blockchain"],
    "phase":    ["phase", "stage"],
    "value":    ["value", "est", "estimate", "worth"],
    "reward":   ["reward", "reward_type", "prize"],
    "project":  ["project", "token"],
    "category": ["cat", "category", "type"],
    "label":    ["label", "nickname", "name"],
}


def _extract_fields(text: str) -> tuple[dict, str]:
    """Pull `<tag> <value>` pairs out of free text. Returns (fields, remaining)."""
    fields: dict[str, str] = {}
    remaining = text
    tag_alt = "|".join(re.escape(x) for pair in _FIELD_TAGS.values() for x in pair)
    sep = r'[\s:"]+'  # single-quoted: embedded double-quote won't split the literal
    for key, tags in sorted(_FIELD_TAGS.items(), key=lambda kv: -max(len(t) for t in kv[1])):
        for tag in sorted(tags, key=len, reverse=True):
            pat = re.compile(r"\b" + re.escape(tag) + sep + r"(.+?)" +
                             r"(?=\s+(?:" + tag_alt + r")\b|$)", re.IGNORECASE)
            m = pat.search(remaining)
            if not m or key in fields:
                continue
            val = m.group(1).strip().strip('"\'')
            if key == "deadline":
                val = parse_date(val)
                if val is None:
                    continue
            fields[key] = val
            remaining = remaining[:m.start()] + " " + remaining[m.end():]
    remaining = re.sub(r"\s+", " ", remaining).strip()
    return fields, remaining


def _days_left(deadline: str) -> int | None:
    if not deadline:
        return None
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").date()
        return (d - date.today()).days
    except ValueError:
        return None


def _fmt_deadline(deadline: str) -> str:
    d = _days_left(deadline)
    if d is None:
        return ""
    if d == 0:
        return " (TODAY!)"
    if d < 0:
        return f" (overdue by {-d}d)"
    return f" (in {d}d)"


# --------------------------------------------------------------------------
# the plugin
# --------------------------------------------------------------------------
class AirdropPlugin(Plugin):
    slug = "airdrop"
    title = "Airdrops"
    icon = "📦"
    version = "1.0.0"
    description = "Track airdrops, tasks, wallets and deadlines — chat in Banglish."
    order = 10
    SCHEMA = SCHEMA

    # -- storage helpers (all parameterised -> injection-safe) ---------------
    def list_airdrops(self, status: str | None = None) -> list[dict]:
        if status:
            return self.store.fetch(
                "SELECT * FROM airdrops WHERE status = ? ORDER BY created_at DESC",
                (status,))
        return self.store.fetch("SELECT * FROM airdrops ORDER BY created_at DESC")

    def get_airdrop(self, airdrop_id: int) -> dict | None:
        return self.store.fetchone("SELECT * FROM airdrops WHERE id = ?", (airdrop_id,))

    def find_airdrop_by_name(self, name: str) -> dict | None:
        return self.store.fetchone(
            "SELECT * FROM airdrops WHERE lower(name) = lower(?) ORDER BY id", (name,))

    def create_airdrop(self, name, project="", status="active", reward_type="tbd",
                       estimated_value="", network="TBD", link="", deadline="",
                       phase="", notes=""):
        now = _now()
        aid = self.store.insert(
            "airdrops", name=name.strip(), project=project, status=status,
            reward_type=reward_type, estimated_value=estimated_value,
            network=network, link=link, deadline=deadline, phase=phase,
            notes=notes, created_at=now, updated_at=now)
        return self.get_airdrop(aid)

    def update_airdrop(self, airdrop_id: int, **fields) -> dict | None:
        allowed = {"project", "status", "reward_type", "estimated_value",
                   "network", "link", "deadline", "phase", "notes", "name"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k} = ?")
                args.append(str(v).strip() if isinstance(v, str) else v)
        if sets:
            sets.append("updated_at = ?")
            args.append(_now())
            args.append(airdrop_id)
            self.store.exec(f"UPDATE airdrops SET {', '.join(sets)} WHERE id = ?",
                            tuple(args))
        return self.get_airdrop(airdrop_id)

    def delete_airdrop(self, airdrop_id: int) -> None:
        self.store.exec("DELETE FROM airdrops WHERE id = ?", (airdrop_id,))

    def count_airdrops_by_status(self) -> dict[str, int]:
        counts = dict.fromkeys(ALLOWED_STATUS, 0)
        for r in self.store.fetch(
                "SELECT status, COUNT(*) c FROM airdrops GROUP BY status"):
            counts[r["status"]] = r["c"]
        return counts

    def list_tasks(self, airdrop_id: int | None = None, status: str | None = None):
        sql = """SELECT t.*, a.name AS airdrop_name
                 FROM tasks t JOIN airdrops a ON a.id = t.airdrop_id"""
        cond, args = [], []
        if airdrop_id is not None:
            cond.append("t.airdrop_id = ?"); args.append(airdrop_id)
        if status:
            cond.append("t.status = ?"); args.append(status)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        return self.store.fetch(sql + " ORDER BY t.id DESC", tuple(args))

    def add_task(self, airdrop_id: int, title: str, category: str = "other",
                 target_url: str = ""):
        tid = self.store.insert(
            "tasks", airdrop_id=airdrop_id, title=title, category=category,
            status="pending", target_url=target_url, created_at=_now(), done_at="")
        return self.store.fetchone(
            """SELECT t.*, a.name AS airdrop_name
               FROM tasks t JOIN airdrops a ON a.id = t.airdrop_id WHERE t.id = ?""",
            (tid,))

    def find_task(self, airdrop_id: int, title: str) -> dict | None:
        return self.store.fetchone(
            """SELECT t.*, a.name AS airdrop_name
               FROM tasks t JOIN airdrops a ON a.id = t.airdrop_id
               WHERE t.airdrop_id = ? AND lower(t.title) = lower(?)""",
            (airdrop_id, title))

    def set_task_status(self, task_id: int, status: str):
        if status not in ("pending", "done"):
            raise ValueError(f"bad task status: {status}")
        done_at = _now() if status == "done" else ""
        self.store.exec("UPDATE tasks SET status = ?, done_at = ? WHERE id = ?",
                        (status, done_at, task_id))
        return self.store.fetchone(
            """SELECT t.*, a.name AS airdrop_name
               FROM tasks t JOIN airdrops a ON a.id = t.airdrop_id WHERE t.id = ?""",
            (task_id,))

    def delete_task(self, task_id: int) -> None:
        self.store.exec("DELETE FROM tasks WHERE id = ?", (task_id,))

    def count_pending_tasks(self) -> int:
        r = self.store.fetchone(
            "SELECT COUNT(*) c FROM tasks WHERE status = 'pending'")
        return r["c"] if r else 0

    def list_wallets(self):
        return self.store.fetch("SELECT * FROM wallets ORDER BY id")

    def add_wallet(self, address: str, label: str = "", network: str = "TBD",
                   note: str = ""):
        wid = self.store.insert(
            "wallets", label=label, address=address, network=network,
            note=note, created_at=_now())
        return self.store.fetchone("SELECT * FROM wallets WHERE id = ?", (wid,))

    def get_wallet(self, wallet_id: int) -> dict | None:
        return self.store.fetchone("SELECT * FROM wallets WHERE id = ?", (wallet_id,))

    def update_wallet(self, wallet_id: int, **fields) -> dict | None:
        allowed = {"label", "address", "network", "note"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k} = ?")
                args.append(str(v).strip() if isinstance(v, str) else v)
        if sets:
            args.append(wallet_id)
            self.store.exec(f"UPDATE wallets SET {', '.join(sets)} WHERE id = ?",
                            tuple(args))
        return self.get_wallet(wallet_id)

    def delete_wallet(self, wallet_id: int) -> None:
        self.store.exec("DELETE FROM wallets WHERE id = ?", (wallet_id,))

    @staticmethod
    def validate_address(address: str) -> bool:
        """Cheap sanity check (not chain verification): EVM hex (0x), raw TON
        (0:), TRON (41…) and long base58 (SOL, TON UQ…)."""
        addr = address.strip()
        if len(addr) < 16 or len(addr) > 140:
            return False
        if addr.startswith(("0x", "0X", "0:")):
            rest = addr[2:]
            try:
                int(rest, 16)
            except ValueError:
                return False
            return len(rest) >= 12
        base58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        return all(c in base58 for c in addr) and len(addr) >= 32

    def upcoming_deadlines(self, days: int = 14) -> list[dict]:
        today = _today()
        limit = (date.today() + timedelta(days=days)).isoformat()
        return self.store.fetch(
            """SELECT * FROM airdrops
               WHERE deadline >= ? AND deadline <= ? AND status != 'done'
               ORDER BY deadline""", (today, limit))

    def overdue_deadlines(self) -> list[dict]:
        return self.store.fetch(
            """SELECT * FROM airdrops
               WHERE deadline != '' AND deadline < ? AND status != 'done'
               ORDER BY deadline""", (_today(),))

    def task_summary(self) -> dict:
        r = self.store.fetchone(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done,
                      SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending
               FROM tasks""")
        total = r["total"] or 0
        return {"total": total, "done": r["done"] or 0, "pending": r["pending"] or 0}

    def _match_airdrop(self, query: str) -> dict | list | None:
        q = query.strip().lower()
        direct = self.find_airdrop_by_name(query.strip())
        if direct:
            return direct
        rows = [a for a in self.list_airdrops()
                if q in a["name"].lower() or q in a["project"].lower()]
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]
        return rows

    # -- dashboard / plugin hooks -------------------------------------------
    def dashboard(self) -> dict:
        counts = self.count_airdrops_by_status()
        return {
            "airdrops_by_status": counts,
            "total_airdrops": sum(counts.values()),
            "active": counts["active"] + counts["farming"] + counts["new"] + counts["claimable"],
            "deadlines_next_7d": self.upcoming_deadlines(7),
            "overdue": self.overdue_deadlines(),
            "pending_tasks": self.count_pending_tasks(),
            "wallet_count": len(self.list_wallets()),
            "today": _today(),
        }

    def summary(self) -> dict | None:
        d = self.dashboard()
        return {
            "cards": [
                {"k": "📦 Total airdrops", "v": d["total_airdrops"],
                 "s": f"{d['airdrops_by_status']['active']} active"},
                {"k": "🛠️ Active", "v": d["active"],
                 "s": f"new {d['airdrops_by_status']['new']} · farming {d['airdrops_by_status']['farming']}"},
                {"k": "⏰ 7d deadlines", "v": len(d["deadlines_next_7d"]),
                 "s": f"overdue {len(d['overdue'])}"},
                {"k": "✅ Pending tasks", "v": d["pending_tasks"],
                 "s": f"wallets {d['wallet_count']}"},
            ],
            "deadlines": [{"name": a["name"], "deadline": a["deadline"],
                           "status": a["status"]} for a in d["deadlines_next_7d"]],
            "overdue": [a["name"] for a in d["overdue"]],
        }

    def export(self) -> dict | None:
        return {"airdrops": self.list_airdrops(), "tasks": self.list_tasks(),
                "wallets": self.list_wallets()}

    def import_data(self, payload: dict) -> dict | None:
        added_a = added_t = added_w = 0
        for a in payload.get("airdrops", []):
            if not self.find_airdrop_by_name(a.get("name", "")):
                self.create_airdrop(**{k: a.get(k, "") for k in (
                    "name", "project", "status", "reward_type", "estimated_value",
                    "network", "link", "deadline", "phase", "notes")})
                added_a += 1
        for t in payload.get("tasks", []):
            ad = self.get_airdrop(t.get("airdrop_id") or 0)
            if ad and not self.find_task(ad["id"], t.get("title", "")):
                self.add_task(ad["id"], t["title"], t.get("category", "other"),
                              t.get("target_url", ""))
                added_t += 1
        for w in payload.get("wallets", []):
            if not any(x["address"] == w.get("address") for x in self.list_wallets()):
                self.add_wallet(w["address"], w.get("label", ""),
                                w.get("network", "TBD"), w.get("note", ""))
                added_w += 1
        return {"added_airdrops": added_a, "added_tasks": added_t,
                "added_wallets": added_w}

    def help_text(self) -> str:
        return (
            "▪ add airdrop <name> deadline <date> reward <type> value <est> network <chain>\n"
            "▪ delete airdrop <name>\n"
            "▪ list airdrops\n"
            "▪ add task \"<title>\" to <airdrop>\n"
            "▪ mark \"<title>\" in <airdrop> done\n"
            "▪ list tasks\n"
            "▪ add wallet <address> label <nick> network <chain>\n"
            "▪ list wallets\n"
            "▪ deadlines this week\n"
            "▪ progress / summary")

    # -- chat intents --------------------------------------------------------
    def process(self, text: str) -> tuple | None:
        msg = text.strip()
        low = msg.lower()
        if low in ("help", "help me", "ki paro", "commands", "?"):
            return (True, self.help_text() + "\n\n'Dashboard dekhar jonno: progress",
                    "none", {})
        if re.search(r"\b(add|new|register|notun)\b.*airdrop", low):
            return (True, *self._add_airdrop(msg))
        if re.search(r"\b(remove|delete|dur)\b.*\bairdrop", low):
            return (True, *self._delete_airdrop(msg))
        if re.search(r"deadline", low) or low in ("dues", "due"):
            return (True, *self._deadlines(msg))
        if re.search(r"\b(add|new)\b.*\bwallet", low):
            return (True, *self._add_wallet(msg))
        if re.search(r"\b(list|show|dekhau|sob)\b.*\bwallet", low) or low == "wallets":
            return (True, *self._list_wallets())
        if re.search(r"\b(delete|remove)\b.*\bwallet", low):
            return (True, *self._delete_wallet(msg))
        if re.search(r"\b(add|new)\b.*\btask", low):
            return (True, *self._add_task(msg))
        if re.search(r"\b(list|show)\b.*\btask", low) or low in ("tasks", "task list"):
            return (True, self._list_tasks(msg), "airdrop", {}, True)
        if re.search(r"\b(mark|done|complete|finish)\b", low) and re.search(
                r"\b(task|done)\b", low):
            return (True, *self._mark_task_done(msg))
        if re.search(r"\b(unmark|pending|reset)\b.*\btask", low):
            return (True, *self._reset_task(msg))
        if re.search(r"\b(list|show)\b.*\bairdrop", low) or low in ("airdrops", "list"):
            return (True, *self._list_airdrops(msg))
        if low in ("how many", "status", "stats", "count", "dashboard", "overview",
                   "summary", "progress", "report"):
            return (True, self._summary(), "dashboard", {}, True)
        if re.search(r"\b(how many|count|stats|status)\b", low):
            return (True, self._summary(), "dashboard", {}, True)
        if re.search(r"\b(export|backup)\b", low) and re.search(r"airdrop", low):
            return (True, ("Export korte UI te '💾 Backup' tab e 'Export JSON' "
                           "button presh korun."), "backup", {})
        if re.search(r"\b(report|scam|fake|verify|trusted?)\b", low) and (
                re.search(r"https?://", low)
                or re.search(r"\b\w+\.(com|io|net|org|app|xyz)\b", low)):
            from astra.research import quick_lookup
            r = quick_lookup(msg)
            return (True, r.text, "none", r.data)
        return None  # not our domain -> next plugin / LLM / fallback

    # -- airdrops ------------------------------------------------------------
    def _add_airdrop(self, msg: str) -> tuple[str, str, dict, bool]:
        fields, rest = _extract_fields(msg)
        m = re.search(r"(?:add|new|register)(?:\s+airdrop)?\s+([a-zA-Z0-9_ .\-]+)",
                      rest, re.IGNORECASE)
        if not m:
            return ("Name ta bollen na. 🔁 Try: add airdrop <name> deadline <date>",
                    "none", {}, False)
        name = m.group(1).strip()
        if len(name) < 2:
            return ("Name ektu choto lagche. Try: add airdrop <name>", "none", {}, False)
        a = self.create_airdrop(
            name=name, project=fields.get("project", ""),
            status=fields.get("status", "active") if fields.get("status") in ALLOWED_STATUS else "active",
            reward_type=fields.get("reward", "tbd"),
            estimated_value=fields.get("value", ""),
            network=fields.get("network", "TBD"),
            link=fields.get("link", ""),
            deadline=fields.get("deadline", ""),
            phase=fields.get("phase", ""))
        tail = [f"Network: {a['network']}"]
        if a["deadline"]:
            tail.append(f"Deadline: {a['deadline']}{_fmt_deadline(a['deadline'])}")
        if a["estimated_value"]:
            tail.append(f"Est. value: {a['estimated_value']}")
        text = (f"✅ Airdrop '{a['name']}' add hoye geche (id #{a['id']}).\n"
                + "\n".join(f"  • {t}" for t in tail)
                + "\n\nTasks add korar jonno: add task \"<task>\" to " + a["name"])
        return (text, "airdrop", {"airdrop": a}, True)

    def _delete_airdrop(self, msg: str) -> tuple[str, str, dict, bool]:
        m = re.search(r"(?:delete|remove)\s+airdrop\s+(.+)", msg, re.IGNORECASE)
        if not m:
            return ("Kono airdrop name pailam na.", "none", {}, False)
        found = self._match_airdrop(m.group(1))
        if isinstance(found, list):
            return ("Ekadhik airdrop match korche: " + ", ".join(
                f"'{a['name']}' (id {a['id']})" for a in found)
                + ". List e klik kore delete korte parben.", "airdrop", {}, True)
        if not found:
            return (f"'{m.group(1)}' naam er kono airdrop nai.", "none", {}, False)
        self.delete_airdrop(found["id"])
        return (f"🗑️ Airdrop '{found['name']}' delete hoye geche (tasks o shathe).",
                "airdrop", {}, True)

    def _list_airdrops(self, msg: str) -> tuple[str, str, dict, bool]:
        m = re.search(r"status[:\s]*([a-z]+)", msg.lower())
        status = m.group(1) if m else None
        status = status if status in ALLOWED_STATUS else None
        rows = self.list_airdrops(status)
        if not rows:
            return "Kono airdrop nai ei condition e. New add korun!", "airdrop", {}, False
        lines = [f"Airdrops ({len(rows)}):"]
        for a in rows:
            flags = []
            if a["status"] != "done" and a["deadline"]:
                flags.append("⏰" + _fmt_deadline(a["deadline"]))
            if a["status"] == "done":
                flags.append("🏁")
            if a["status"] == "dropped":
                flags.append("✖")
            lines.append("  • [{}] {}{} (id {}){}".format(
                a["status"], a["name"], f" — {a['project']}" if a["project"] else "",
                a["id"], " ".join(flags)))
        return "\n".join(lines), "airdrop", {}, True

    def _summary(self) -> str:
        d = self.dashboard()
        c = d["airdrops_by_status"]
        lines = [
            "📊 **Report**",
            f"  • Total airdrops: {d['total_airdrops']}",
            f"  • Active: {c['active']}  |  New: {c['new']}  |  Farming: {c['farming']}",
            f"  • Done: {c['done']}  |  Dropped: {c['dropped']}  |  Claimable: {c['claimable']}",
            f"  • Pending tasks: {d['pending_tasks']}",
            f"  • Wallets: {d['wallet_count']}",
        ]
        if d["deadlines_next_7d"]:
            lines.append("  • Deadline (7d): " + ", ".join(
                f"{a['name']}{_fmt_deadline(a['deadline'])}"
                for a in d["deadlines_next_7d"]))
        if d["overdue"]:
            lines.append("  • Overdue: " + ", ".join(a["name"] for a in d["overdue"]))
        return "\n".join(lines)

    def _deadlines(self, msg: str) -> tuple[str, str, dict, bool]:
        low = msg.lower()
        days = 14
        if "week" in low or "7" in low:
            days = 7
        elif "month" in low or "30" in low:
            days = 30
        up = self.upcoming_deadlines(days)
        overdue = self.overdue_deadlines()
        if not up and not overdue:
            return (f"Kono upcoming deadline nai agami {days} diner moddhe. 🎉",
                    "dashboard", {}, False)
        lines = [f"⏰ Deadlines ({days} din):"]
        for a in up:
            lines.append("  • {} — {}{}".format(a["name"], a["deadline"],
                                                _fmt_deadline(a["deadline"])))
        if overdue:
            lines.append("Overdue:")
            for a in overdue:
                lines.append("  • {} — {}{}".format(a["name"], a["deadline"],
                                                    _fmt_deadline(a["deadline"])))
        return ("\n".join(lines), "dashboard", {"upcoming": up, "overdue": overdue}, True)

    # -- tasks ----------------------------------------------------------------
    def _add_task(self, msg: str) -> tuple[str, str, dict, bool]:
        fields, rest = _extract_fields(msg)
        m = re.search(r'"([^"]+)"', msg)
        title = m.group(1).strip() if m else None
        if not title:
            t = re.search(r"add\s+task\s+([a-zA-Z0-9_ .\-]+?)\s+(?:to|in)\b", rest, re.IGNORECASE)
            if t:
                title = t.group(1).strip()
        am = (re.search(r"\b(?:to|in)\s+([a-zA-Z0-9_ .\-]+)$", rest, re.IGNORECASE)
              or re.search(r"\b(?:to|in)\s+([a-zA-Z0-9_ .\-]+)", msg, re.IGNORECASE))
        airdrop_query = am.group(1).strip() if am else None
        if not title or not airdrop_query:
            return ('Format: add task "join telegram" to Notcoin', "none", {}, False)
        found = self._match_airdrop(airdrop_query)
        if isinstance(found, list):
            return ("Airdrop ambiguous: " + ", ".join(a["name"] for a in found)
                    + ". Airdrops tab e manually add korun.", "none", {}, False)
        if not found:
            return (f"Airdrop '{airdrop_query}' khuje pelam na. Age add korun.",
                    "none", {}, False)
        cat = fields.get("category", "other")
        task = self.add_task(found["id"], title, cat)
        return (f"✅ Task '{title}' add hoye geche '{found['name']}' e (category: {cat}).",
                "airdrop", {"task": task, "airdrop": found}, True)

    def _mark_task_done(self, msg: str) -> tuple[str, str, dict, bool]:
        target = re.sub(r"\s+(?:done|complete|done\b)", "", msg, flags=re.I)
        m = re.search(r"mark\s+(?:task\s+)?#?(\d+)\s*$", target.lower())
        if m:
            task = self.set_task_status(int(m.group(1)), "done")
            if not task:
                return (f"Task #{m.group(1)} paoa gelo na.", "none", {}, False)
            return (f"🎉 Task '{task['title']}' done! ({task['airdrop_name']})",
                    "airdrop", {"task": task}, True)
        tm = re.search(r'"([^"]+)"', target)
        am = re.search(r"\b(?:in|to)\s+([a-zA-Z0-9_ .\-]+)$", target, re.IGNORECASE)
        if tm and am:
            found = self._match_airdrop(am.group(1))
            if isinstance(found, list) or not found:
                return ("Airdrop match korte parini.", "none", {}, False)
            task = self.find_task(found["id"], tm.group(1))
            if not task:
                return (f"'{tm.group(1)}' task khuje pelam na '{found['name']}' e.",
                        "none", {}, False)
            task = self.set_task_status(task["id"], "done")
            return (f"🎉 Task '{task['title']}' done! ({task['airdrop_name']})",
                    "airdrop", {"task": task}, True)
        return ('Format: mark "join telegram" in Notcoin done', "none", {}, False)

    def _reset_task(self, msg: str) -> tuple[str, str, dict, bool]:
        m = re.search(r'#?(\d+)', msg)
        if m:
            task = self.set_task_status(int(m.group(1)), "pending")
            if task:
                return (f"↩️ Task '{task['title']}' pending e fere geche.",
                        "airdrop", {"task": task}, True)
        return ("Task number den: 'reset task 12'", "none", {}, False)

    def _list_tasks(self, msg: str) -> str:
        m = re.search(r"\b(?:in|for|of)\s+([a-zA-Z0-9_ .\-]+)$", msg, re.IGNORECASE)
        if m:
            found = self._match_airdrop(m.group(1))
            if isinstance(found, dict):
                rows, title = self.list_tasks(found["id"]), f"Tasks of '{found['name']}'"
            elif isinstance(found, list):
                return "Airdrop ambiguous."
            else:
                rows, title = [], ""
        else:
            rows, title = self.list_tasks(), "All tasks"
        if not rows:
            return f"{title}: kono task nai."
        lines = [f"✅ {title} ({len(rows)}):"]
        for t in rows:
            mark = "☑️" if t["status"] == "done" else "⬜"
            lines.append(f"  {mark} #{t['id']} [{t['airdrop_name']}] {t['title']} "
                         f"({t['category']})")
        return "\n".join(lines)

    # -- wallets --------------------------------------------------------------
    def _add_wallet(self, msg: str) -> tuple[str, str, dict, bool]:
        fields, rest = _extract_fields(msg)
        m = re.search(r"wallet\s+([0-9a-zA-Z:]+)\s*", msg)
        if not m:
            return ("Address den: add wallet 0x... label main100 network eth",
                    "none", {}, False)
        addr = m.group(1)
        if not self.validate_address(addr):
            return ("Address ta valid mone hochhe na (hex check fail). Abar likhun.",
                    "none", {}, False)
        w = self.add_wallet(addr, fields.get("label", "wallet_" + str(
            len(self.list_wallets()) + 1)), fields.get("network", "TBD"))
        return (f"👛 Wallet added: {w['label']} ({w['network']})\n  {w['address']}\n"
                "  via UI te note add korte parben.", "airdrop", {"wallet": w}, True)

    def _list_wallets(self) -> tuple[str, str, dict, bool]:
        rows = self.list_wallets()
        if not rows:
            return "Kono wallet nai. add wallet <address> label <nick> 🎒", "airdrop", {}, False
        lines = [f"👛 Wallets ({len(rows)}):"]
        for w in rows:
            lines.append(f"  #{w['id']} {w['label']} [{w['network']}]\n    {w['address']}"
                         + (f"\n    📝 {w['note']}" if w["note"] else ""))
        return "\n".join(lines), "airdrop", {}, True

    def _delete_wallet(self, msg: str) -> tuple[str, str, dict, bool]:
        m = re.search(r"#?(\d+)", msg)
        if not m:
            return ("Wallet id den: delete wallet 3", "none", {}, False)
        wid = int(m.group(1))
        w = self.get_wallet(wid)
        if not w:
            return (f"Wallet #{wid} nai.", "none", {}, False)
        self.delete_wallet(wid)
        return (f"🗑️ Wallet '{w['label']}' delete hoye geche. → wallets", "airdrop", {}, True)

    # -- HTTP routes ------------------------------------------------------------
    def routes(self) -> list[tuple]:
        return [
            ("GET",    ("api", "airdrops"),                  self.r_list_airdrops),
            ("POST",   ("api", "airdrops"),                  self.r_create_airdrop),
            ("GET",    ("api", "airdrops", "<id>"),          self.r_get_airdrop),
            ("PATCH",  ("api", "airdrops", "<id>"),          self.r_update_airdrop),
            ("DELETE", ("api", "airdrops", "<id>"),          self.r_delete_airdrop),
            ("GET",    ("api", "airdrops", "<id>", "tasks"), self.r_airdrop_tasks),
            ("GET",    ("api", "tasks"),                     self.r_list_tasks),
            ("POST",   ("api", "tasks"),                     self.r_create_task),
            ("PATCH",  ("api", "tasks", "<id>"),             self.r_update_task),
            ("DELETE", ("api", "tasks", "<id>"),             self.r_delete_task),
            ("GET",    ("api", "wallets"),                   self.r_list_wallets),
            ("POST",   ("api", "wallets"),                   self.r_create_wallet),
            ("PATCH",  ("api", "wallets", "<id>"),           self.r_update_wallet),
            ("DELETE", ("api", "wallets", "<id>"),           self.r_delete_wallet),
            ("GET",    ("api", "wallet", "validate"),        self.r_validate_wallet),
        ]

    def r_list_airdrops(self, store, params, body, q):
        return 200, {"ok": True, "data": self.list_airdrops(q.get("status"))}

    def r_get_airdrop(self, store, params, body, q):
        a = self.get_airdrop(params["id"])
        return (200, {"ok": True, "data": a}) if a else (404, {"ok": False, "error": "not found"})

    def r_create_airdrop(self, store, params, body, q):
        name = (body.get("name") or "").strip()
        if not name:
            return 400, {"ok": False, "error": "name required"}
        a = self.create_airdrop(
            name=name, project=body.get("project", ""),
            status=body.get("status", "active"),
            reward_type=body.get("reward_type", "tbd"),
            estimated_value=body.get("estimated_value", ""),
            network=body.get("network", "TBD"), link=body.get("link", ""),
            deadline=body.get("deadline", ""), phase=body.get("phase", ""),
            notes=body.get("notes", ""))
        return 201, {"ok": True, "data": a}

    def r_update_airdrop(self, store, params, body, q):
        a = self.update_airdrop(params["id"], **{k: v for k, v in body.items() if k != "id"})
        return (200, {"ok": True, "data": a}) if a else (404, {"ok": False, "error": "not found"})

    def r_delete_airdrop(self, store, params, body, q):
        self.delete_airdrop(params["id"])
        return 200, {"ok": True}

    def r_airdrop_tasks(self, store, params, body, q):
        return 200, {"ok": True, "data": self.list_tasks(params["id"])}

    def r_list_tasks(self, store, params, body, q):
        return 200, {"ok": True, "data": self.list_tasks()}

    def r_create_task(self, store, params, body, q):
        aid, title = body.get("airdrop_id"), (body.get("title") or "").strip()
        if not self.get_airdrop(aid or 0) or not title:
            return 400, {"ok": False, "error": "airdrop_id + title required"}
        t = self.add_task(aid, title, body.get("category", "other"),
                          body.get("target_url", ""))
        return 201, {"ok": True, "data": t}

    def r_update_task(self, store, params, body, q):
        status = body.get("status")
        if status not in ("pending", "done"):
            return 400, {"ok": False, "error": "status must be pending|done"}
        t = self.set_task_status(params["id"], status)
        return (200, {"ok": True, "data": t}) if t else (404, {"ok": False, "error": "not found"})

    def r_delete_task(self, store, params, body, q):
        self.delete_task(params["id"])
        return 200, {"ok": True}

    def r_list_wallets(self, store, params, body, q):
        return 200, {"ok": True, "data": self.list_wallets()}

    def r_create_wallet(self, store, params, body, q):
        addr = (body.get("address") or "").strip()
        if not self.validate_address(addr):
            return 400, {"ok": False, "error": "address format looks invalid"}
        w = self.add_wallet(addr, body.get("label", ""), body.get("network", "TBD"),
                            body.get("note", ""))
        return 201, {"ok": True, "data": w}

    def r_update_wallet(self, store, params, body, q):
        w = self.update_wallet(params["id"], **{k: v for k, v in body.items() if k != "id"})
        return (200, {"ok": True, "data": w}) if w else (404, {"ok": False, "error": "not found"})

    def r_delete_wallet(self, store, params, body, q):
        self.delete_wallet(params["id"])
        return 200, {"ok": True}

    def r_validate_wallet(self, store, params, body, q):
        ok = self.validate_address(q.get("address", ""))
        return 200, {"ok": True, "valid": ok, "data": {"valid": ok}}


# explicit export so run.py's build_registry() can find the plugin class by
# name (not introspection) — future plugins should do the same.
Plugin = AirdropPlugin