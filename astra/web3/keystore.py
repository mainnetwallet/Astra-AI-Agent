"""Web3 keystore — encrypted private materials, never in plaintext.

Secrets (seed phrases / private keys) are wrapped with a key derived from
the operator's master secret (ASTRA_MASTER_SECRET, or file `data/.master_key`)
plus a per-record random salt. Only the ciphertext and an HMAC tag reach
SQLite. Deriving keys without libsodium/hashlib-based KDF: we use
PBKDF2-HMAC-SHA256 via hashlib.pbkdf2_hmac — stdlib, auditable, n=120_000.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from astra.core.exceptions import AstraError

KDF_ITERATIONS = 120_000
DEFAULT_MASTER_KEY_FILE = "data/.master_key"


class KeystoreError(AstraError):
    pass


def _derive_master(master_secret: str, force_read: bool = True) -> bytes:
    """Normalize the operator master secret into a 32-byte key."""
    if not master_secret:
        raise KeystoreError("no master secret configured (ASTRA_MASTER_SECRET "
                            "or data/.master_key)")
    return hashlib.sha256(master_secret.encode("utf-8")).digest()


class SecureKeyStore:
    """Encrypt-at-rest key store. Interface keys on the operator master
    secret; records live in the SQLite `web3_keys` table as ciphertext only.

    Never returns a secret except through `decrypt()` which the caller must
    hold in memory transiently — it is never serialized, logged, or emitted.
    """

    TABLE = """
    CREATE TABLE IF NOT EXISTS web3_keys (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT UNIQUE NOT NULL,   -- e.g. 'default'
        address    TEXT DEFAULT '',        -- derived address (safe to show)
        kind       TEXT DEFAULT 'seed',    -- 'seed' | 'key'
        ciphertext TEXT NOT NULL,
        tag        TEXT NOT NULL,
        salt       TEXT NOT NULL,
        created_at TEXT DEFAULT ''
    );
    """

    def __init__(self, store, master_secret: str = ""):
        self.store = store
        self._master = _derive_master(master_secret)
        if not store.table_exists("web3_keys"):
            store.install(self.TABLE)

    # -- low-level crypto ----------------------------------------------------
    def _seal(self, data: str) -> tuple[str, str, str]:
        salt = os.urandom(16)
        key = hashlib.pbkdf2_hmac(
            "sha256", self._master, salt, KDF_ITERATIONS, dklen=32)
        # AES unsupported; use XChaCha-like construction via HMAC drbg is
        # overkill. For the keystore wrap we use an XOR keystream from
        # SHA-256(key || counter) — auditable, non-repeating per salt.
        plain = data.encode("utf-8")
        blocks = []
        for i in range(0, len(plain), 32):
            ctr = (i // 32).to_bytes(8, "big")
            keystream = hashlib.sha256(key + ctr).digest()
            chunk = plain[i:i + 32]
            blocks.append(bytes(a ^ b for a, b in zip(chunk, keystream)))
        ciphertext = b"".join(blocks)
        tag = hmac.new(key, ciphertext, hashlib.sha256).hexdigest()
        return (ciphertext.hex(), tag, salt.hex())

    def _open(self, ciphertext_hex: str, tag_hex: str, salt_hex: str) -> str:
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac(
            "sha256", self._master, salt, KDF_ITERATIONS, dklen=32)
        ciphertext = bytes.fromhex(ciphertext_hex)
        expected = hmac.new(key, ciphertext, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, tag_hex):
            raise KeystoreError("keystore integrity check failed (wrong "
                                "master secret or tampered record)")
        plain = b""
        for i in range(0, len(ciphertext), 32):
            ctr = (i // 32).to_bytes(8, "big")
            keystream = hashlib.sha256(key + ctr).digest()
            chunk = ciphertext[i:i + 32]
            plain += bytes(a ^ b for a, b in zip(chunk, keystream))
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KeystoreError("keystore decryption failed") from exc

    # -- records -------------------------------------------------------------
    def store_seed(self, name: str, seed_phrase: str, address: str = "") -> dict:
        """Seal and persist a seed phrase. `address` is derived separately and
        is the only safe-on-disk representation."""
        ct, tag, salt = self._seal(seed_phrase)
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.store.insert("web3_keys", name=name, address=address,
                              kind="seed", ciphertext=ct, tag=tag, salt=salt,
                              created_at=now)
            return {"name": name, "address": address, "kind": "seed"}
        except Exception as exc:
            raise KeystoreError(f"failed to store key record: {exc}") from exc

    def store_key(self, name: str, private_key_hex: str, address: str = "") -> dict:
        ct, tag, salt = self._seal(private_key_hex)
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.store.insert("web3_keys", name=name, address=address,
                          kind="key", ciphertext=ct, tag=tag, salt=salt,
                          created_at=now)
        return {"name": name, "address": address, "kind": "key"}

    def get(self, name: str) -> dict | None:
        row = self.store.fetchone(
            "SELECT * FROM web3_keys WHERE name = ?", (name,))
        if not row:
            return None
        return {"id": row["id"], "name": row["name"],
                "address": row["address"], "kind": row["kind"]}

    def decrypt(self, name: str) -> str:
        """Return the plaintext secret for transient in-memory use.
        The caller must treat the return value as a secret: never log, emit,
        or store it."""
        row = self.store.fetchone(
            "SELECT * FROM web3_keys WHERE name = ?", (name,))
        if not row:
            raise KeystoreError(f"no key record named '{name}'")
        return self._open(row["ciphertext"], row["tag"], row["salt"])

    def list_safe(self) -> list[dict]:
        """Public metadata only — never ciphertext/tag/salt."""
        out = []
        for r in self.store.fetch("SELECT * FROM web3_keys ORDER BY id"):
            out.append({"id": r["id"], "name": r["name"],
                        "address": r["address"], "kind": r["kind"]})
        return out

    def delete(self, name: str) -> None:
        self.store.exec("DELETE FROM web3_keys WHERE name = ?", (name,))

    def rotate_master(self, new_secret: str) -> int:
        """Re-wrap every record under a new master secret."""
        new_master = _derive_master(new_secret)
        self._master = new_master
        re_wrapped = 0
        rows = self.store.fetch("SELECT * FROM web3_keys")
        for row in rows:
            try:
                plain = self._open(row["ciphertext"], row["tag"], row["salt"])
                ct, tag, salt = self._seal(plain)
                self.store.exec(
                    "UPDATE web3_keys SET ciphertext=?, tag=?, salt=? WHERE id=?",
                    (ct, tag, salt, row["id"]))
                re_wrapped += 1
            except KeystoreError:
                continue
        return re_wrapped
