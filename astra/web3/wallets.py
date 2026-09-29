"""Web3 wallet registry — the ONE canonical list of wallets Astra knows about.

What lives where
----------------
* Wallet *metadata* (id, address, name, source, timestamps, active flag) and
  *groups* live in SQLite (`web3_wallets`, `web3_wallet_groups`,
  `web3_wallet_group_members`). Nothing secret is stored in these tables.
* Wallet *secrets* (private keys) live ONLY in the existing encrypted
  `SecureKeyStore` (`web3_keys`), under the record name == the wallet's
  lowercase address. That is also exactly the name the Transaction Manager
  decrypts with (`from_address`), so signing needs no special path: the
  registry resolves a wallet, the policy engine evaluates the send, and the
  manager decrypts the key transiently at signing time.

Group model
-----------
Many-to-many: a wallet may belong to any number of groups (or none). "Move"
is an atomic remove-from-A + add-to-B. Deleting a group never deletes
wallets. Group names are unique, case-insensitively.

Security invariants (asserted in tests/test_web3_wallets.py)
------------------------------------------------------------
* Secrets are never returned by list/get/describe/resolve — the only
  function that ever hands back a private key is `create_wallet()`, exactly
  once, to the operator's create-wallet response.
* Validation results never echo any part of the submitted secret; invalid
  lines are identified by line number + reason only.
* Nothing here logs, emits events for, or raises errors containing secrets.
* Seed phrases and Web3-Secret-Storage JSON keystores are refused: the
  runtime's seed derivation is not BIP-44, so a phrase would silently map to
  a *different* address than the user's other wallets, and V3 keystores need
  AES-CTR (no crypto dependency exists in this project).
"""
from __future__ import annotations

import os
import re
import stat
import uuid
from datetime import datetime

from astra.core.exceptions import AstraError
from .keystore import SecureKeyStore, DEFAULT_MASTER_KEY_FILE
from .signer import N, generate_private_key, private_to_address

MAX_IMPORT_LINES = 100
MAX_IMPORT_CHARS = 100_000
MAX_NAME_LEN = 40
MAX_GROUP_NAME_LEN = 40

_HEX64 = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SPLIT = re.compile(r"[\s,;:=]+")

SOURCES = ("imported", "created", "watch")

SCHEMA = """
CREATE TABLE IF NOT EXISTS web3_wallets (
    id            TEXT PRIMARY KEY,
    address       TEXT UNIQUE NOT NULL,     -- lowercase 0x + 40 hex
    name          TEXT DEFAULT '',
    source        TEXT DEFAULT 'imported',  -- imported | created | watch
    keystore_name TEXT DEFAULT '',          -- '' for watch-only (no key)
    active        INTEGER DEFAULT 0,        -- at most one row is 1
    created_at    TEXT DEFAULT '',
    updated_at    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS web3_wallet_groups (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    name_key   TEXT UNIQUE NOT NULL,        -- lowercase, for uniqueness
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS web3_wallet_group_members (
    group_id TEXT NOT NULL,
    wallet_id TEXT NOT NULL,
    added_at TEXT DEFAULT '',
    PRIMARY KEY (group_id, wallet_id)
);
"""


class WalletError(AstraError):
    """A wallet-registry request could not be honoured (validation/state)."""

    code = "wallet_error"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_address(addr: str) -> str:
    return (addr or "").strip().lower()


def ensure_master_key_file(path: str = DEFAULT_MASTER_KEY_FILE) -> str:
    """Return the operator master secret, provisioning a random one on first
    use. The file is created owner-read/write only (0600) and lives under the
    git-ignored `data/` directory. An existing file is never overwritten."""
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    secret = os.urandom(32).hex()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    return secret


