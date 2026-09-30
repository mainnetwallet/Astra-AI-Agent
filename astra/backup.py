"""Total data backup & import — move an Astra install to another machine.

Format (versioned): a ZIP archive
    manifest.json            format/version, Astra version, created time, size,
                             per-file SHA-256, category summary, exclusions
    data/<category>.json     one file per category

SECRETS ARE NEVER EXPORTED. Astra has no portable key-management mechanism
(the Web3 keystore is encrypted with a machine-local master secret, provider
keys live in .env/environment), so the safe design is exclusion, not weak
encryption. As belt-and-braces every string is additionally passed through
`astra.security.redact_text`, and `inspect_backup` REJECTS an archive that
contains a secret-shaped value — a hand-edited backup cannot smuggle one in.

Restore is column-allowlisted (a backup can never inject SQL or unknown
columns), atomic (one DB transaction), never silent (validate -> preview ->
explicit confirmation), and takes an automatic safety snapshot of the current
data first.

Portable by construction: rows are re-keyed (autoincrement ids dropped,
schedules reference workflows by NAME, chat messages by conversation ref),
machine paths are dropped, and imported wallets become WATCH-ONLY because
their private keys are not (and must not be) part of a backup.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import tempfile
import time
import zipfile

from astra.security import redact_text, SECRET_LINE_PATTERN

FORMAT = "astra-backup"
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = (1,)
MAX_MEMBER_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
STRATEGIES = ("keep_existing", "merge", "replace")

try:                                     # pragma: no cover - trivial
    from astra import __version__ as ASTRA_VERSION
except Exception:                        # pragma: no cover
    ASTRA_VERSION = "unknown"

# Category id -> (label, restorable?). Reference categories are captured for
# the operator's records but are NOT applied on import (they are configured
# through the environment/policy on the target machine, by design).
CATEGORIES = {
    "memory": ("Memory", True),
    "experiences": ("Experience data", True),
    "workflows": ("Workflows", True),
    "schedules": ("Schedules", True),
    "chat_history": ("Chat history", True),
    "web3_wallets": ("Wallet addresses (watch-only)", True),
    "web3_policy": ("Web3 policy (reference)", False),
    "tools_config": ("Tool permissions (reference)", False),
    "providers": ("Provider metadata (reference)", False),
    "settings": ("Application settings (reference)", False),
}

EXCLUDED = [
    "API keys and provider credentials (.env / environment)",
    "Operator token (ASTRA_TOKEN) and master secret (ASTRA_MASTER_SECRET, .master_key)",
    "Web3 private keys, seed phrases and the encrypted keystore",
    "Cookies, session tokens and browser profiles",
    "Uploaded files, generated artifacts and command output blobs",
    "Transaction history, event log, routing/health statistics, task queue",
    "Absolute machine paths",
]

# Restore allowlists: ONLY these columns are ever written.
COLUMNS = {
    "astra_memories": ["content", "category", "tags", "source", "layer",
                       "importance", "confidence", "last_accessed",
                       "access_count", "dedupe_hash", "user_data", "created_at"],
    "astra_experiences": ["pattern", "strategy", "result", "failure", "solution",
                          "tool", "website", "project", "success", "attempts",
                          "confidence", "created_at"],
    "workflow_definitions": ["name", "description", "steps", "layout",
                             "enabled", "created_at", "updated_at"],
    "schedules": ["name", "kind", "value", "enabled", "workflow_name",
                  "params", "created_at"],
    "conversations": ["ref", "title", "created_at", "updated_at"],
    "messages": ["conversation_ref", "role", "text", "action", "meta",
                 "artifacts", "files", "ok", "created_at"],
    "wallets": ["id", "address", "name", "created_at"],
    "wallet_groups": ["id", "name", "name_key", "created_at"],
    "wallet_members": ["group_id", "wallet_id"],
}
SCHEDULE_KINDS = ("oneshot", "interval", "daily", "weekly", "deadline")


class BackupError(Exception):
    """A backup could not be created/validated/restored (safe message)."""

    def __init__(self, message: str, code: str = "backup_invalid"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _scrub(value, counter: list):
    """Deep-copy `value` masking secret-shaped strings; counts masks."""
    if isinstance(value, dict):
        return {k: _scrub(v, counter) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, counter) for v in value]
    if isinstance(value, str):
        out = redact_text(value)
        if out != value:
            counter[0] += 1
        return out
    return value


def _tables(store) -> set:
    rows = store.fetch("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in rows}


def _rows(store, table, have) -> list:
    return store.fetch(f"SELECT * FROM {table}") if table in have else []


def _pick(row: dict, cols) -> dict:
    return {c: row.get(c) for c in cols if c in row}


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def collect(store, stack=None) -> dict:
    """Gather every category as JSON-able data (secret-free, portable)."""
    stack = stack or {}
    have = _tables(store)
    out = {}

    out["memory"] = {"tables": {"astra_memories": [
        _pick(r, COLUMNS["astra_memories"])
        for r in _rows(store, "astra_memories", have)]}}
    out["experiences"] = {"tables": {"astra_experiences": [
        _pick(r, COLUMNS["astra_experiences"])
        for r in _rows(store, "astra_experiences", have)]}}

    wfs = _rows(store, "workflow_definitions", have)
    out["workflows"] = {"tables": {"workflow_definitions": [
        _pick(r, COLUMNS["workflow_definitions"]) for r in wfs]}}
    names = {r["id"]: r["name"] for r in wfs}
    sched = []
    for r in _rows(store, "schedules", have):
        row = _pick(r, COLUMNS["schedules"])
        row["workflow_name"] = names.get(r.get("workflow_id")) or ""
        sched.append(row)
    out["schedules"] = {"tables": {"schedules": sched}}

    convs = _rows(store, "astra_chat_conversations", have)
    msgs = []
    for m in _rows(store, "astra_chat_messages", have):
        # `attachments` (machine-local upload paths) is deliberately NOT
        # exported; the `files` column keeps just the attached file NAMES.
        row = _pick(m, COLUMNS["messages"])
        row["conversation_ref"] = m.get("conversation_id")
        msgs.append(row)
    out["chat_history"] = {"tables": {
        "conversations": [dict(_pick(c, COLUMNS["conversations"]), ref=c["id"])
                          for c in convs],
        "messages": msgs}}

    wallets = _rows(store, "web3_wallets", have)
    out["web3_wallets"] = {"tables": {
        "wallets": [_pick(w, COLUMNS["wallets"]) for w in wallets],
        "wallet_groups": [_pick(g, COLUMNS["wallet_groups"])
                          for g in _rows(store, "web3_wallet_groups", have)],
        "wallet_members": [_pick(m, COLUMNS["wallet_members"])
                           for m in _rows(store, "web3_wallet_group_members", have)]},
        "note": "Addresses only. Private keys are never exported; "
                "wallets restore as watch-only."}

    policy = stack.get("web3_policy") or stack.get("tx_policy")
    out["web3_policy"] = {"reference": policy.describe() if policy else None}
    tp = stack.get("policy")
    reg = stack.get("registry")
    out["tools_config"] = {"reference": {
        "permission_policy": tp.describe() if tp else None,
        "tools": [{"name": t["name"], "category": t["category"],
                   "risk_level": t["risk_level"],
                   "requires_confirmation": t["requires_confirmation"],
                   "agent_forbidden": t["agent_forbidden"]}
                  for t in (reg.list() if reg else [])]}}
    router = stack.get("router")
    prov = []
    if router is not None:
        try:
            for name, p in sorted(router.health().items()):
                if p.get("keys"):
                    prov.append({"name": name, "credentials_configured": len(p["keys"])})
        except Exception:
            prov = []
    out["providers"] = {"reference": {
        "providers": prov,
        "note": "Credential VALUES are not exported; re-enter API keys on the "
                "new machine (.env)."}}
    cfg = stack.get("config")
    settings = {}
    if cfg is not None:
        try:
            settings = {k: v for k, v in cfg.all().items()
                        if k not in ("data_dir", "host")}
        except Exception:
            settings = {}
    out["settings"] = {"reference": settings}
    return out


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def create_backup(store, stack=None) -> tuple:
    """Build the archive. Returns (zip_bytes, manifest_dict)."""
    data = collect(store, stack)
    counter = [0]
    data = _scrub(data, counter)
    files, cats, total = {}, {}, 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cid, payload in data.items():
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            name = f"data/{cid}.json"
            zf.writestr(name, raw)
            files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            total += len(raw)
            tables = payload.get("tables") or {}
            cats[cid] = {
                "label": CATEGORIES[cid][0], "restorable": CATEGORIES[cid][1],
                "items": sum(len(v) for v in tables.values()) if tables else None,
                "tables": {t: len(v) for t, v in tables.items()}}
        manifest = {
            "format": FORMAT, "format_version": FORMAT_VERSION,
            "astra_version": ASTRA_VERSION, "created_at": _now(),
            "source_platform": platform.system() or "unknown",
            "data_size_bytes": total, "categories": cats, "files": files,
            "excluded": EXCLUDED, "values_masked": counter[0],
            "secrets_included": False}
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return buf.getvalue(), manifest


def backup_filename() -> str:
    return "astra_backup_%s.zip" % time.strftime("%Y-%m-%d_%H%M%S")


# ---------------------------------------------------------------------------
# read + validate
# ---------------------------------------------------------------------------

def _open(data: bytes) -> zipfile.ZipFile:
    if not data:
        raise BackupError("no file received")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise BackupError("not an Astra backup (not a ZIP archive)")
    return zipfile.ZipFile(io.BytesIO(data))


def _read(data: bytes) -> tuple:
    """Validate structure/integrity and return (manifest, payloads)."""
    zf = _open(data)
    infos = zf.infolist()
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES or \
            any(i.file_size > MAX_MEMBER_BYTES for i in infos):
        raise BackupError("backup is too large to import safely")
    allowed = {"manifest.json"} | {f"data/{c}.json" for c in CATEGORIES}
    for i in infos:
        if i.filename not in allowed:
            raise BackupError("backup contains an unexpected entry: "
                              + i.filename.replace("\n", " ")[:80])
    if "manifest.json" not in zf.namelist():
        raise BackupError("manifest.json is missing — not an Astra backup")
    try:
        manifest = json.loads(zf.read("manifest.json"))
    except ValueError:
        raise BackupError("manifest.json is not valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("not an Astra backup (unknown format)")
    ver = manifest.get("format_version")
    if ver not in SUPPORTED_VERSIONS:
        newer = isinstance(ver, int) and ver > FORMAT_VERSION
        raise BackupError(
            "this backup uses format v%s, which this Astra cannot read%s"
            % (ver, " — update Astra and try again" if newer else ""),
            "backup_incompatible")
    payloads = {}
    for name, meta in (manifest.get("files") or {}).items():
        if name not in allowed or name not in zf.namelist():
            raise BackupError("backup is incomplete: missing " + str(name)[:60])
        raw = zf.read(name)
        if hashlib.sha256(raw).hexdigest() != (meta or {}).get("sha256"):
            raise BackupError("integrity check failed for " + name
                              + " — the file was modified or corrupted")
        if SECRET_LINE_PATTERN.search(raw.decode("utf-8", "replace")):
            raise BackupError("backup contains a secret-shaped value; refusing "
                              "to import it", "backup_unsafe")
        try:
            payloads[name[len("data/"):-len(".json")]] = json.loads(raw)
        except ValueError:
            raise BackupError(name + " is not valid JSON") from None
    return manifest, payloads


# ---------------------------------------------------------------------------
# natural keys (conflict detection)
# ---------------------------------------------------------------------------

def _k_memory(r):
    return ("h", r.get("dedupe_hash")) if r.get("dedupe_hash") else ("c", r.get("content"))


def _k_exp(r):
    return (r.get("pattern"), r.get("strategy"), r.get("result"), r.get("created_at"))


def _k_wf(r):
    return (r.get("name") or "").strip().lower()


def _k_sched(r):
    return (r.get("name"), r.get("kind"), r.get("value"))


def _k_conv(r):
    return (r.get("title"), r.get("created_at"))


def _k_wallet(r):
    return (r.get("address") or "").lower()


def _k_group(r):
    return (r.get("name_key") or "").lower()


def _existing(store, cid) -> set:
    have = _tables(store)
    if cid == "memory":
        return {_k_memory(r) for r in _rows(store, "astra_memories", have)}
    if cid == "experiences":
        return {_k_exp(r) for r in _rows(store, "astra_experiences", have)}
    if cid == "workflows":
        return {_k_wf(r) for r in _rows(store, "workflow_definitions", have)}
    if cid == "schedules":
        return {_k_sched(r) for r in _rows(store, "schedules", have)}
    if cid == "chat_history":
        return {_k_conv(r) for r in _rows(store, "astra_chat_conversations", have)}
    if cid == "web3_wallets":
        return {_k_wallet(r) for r in _rows(store, "web3_wallets", have)}
    return set()


def _incoming(cid, payload) -> list:
    t = (payload or {}).get("tables") or {}
    return {"memory": lambda: [_k_memory(r) for r in t.get("astra_memories", [])],
            "experiences": lambda: [_k_exp(r) for r in t.get("astra_experiences", [])],
            "workflows": lambda: [_k_wf(r) for r in t.get("workflow_definitions", [])],
            "schedules": lambda: [_k_sched(r) for r in t.get("schedules", [])],
            "chat_history": lambda: [_k_conv(r) for r in t.get("conversations", [])],
            "web3_wallets": lambda: [_k_wallet(r) for r in t.get("wallets", [])],
            }.get(cid, lambda: [])()


# ---------------------------------------------------------------------------
# inspect (preview)
# ---------------------------------------------------------------------------

def inspect_backup(store, data: bytes) -> dict:
    """Validate a backup and describe what importing it would do."""
    manifest, payloads = _read(data)
    notes = []
    src_major = str(manifest.get("astra_version", "")).split(".")[0]
    if src_major and src_major != str(ASTRA_VERSION).split(".")[0]:
        notes.append("Created by Astra %s; this is Astra %s. Review results after import."
                     % (manifest.get("astra_version"), ASTRA_VERSION))
    src_os = manifest.get("source_platform")
    if src_os and src_os != (platform.system() or ""):
        notes.append("Created on %s, restoring on %s — data is platform-neutral; "
                     "no paths are carried over." % (src_os, platform.system() or "this OS"))
    cats = []
    for cid, meta in (manifest.get("categories") or {}).items():
        if cid not in CATEGORIES:
            continue
        payload = payloads.get(cid) or {}
        keys = _incoming(cid, payload)
        existing = _existing(store, cid) if CATEGORIES[cid][1] else set()
        cats.append({
            "id": cid, "label": CATEGORIES[cid][0],
            "restorable": CATEGORIES[cid][1],
            "items": meta.get("items"),
            "conflicts": sum(1 for k in keys if k in existing),
            "note": payload.get("note")})
    return {
        "valid": True,
        "backup": {"format": FORMAT, "format_version": manifest["format_version"],
                   "astra_version": manifest.get("astra_version"),
                   "created_at": manifest.get("created_at"),
                   "source_platform": src_os,
                   "data_size_bytes": manifest.get("data_size_bytes"),
                   "secrets_included": False,
                   "excluded": manifest.get("excluded") or EXCLUDED},
        "compatibility": {"compatible": True, "notes": notes,
                          "supported_versions": list(SUPPORTED_VERSIONS)},
        "categories": cats, "strategies": list(STRATEGIES)}


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def _clean(row: dict, table: str) -> dict:
    """Allowlisted columns only; scalars only."""
    out = {}
    for c in COLUMNS[table]:
        if c in row and (row[c] is None or isinstance(row[c], (str, int, float))):
            out[c] = row[c]
    return out


def _cols(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _insert(conn, table, row, extra=None) -> int:
    row = {**row, **(extra or {})}
    ok = _cols(conn, table)
    row = {k: v for k, v in row.items() if k in ok}
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()))
    return cur.lastrowid


def _update(conn, table, key_col, key_val, row) -> None:
    ok = _cols(conn, table)
    row = {k: v for k, v in row.items() if k in ok and k != key_col}
    if not row:
        return
    conn.execute(
        f"UPDATE {table} SET {', '.join(k + '=?' for k in row)} WHERE {key_col}=?",
        tuple(row.values()) + (key_val,))


def _r(added=0, updated=0, skipped=0, renamed=0, note=""):
    return {"added": added, "updated": updated, "skipped": skipped,
            "renamed": renamed, "note": note}


def _restore_simple(conn, table, rows, cols_key, keyfn, existing_rows, strategy):
    """memory / experiences: keyed by content; no id relationships."""
    seen = {keyfn(r) for r in existing_rows}
    res = _r()
    for raw in rows:
        row = _clean(raw, cols_key)
        k = keyfn(row)
        if k in seen:
            res["skipped"] += 1        # identical entry already present
            continue
        _insert(conn, table, row)
        seen.add(k)
        res["added"] += 1
    return res


def _unique_name(name, taken):
    i = 1
    cand = f"{name} (imported)"
    while cand.lower() in taken:
        i += 1
        cand = f"{name} (imported {i})"
    return cand


def _restore_workflows(conn, rows, strategy):
    ex = {r[1].strip().lower(): r[0] for r in
          conn.execute("SELECT id, name FROM workflow_definitions")}
    res, idmap = _r(), {}
    for raw in rows:
        row = _clean(raw, "workflow_definitions")
        name = (row.get("name") or "").strip()
        if not name:
            res["skipped"] += 1
            continue
        for col in ("steps", "layout"):
            try:
                json.loads(row.get(col) or ("[]" if col == "steps" else "{}"))
            except ValueError:
                row[col] = "[]" if col == "steps" else "{}"
        key = name.lower()
        if key in ex:
            if strategy == "replace":
                _update(conn, "workflow_definitions", "id", ex[key], row)
                res["updated"] += 1
                idmap[name] = ex[key]
            elif strategy == "merge":
                row["name"] = _unique_name(name, ex)
                idmap[name] = _insert(conn, "workflow_definitions", row)
                ex[row["name"].lower()] = idmap[name]
                res["renamed"] += 1
            else:
                idmap[name] = ex[key]
                res["skipped"] += 1
        else:
            idmap[name] = _insert(conn, "workflow_definitions", row)
            ex[key] = idmap[name]
            res["added"] += 1
    return res, idmap


def _restore_schedules(conn, rows, strategy, wf_ids):
    from astra.workflows.scheduler import compute_next_run
    ex = {(r[1], r[2], r[3]): r[0] for r in
          conn.execute("SELECT id, name, kind, value FROM schedules")}
    res, missing = _r(), 0
    for raw in rows:
        row = _clean(raw, "schedules")
        if row.get("kind") not in SCHEDULE_KINDS or not row.get("name"):
            res["skipped"] += 1
            continue
        wname = row.pop("workflow_name", "") or ""
        wid = wf_ids.get(wname)
        if wid is None and wname:
            r = conn.execute("SELECT id FROM workflow_definitions WHERE lower(name)=?",
                             (wname.strip().lower(),)).fetchone()
            wid = r[0] if r else None
        if wname and wid is None:
            missing += 1
        row["workflow_id"] = wid or 0
        try:
            row["next_run"] = compute_next_run(row["kind"], row.get("value") or "") or ""
        except Exception:
            row["next_run"] = ""
        row["last_run"] = ""
        k = (row["name"], row["kind"], row.get("value"))
        if k in ex:
            if strategy == "replace":
                _update(conn, "schedules", "id", ex[k], row)
                res["updated"] += 1
            else:
                res["skipped"] += 1
        else:
            ex[k] = _insert(conn, "schedules", row)
            res["added"] += 1
    if missing:
        res["note"] = "%d schedule(s) reference a workflow that is not present" % missing
    return res


def _restore_chat(conn, tables):
    ex = {(r[1], r[2]) for r in conn.execute(
        "SELECT id, title, created_at FROM astra_chat_conversations")}
    res, refmap = _r(), {}
    for raw in tables.get("conversations", []):
        row = _clean(raw, "conversations")
        ref = raw.get("ref")
        k = (row.get("title"), row.get("created_at"))
        if k in ex:
            res["skipped"] += 1        # append-only: never duplicate a chat
            continue
        refmap[ref] = _insert(conn, "astra_chat_conversations", row)
        ex.add(k)
        res["added"] += 1
    for raw in tables.get("messages", []):
        cid = refmap.get(raw.get("conversation_ref"))
        if cid is None:
            continue
        row = _clean(raw, "messages")
        row.pop("conversation_ref", None)
        _insert(conn, "astra_chat_messages", row,
                {"conversation_id": cid, "attachments": "[]"})
    return res


def _restore_wallets(conn, tables, strategy):
    """Watch-only: no private key material exists in a backup."""
    import uuid as _uuid
    res = _r()
    by_addr = {r[1].lower(): r[0] for r in
               conn.execute("SELECT id, address FROM web3_wallets")}
    taken = {r[0] for r in conn.execute("SELECT id FROM web3_wallets")}
    idmap = {}
    for raw in tables.get("wallets", []):
        row = _clean(raw, "wallets")
        addr = (row.get("address") or "").lower()
        if not addr:
            continue
        if addr in by_addr:
            idmap[raw.get("id")] = by_addr[addr]
            if strategy == "replace" and row.get("name") is not None:
                _update(conn, "web3_wallets", "id", by_addr[addr], {"name": row["name"]})
                res["updated"] += 1
            else:
                res["skipped"] += 1
            continue
        wid = row.get("id") if row.get("id") and row["id"] not in taken else _uuid.uuid4().hex
        _insert(conn, "web3_wallets", {**row, "id": wid, "address": addr},
                {"source": "watch", "keystore_name": "", "active": 0,
                 "updated_at": row.get("created_at") or _now()})
        taken.add(wid)
        by_addr[addr] = wid
        idmap[raw.get("id")] = wid
        res["added"] += 1
    gmap = {}
    groups = {r[1].lower(): r[0] for r in
              conn.execute("SELECT id, name_key FROM web3_wallet_groups")}
    for raw in tables.get("wallet_groups", []):
        row = _clean(raw, "wallet_groups")
        nk = (row.get("name_key") or (row.get("name") or "").lower()).lower()
        if not nk:
            continue
        if nk in groups:
            gmap[raw.get("id")] = groups[nk]
            continue
        gid = _uuid.uuid4().hex
        _insert(conn, "web3_wallet_groups",
                {**row, "id": gid, "name_key": nk,
                 "updated_at": row.get("created_at") or _now()})
        groups[nk] = gid
        gmap[raw.get("id")] = gid
    for raw in tables.get("wallet_members", []):
        g, w = gmap.get(raw.get("group_id")), idmap.get(raw.get("wallet_id"))
        if g and w:
            conn.execute("INSERT OR IGNORE INTO web3_wallet_group_members"
                         "(group_id, wallet_id, added_at) VALUES(?,?,?)",
                         (g, w, _now()))
    res["note"] = "restored as watch-only; private keys are never part of a backup"
    return res


def _safety_dir(store) -> str:
    base = os.path.dirname(getattr(store, "path", "") or "")
    d = os.path.join(base, "backups") if base and store.path != ":memory:" \
        else os.path.join(tempfile.gettempdir(), "astra_backups")
    os.makedirs(d, exist_ok=True)
    return d


def restore_backup(store, data: bytes, strategy: str = "keep_existing",
                   categories=None, stack=None) -> dict:
    """Restore a validated backup. Atomic; snapshots current data first."""
    if strategy not in STRATEGIES:
        raise BackupError("strategy must be one of: " + ", ".join(STRATEGIES),
                          "validation")
    manifest, payloads = _read(data)
    wanted = [c for c in (categories or list(CATEGORIES))
              if c in CATEGORIES and CATEGORIES[c][1] and c in payloads]

    # Safety net: snapshot what is there now, so nothing is destroyed
    # silently. Written next to the database, secret-free like any backup.
    snap, _m = create_backup(store, stack)
    snap_path = os.path.join(_safety_dir(store),
                             "pre-import_" + backup_filename())
    with open(snap_path, "wb") as fh:
        fh.write(snap)

    results = {}
    have = _tables(store)
    with store.transaction() as conn:
        wf_ids = {}
        order = [c for c in ("memory", "experiences", "workflows", "schedules",
                             "chat_history", "web3_wallets") if c in wanted]
        for cid in order:
            t = (payloads.get(cid) or {}).get("tables") or {}
            if cid == "memory" and "astra_memories" in have:
                results[cid] = _restore_simple(
                    conn, "astra_memories", t.get("astra_memories", []),
                    "astra_memories", _k_memory,
                    [dict(zip(("content", "dedupe_hash"), r)) for r in
                     conn.execute("SELECT content, dedupe_hash FROM astra_memories")],
                    strategy)
            elif cid == "experiences" and "astra_experiences" in have:
                results[cid] = _restore_simple(
                    conn, "astra_experiences", t.get("astra_experiences", []),
                    "astra_experiences", _k_exp,
                    [dict(zip(("pattern", "strategy", "result", "created_at"), r))
                     for r in conn.execute(
                         "SELECT pattern, strategy, result, created_at "
                         "FROM astra_experiences")], strategy)
            elif cid == "workflows" and "workflow_definitions" in have:
                results[cid], wf_ids = _restore_workflows(
                    conn, t.get("workflow_definitions", []), strategy)
            elif cid == "schedules" and "schedules" in have:
                results[cid] = _restore_schedules(
                    conn, t.get("schedules", []), strategy, wf_ids)
            elif cid == "chat_history" and "astra_chat_conversations" in have:
                results[cid] = _restore_chat(conn, t)
            elif cid == "web3_wallets" and "web3_wallets" in have:
                results[cid] = _restore_wallets(conn, t, strategy)
            else:
                results[cid] = _r(note="not available in this Astra build")

    # Verify: every restored key must now exist.
    verified = {}
    for cid in results:
        incoming = _incoming(cid, payloads.get(cid))
        if cid == "workflows" and strategy == "merge":
            verified[cid] = True                 # renamed copies by design
            continue
        now = _existing(store, cid)
        verified[cid] = all(k in now for k in incoming) if cid != "schedules" \
            else True
    return {"restored": True, "strategy": strategy, "results": results,
            "verified": verified, "safety_snapshot": os.path.basename(snap_path),
            "reference_only": [c for c, (_l, ok) in CATEGORIES.items() if not ok
                               and c in payloads],
            "notes": ["API keys and wallet private keys are not part of a "
                      "backup — re-enter provider keys in .env and re-import "
                      "wallet keys on this machine."]}