class WalletRegistry:
    """Canonical wallet + group registry. Thread-safety comes from the
    Store's RLock; multi-statement operations run under `self.store._lock`."""

    def __init__(self, store, keystore: SecureKeyStore | None = None,
                 master_key_path: str = DEFAULT_MASTER_KEY_FILE,
                 on_keystore=None):
        self.store = store
        self._keystore = keystore
        self._master_key_path = master_key_path
        # called with the keystore whenever one is (lazily) provisioned, so
        # the Transaction Manager can be handed the same instance.
        self._on_keystore = on_keystore
        store.install(SCHEMA)

    # ── keystore access ──────────────────────────────────────────────────
    def _ks(self, provision: bool = False) -> SecureKeyStore | None:
        if self._keystore is None and provision:
            secret = ensure_master_key_file(self._master_key_path)
            self._keystore = SecureKeyStore(self.store, secret)
            if self._on_keystore:
                self._on_keystore(self._keystore)
        return self._keystore

    # ── read helpers ─────────────────────────────────────────────────────
    def _group_ids_for(self, wallet_id: str) -> list[str]:
        rows = self.store.fetch(
            "SELECT group_id FROM web3_wallet_group_members "
            "WHERE wallet_id=? ORDER BY rowid", (wallet_id,))
        return [r["group_id"] for r in rows]

    def _public(self, row: dict) -> dict:
        """Safe wallet view. There is deliberately no path from a DB row to a
        secret here: the keystore name is not even included."""
        return {
            "id": row["id"], "address": row["address"], "name": row["name"],
            "source": row["source"], "can_sign": row["source"] != "watch",
            "active": bool(row["active"]),
            "group_ids": self._group_ids_for(row["id"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def _row(self, ref: str) -> dict | None:
        """Look a wallet up by id or (case-insensitive) address."""
        ref = (ref or "").strip()
        if not ref:
            return None
        return self.store.fetchone(
            "SELECT * FROM web3_wallets WHERE id=? OR address=?",
            (ref, ref.lower()))

    def list_wallets(self, group_id: str | None = None) -> list[dict]:
        if group_id:
            rows = self.store.fetch(
                "SELECT w.* FROM web3_wallets w JOIN web3_wallet_group_members m "
                "ON m.wallet_id = w.id WHERE m.group_id=? "
                "ORDER BY w.rowid", (group_id,))
        else:
            rows = self.store.fetch(
                "SELECT * FROM web3_wallets ORDER BY rowid")
        return [self._public(r) for r in rows]

    def get(self, ref: str) -> dict | None:
        row = self._row(ref)
        return self._public(row) if row else None

    def active(self) -> dict | None:
        row = self.store.fetchone(
            "SELECT * FROM web3_wallets WHERE active=1 LIMIT 1")
        return self._public(row) if row else None

    def count(self) -> int:
        return int(self.store.fetchone(
            "SELECT COUNT(*) c FROM web3_wallets")["c"])

    def describe(self) -> dict:
        """Everything the Web3 Center needs, in one call (safe fields only)."""
        act = self.active()
        return {"wallets": self.list_wallets(), "groups": self.list_groups(),
                "active_id": act["id"] if act else None,
                "active_address": act["address"] if act else None}

    # ── selection ────────────────────────────────────────────────────────
    def set_active(self, ref: str) -> dict:
        with self.store._lock:
            row = self._row(ref)
            if row is None:
                raise WalletError("wallet not found")
            self.store.exec("UPDATE web3_wallets SET active=0 WHERE active=1")
            self.store.exec(
                "UPDATE web3_wallets SET active=1, updated_at=? WHERE id=?",
                (_now(), row["id"]))
            return self.get(row["id"])

    def _ensure_active(self) -> None:
        if self.active() is None:
            first = self.store.fetchone(
                "SELECT id FROM web3_wallets ORDER BY rowid LIMIT 1")
            if first:
                self.set_active(first["id"])

    # ── import ───────────────────────────────────────────────────────────
    @staticmethod
    def _parse_line(line: str) -> dict:
        """Classify one input line. The returned dict never contains any
        substring of the secret except the derived public address."""
        tokens = [t for t in _SPLIT.split(line) if t]
        key_tok = next((t for t in tokens if _HEX64.match(t)), None)
        if key_tok is not None:
            name = " ".join(t for t in tokens if t is not key_tok)[:MAX_NAME_LEN]
            try:
                priv = int(key_tok[2:] if key_tok[:2].lower() == "0x"
                           else key_tok, 16)
                if not 1 <= priv < N:
                    raise ValueError
                address = private_to_address(priv)
            except Exception:
                return {"ok": False, "reason":
                        "not a valid secp256k1 private key"}
            return {"ok": True, "kind": "key", "address": address,
                    "name": name, "_priv": priv}
        addr_tok = next((t for t in tokens if _ADDR.match(t)), None)
        if addr_tok is not None:
            name = " ".join(t for t in tokens if t is not addr_tok)[:MAX_NAME_LEN]
            return {"ok": True, "kind": "watch",
                    "address": addr_tok.lower(), "name": name}
        if line.lstrip().startswith(("{", "[")):
            return {"ok": False, "reason":
                    "JSON keystore import is not supported "
                    "(needs AES; paste the private key instead)"}
        if len(tokens) in (12, 15, 18, 21, 24) and all(
                t.isalpha() for t in tokens):
            return {"ok": False, "reason":
                    "seed phrases are not supported (non-standard "
                    "derivation would give a different address) — "
                    "import the private key instead"}
        return {"ok": False, "reason":
                "expected a 64-hex-character private key or a 0x address"}

    def _count_new(self, lines) -> int:
        """How many of these lines would import as NEW wallets (valid,
        not a duplicate of the batch or of the registry)."""
        seen, n = set(), 0
        for _, ln in lines:
            p = self._parse_line(ln)
            if not p["ok"]:
                continue
            if p["address"] in seen or self._row(p["address"]) is not None:
                continue
            seen.add(p["address"])
            n += 1
        return n

    def import_wallets(self, text: str, group_id: str | None = None,
                       dry_run: bool = False,
                       group_name: str | None = None) -> dict:
        """Validate and (unless dry_run) import one wallet per line.

        Line formats (blank lines and `#` comments are ignored):
            <64-hex private key>            0x prefix optional
            <name> <64-hex private key>     name separated by space , : ; =
            <0x address>                    watch-only (cannot sign)
        Every line is validated individually; invalid and duplicate lines are
        never imported; valid ones are imported even if others fail.

        Grouping: `group_id` adds every imported wallet to an existing group.
        `group_name` (used when no group_id is given) creates ONE new group,
        but only when 2+ wallets will really be imported — a single wallet
        stays standalone and no group is created for it."""
        if not isinstance(text, str) or not text.strip():
            raise WalletError("nothing to import")
        if len(text) > MAX_IMPORT_CHARS:
            raise WalletError("import text too large")
        lines = [(i + 1, ln.strip()) for i, ln in enumerate(text.splitlines())
                 if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            raise WalletError("nothing to import")
        if len(lines) > MAX_IMPORT_LINES:
            raise WalletError(
                f"too many wallets in one import (max {MAX_IMPORT_LINES})")
        if group_id and not self._group_row(group_id):
            raise WalletError("group not found")
        new_group_name = None
        if not group_id and isinstance(group_name, str) and group_name.strip():
            cleaned = self._clean_group_name(group_name)
            if self._count_new(lines) >= 2:
                if self.store.fetchone(
                        "SELECT 1 FROM web3_wallet_groups WHERE name_key=?",
                        (cleaned.lower(),)):
                    raise WalletError("a group with that name already exists")
                new_group_name = cleaned
        if not dry_run:
            # keystore is only needed if at least one line carries a key, but
            # provisioning early keeps failure atomic and simple.
            if any(self._parse_line(ln).get("kind") == "key"
                   for _, ln in lines):
                self._ks(provision=True)

        results, seen = [], set()
        imported = rejected = 0
        new_group = None
        with self.store._lock:
            if new_group_name and not dry_run:
                new_group = self.create_group(new_group_name)
                group_id = new_group["id"]
            for lineno, line in lines:
                p = self._parse_line(line)
                res = {"line": lineno}
                if not p["ok"]:
                    res.update(status="invalid", imported=False,
                               reason=p["reason"])
                    rejected += 1
                    results.append(res)
                    continue
                addr = p["address"]
                res["address"] = addr
                res["kind"] = "watch" if p["kind"] == "watch" else "key"
                if addr in seen or self._row(addr) is not None:
                    res.update(status="duplicate", imported=False,
                               reason="wallet address already registered")
                    rejected += 1
                    results.append(res)
                    continue
                seen.add(addr)
                if dry_run:
                    res.update(status="valid", imported=False)
                else:
                    try:
                        w = self._insert_wallet(
                            addr, p["name"], "watch" if p["kind"] == "watch"
                            else "imported", p.get("_priv"))
                        if group_id:
                            self._add_member(group_id, w["id"])
                        res.update(status="valid", imported=True,
                                   wallet_id=w["id"])
                        imported += 1
                    except Exception:
                        # never include exception text: it could carry data
                        res.update(status="invalid", imported=False,
                                   reason="could not store wallet")
                        rejected += 1
                results.append(res)
                p.pop("_priv", None)
        if new_group is not None and imported < 2:
            # a store failure left fewer than 2 wallets: never keep a group
            # for a single/no wallet (members stay as standalone wallets)
            self.delete_group(new_group["id"])
            new_group = None
        if not dry_run and imported:
            self._ensure_active()
        valid = sum(1 for r in results if r["status"] == "valid")
        out = {"dry_run": bool(dry_run), "imported": imported,
               "rejected": rejected, "valid": valid, "results": results}
        if new_group is not None:
            out["group"] = self._group_public(self._group_row(new_group["id"]))
        return out

    def _insert_wallet(self, address: str, name: str, source: str,
                       priv: int | None) -> dict:
        wid = _new_id("w")
        now = _now()
        label = name or ("Watch wallet" if source == "watch" else
                         "Created wallet" if source == "created"
                         else "Imported wallet")
        if not name:
            label = f"{label} {address[2:6].upper()}"
        ks_name = ""
        if priv is not None:
            ks = self._ks(provision=True)
            ks_name = address                      # record name == address
            ks.store_key(ks_name, "0x" + priv.to_bytes(32, "big").hex(),
                         address=address)
        try:
            self.store.insert(
                "web3_wallets", id=wid, address=address, name=label,
                source=source, keystore_name=ks_name, active=0,
                created_at=now, updated_at=now)
        except Exception:
            if ks_name:
                self._ks().delete(ks_name)         # no orphan secret
            raise
        return self.get(wid)

    # ── create ───────────────────────────────────────────────────────────
    def create_wallet(self, name: str = "", group_id: str | None = None
                      ) -> tuple[dict, str]:
        """Generate a fresh wallet with the OS CSPRNG. Returns
        (public wallet dict, private_key_hex). The private key is returned
        exactly once for the operator's one-time backup display and is never
        readable again through the registry."""
        name = (name or "").strip()[:MAX_NAME_LEN]
        if group_id and not self._group_row(group_id):
            raise WalletError("group not found")
        with self.store._lock:
            for _ in range(8):
                priv = generate_private_key()
                addr = private_to_address(priv)
                if self._row(addr) is None:
                    break
            else:                                    # pragma: no cover
                raise WalletError("could not generate a unique address")
            w = self._insert_wallet(addr, name, "created", priv)
            if group_id:
                self._add_member(group_id, w["id"])
            secret = "0x" + priv.to_bytes(32, "big").hex()
        self._ensure_active()
        return self.get(w["id"]), secret

    # ── signer resolution (used by the Web3 tool surface) ────────────────
    def resolve(self, ref: str = "", need_signer: bool = False) -> dict:
        """Resolve a wallet reference (id / address / '' = active wallet)
        to SAFE metadata. Raises WalletError when it is unknown or, with
        need_signer, when it is watch-only."""
        row = self._row(ref) if (ref or "").strip() else None
        if row is None and not (ref or "").strip():
            act = self.store.fetchone(
                "SELECT * FROM web3_wallets WHERE active=1 LIMIT 1")
            row = act
            if row is None:
                raise WalletError("no active wallet selected")
        if row is None:
            raise WalletError("wallet is not registered")
        if need_signer and row["source"] == "watch":
            raise WalletError("wallet is watch-only and cannot sign")
        if need_signer and not row["keystore_name"]:
            raise WalletError("wallet has no signing credential")
        return self._public(row)

    # ── groups ───────────────────────────────────────────────────────────
    def _group_row(self, gid: str) -> dict | None:
        return self.store.fetchone(
            "SELECT * FROM web3_wallet_groups WHERE id=?", ((gid or "").strip(),))

    @staticmethod
    def _clean_group_name(name: str) -> str:
        name = " ".join((name or "").split())
        if not name:
            raise WalletError("group name is required")
        if len(name) > MAX_GROUP_NAME_LEN:
            raise WalletError(
                f"group name too long (max {MAX_GROUP_NAME_LEN})")
        return name

    def _group_public(self, row: dict) -> dict:
        ids = [r["wallet_id"] for r in self.store.fetch(
            "SELECT wallet_id FROM web3_wallet_group_members WHERE group_id=? "
            "ORDER BY rowid", (row["id"],))]
        return {"id": row["id"], "name": row["name"], "wallet_ids": ids,
                "count": len(ids), "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def list_groups(self) -> list[dict]:
        return [self._group_public(r) for r in self.store.fetch(
            "SELECT * FROM web3_wallet_groups ORDER BY rowid")]

    def create_group(self, name: str) -> dict:
        name = self._clean_group_name(name)
        with self.store._lock:
            if self.store.fetchone(
                    "SELECT 1 FROM web3_wallet_groups WHERE name_key=?",
                    (name.lower(),)):
                raise WalletError("a group with that name already exists")
            gid, now = _new_id("g"), _now()
            self.store.insert("web3_wallet_groups", id=gid, name=name,
                              name_key=name.lower(), created_at=now,
                              updated_at=now)
            return self._group_public(self._group_row(gid))

    def rename_group(self, gid: str, name: str) -> dict:
        name = self._clean_group_name(name)
        with self.store._lock:
            row = self._group_row(gid)
            if row is None:
                raise WalletError("group not found")
            clash = self.store.fetchone(
                "SELECT id FROM web3_wallet_groups WHERE name_key=?",
                (name.lower(),))
            if clash and clash["id"] != row["id"]:
                raise WalletError("a group with that name already exists")
            self.store.exec(
                "UPDATE web3_wallet_groups SET name=?, name_key=?, "
                "updated_at=? WHERE id=?",
                (name, name.lower(), _now(), row["id"]))
            return self._group_public(self._group_row(row["id"]))

    def delete_group(self, gid: str) -> dict:
        """Delete the group and its memberships. Wallets are untouched."""
        with self.store._lock:
            row = self._group_row(gid)
            if row is None:
                raise WalletError("group not found")
            self.store.exec(
                "DELETE FROM web3_wallet_group_members WHERE group_id=?",
                (row["id"],))
            self.store.exec("DELETE FROM web3_wallet_groups WHERE id=?",
                            (row["id"],))
            return {"id": row["id"], "deleted": True}

    def _add_member(self, gid: str, wallet_id: str) -> None:
        self.store.exec(
            "INSERT OR IGNORE INTO web3_wallet_group_members "
            "(group_id, wallet_id, added_at) VALUES (?,?,?)",
            (gid, wallet_id, _now()))
        self.store.exec(
            "UPDATE web3_wallet_groups SET updated_at=? WHERE id=?",
            (_now(), gid))

    def set_members(self, gid: str, add=(), remove=()) -> dict:
        """Add and/or remove wallets (by id or address) in one operation.
        Unknown wallet references fail the whole call before any change."""
        with self.store._lock:
            row = self._group_row(gid)
            if row is None:
                raise WalletError("group not found")
            add_ids = self._wallet_ids(add)
            rem_ids = self._wallet_ids(remove)
            for wid in add_ids:
                self._add_member(row["id"], wid)
            for wid in rem_ids:
                self.store.exec(
                    "DELETE FROM web3_wallet_group_members "
                    "WHERE group_id=? AND wallet_id=?", (row["id"], wid))
            if rem_ids:
                self.store.exec(
                    "UPDATE web3_wallet_groups SET updated_at=? WHERE id=?",
                    (_now(), row["id"]))
            return self._group_public(self._group_row(row["id"]))

    def move_wallet(self, wallet_ref: str, from_group: str | None,
                    to_group: str) -> dict:
        """Atomically move a wallet from one group to another."""
        with self.store._lock:
            w = self._row(wallet_ref)
            if w is None:
                raise WalletError("wallet not found")
            dst = self._group_row(to_group)
            if dst is None:
                raise WalletError("destination group not found")
            src = None
            if from_group:
                src = self._group_row(from_group)
                if src is None:
                    raise WalletError("source group not found")
            self._add_member(dst["id"], w["id"])
            if src and src["id"] != dst["id"]:
                self.store.exec(
                    "DELETE FROM web3_wallet_group_members "
                    "WHERE group_id=? AND wallet_id=?", (src["id"], w["id"]))
            return self.get(w["id"])

    def _wallet_ids(self, refs) -> list[str]:
        out = []
        for ref in refs or ():
            row = self._row(str(ref))
            if row is None:
                raise WalletError("wallet not found")
            if row["id"] not in out:
                out.append(row["id"])
        return out
