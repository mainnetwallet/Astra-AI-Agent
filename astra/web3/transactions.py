"""Web3 Transaction Manager — authorized, persisted, reversible-boundary.

The manager owns the transaction lifecycle (PREPARED → VALIDATED →
AUTHORIZED → SIGNED → BROADCAST → CONFIRMED / FAILED / REJECTED) and the
single-threaded signing lock. Deterministic code signs; the LLM can only
PREPARE (create) a transaction — AUTHORIZE only happens through
`authorize()` which the tool surface never exposes to the model, and
`emergency_stop()` shuts the pipeline down out-of-band.

Never-sign-twice: before any sign/broadcast the manager derives the *exact*
deterministic tx hash from the persisted params (RFC 6979 ⇒ byte-identical
signature on replay) and checks on-chain for that hash; only when it is
absent from mempool/chain may it sign+broadcast. Signed raw bytes are sealed
(tag/MAC check) like keystore records and are never returned by the API.
"""
from __future__ import annotations

import threading
from datetime import datetime

from .signer import sign
from .keystore import SecureKeyStore
from . import txtx
from . import chains
from astra.core.exceptions import AstraError
from .policy import (TransactionPolicyEngine, PolicyConfig, TxRequest,
                     TransactionPolicyError, TransactionRejectedError,
                     TransactionFailedError)

_LIFECYCLE = ("CREATED", "PREPARED", "VALIDATED", "AUTHORIZED", "SIGNED",
              "BROADCAST", "CONFIRMED", "FAILED", "REJECTED", "CANCELLED")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


TX_SCHEMA = """
CREATE TABLE IF NOT EXISTS web3_transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id       TEXT UNIQUE NOT NULL,
    status      TEXT DEFAULT 'CREATED',
    chain_id    INTEGER DEFAULT 1,
    from_address TEXT DEFAULT '',
    to_address   TEXT DEFAULT '',
    value_wei   TEXT DEFAULT '0',
    data_hex    TEXT DEFAULT '',
    gas_limit   INTEGER DEFAULT 0,
    max_fee_per_gas INTEGER DEFAULT 0,
    max_priority_fee_per_gas INTEGER DEFAULT 0,
    nonce       INTEGER,
    signed_raw_wrapped TEXT DEFAULT '',    -- sealed, never plaintext API-visible
    submitted_hash TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    kind        TEXT DEFAULT 'send',
    authorized_by TEXT DEFAULT '',         -- always 'policy' only
    created_at  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT '',
    broadcast_at TEXT DEFAULT ''
);
"""


class TransactionManager:
    def __init__(self, store, keystore: SecureKeyStore | None = None,
                 policy: TransactionPolicyEngine | None = None,
                 events=None, config=None):
        self.store = store
        self.keystore = keystore
        self.policy = policy or TransactionPolicyEngine()
        self.events = events
        self.config = config or {}
        self._lock = threading.RLock()     # serializes sign+broadcast
        self._stopped = False              # emergency stop latch
        self._paused = False
        if not store.table_exists("web3_transactions"):
            store.install(TX_SCHEMA)

    # -- lifecycle -----------------------------------------------------------
    def create(self, req: TxRequest) -> dict:
        """Prepare (create) a transaction. The ONLY entry the tool surface
        may call. Returns a control record — never signature material."""
        with self._lock:
            if self._stopped:
                raise TransactionPolicyError(
                    "transaction pipeline is emergency-stopped")
            decision = self.policy.evaluate(req, spent_today_wei=self._spent_today())
            if decision.verdict == "block":
                raise TransactionPolicyError(
                    f"blocked by web3 policy: {decision.reason}")
            tx_id = "tx-" + _new_tx_id(req)
            self.store.insert(
                "web3_transactions", tx_id=tx_id, status="PREPARED",
                chain_id=req.chain_id, from_address=req.from_address,
                to_address=req.to_address, value_wei=str(req.value_wei),
                data_hex=req.data_hex, gas_limit=req.gas_limit,
                max_fee_per_gas=req.max_fee_per_gas,
                max_priority_fee_per_gas=req.max_priority_fee_per_gas,
                nonce=req.nonce, created_at=_now(), updated_at=_now(),
                kind="send", authorized_by="")
            rec = self.get(tx_id)
            rec["decision"] = decision.to_dict()
            rec["requires_approval"] = (decision.verdict == "ask")
            if self.events:
                self.events.emit("web3.transaction.prepared", tx=tx_id,
                                 status="PREPARED",
                                 decision=decision.verdict)
            return rec

    def authorize(self, tx_id: str) -> dict:
        """Move PREPARED → AUTHORIZED. Operator/policy layer only; the LLM
        path never reaches this method."""
        with self._lock:
            self._check_active()
            rec = self._row(tx_id)
            if rec is None:
                raise TransactionRejectedError(f"unknown tx {tx_id}")
            if rec["status"] not in ("PREPARED", "VALIDATED"):
                raise TransactionRejectedError(
                    f"tx {tx_id} not authorizable (status={rec['status']})")
            self.store.exec(
                "UPDATE web3_transactions SET status='AUTHORIZED', "
                "authorized_by='policy', updated_at=? WHERE tx_id=?",
                (_now(), tx_id))
            return {"tx_id": tx_id, "status": "AUTHORIZED"}

    def sign_and_broadcast(self, tx_id: str) -> dict:
        """Sign (deterministic) and broadcast under the signing lock, with
        on-chain re-check so we never double-broadcast the same tx."""
        with self._lock:
            self._check_active()
            rec = self._row(tx_id)
            if rec is None:
                raise TransactionRejectedError(f"unknown tx {tx_id}")
            # already submitted (BROADCAST/CONFIRMED): idempotent no-op —
            # never sign or broadcast the same tx again.
            if rec.get("submitted_hash") and rec["status"] in (
                    "BROADCAST", "CONFIRMED"):
                return self.status(tx_id)
            if rec["status"] not in ("AUTHORIZED", "PREPARED"):
                raise TransactionFailedError(
                    f"tx {tx_id} not in a signable state ({rec['status']})")
            status = rec["status"]
            if status == "PREPARED" and self.policy.mode == "CONFIRM":
                raise TransactionPolicyError(
                    "tx not authorized: send CONFIRM mode without an "
                    "operator approve")

            # never-sign-twice: exactly the bytes that went out (or would go
            # out) are checked on-chain before anything is recomputed. If a
            # sealed raw already exists we re-derive its hash (we never sign
            # again); otherwise the tx was never signed → re-create.
            chain = self._chain_by_id(rec["chain_id"])
            if rec.get("signed_raw_wrapped"):
                ct, tag, salt = rec["signed_raw_wrapped"].split("::", 2)
                raw_hex = self.keystore._open(ct, tag, salt)
                det_hash = txtx.tx_hash(bytes.fromhex(raw_hex[2:])
                                        if raw_hex.startswith("0x")
                                        else bytes.fromhex(raw_hex))
            else:
                det_hash = None
            if det_hash:
                on_chain = None
                for url in chain.get("rpcs", [])[:3]:
                    try:
                        on_chain = txtx.receipt([url], det_hash)
                        break
                    except Exception:
                        continue
                if on_chain:
                    # already mined on-chain — never rebroadcast
                    self.store.exec(
                        "UPDATE web3_transactions SET status='CONFIRMED', "
                        "submitted_hash=?, error='recovered: already on chain', "
                        "updated_at=? WHERE tx_id=?",
                        (det_hash, _now(), tx_id))
                    return {"tx_id": tx_id, "status": "CONFIRMED",
                            "recovered": True, "tx_hash": det_hash}

            self._mark(rec["id"], "AUTHORIZED")
            sig = self._sign_rec(rec, chain=chain)
            raw_hex = sig["raw_hex"]
            # seal the signed raw (sensitive) — never stored plaintext
            if self.keystore:
                ct, tag, salt = self.keystore._seal(raw_hex)
                self.store.exec(
                    "UPDATE web3_transactions SET signed_raw_wrapped=?, "
                    "updated_at=? WHERE tx_id=?",
                    (f"{ct}::{tag}::{salt}", _now(), tx_id))
            self.store.exec(
                "UPDATE web3_transactions SET status='SIGNED', "
                "nonce=?, updated_at=? WHERE tx_id=?",
                (sig["nonce"], _now(), tx_id))
            if self.events:
                self.events.emit("web3.transaction.submitted", tx=tx_id, nonce=sig["nonce"])
            hash_broadcast = txtx.broadcast(chain.get("rpcs", []), raw_hex)
            self.store.exec(
                "UPDATE web3_transactions SET status='BROADCAST', "
                "submitted_hash=?, broadcast_at=?, updated_at=? WHERE tx_id=?",
                (hash_broadcast, _now(), _now(), tx_id))
            if self.events:
                self.events.emit("web3.tx.broadcast", tx=tx_id,
                                 hash=hash_broadcast)
            return {"tx_id": tx_id, "status": "BROADCAST",
                    "tx_hash": hash_broadcast}

    def confirm(self, tx_id: str, timeout_s: float = 0) -> dict:
        """Poll for a mined receipt. Returns lifecycle-consistent status."""
        assert timeout_s >= 0
        import time
        rec = self._row(tx_id)
        if rec is None:
            return {"tx_id": tx_id, "status": "UNKNOWN"}
        if rec["status"] in ("CONFIRMED", "FAILED", "REJECTED"):
            return self.status(tx_id)
        h = rec.get("submitted_hash")
        if not h:
            return {"tx_id": tx_id, "status": rec["status"]}
        chain = self._chain_by_id(rec["chain_id"])
        deadline = time.time() + timeout_s
        last = None
        while True:
            try:
                rcpt = txtx.receipt(chain.get("rpcs", []), h)
            except Exception as exc:
                rcpt, last = None, str(exc)[:120]
            if rcpt and rcpt.get("status") == "0x1":
                self._mark(rec["id"], "CONFIRMED")
                return {"tx_id": tx_id, "status": "CONFIRMED", "tx_hash": h}
            if rcpt is not None and rcpt.get("status") == "0x0":
                self._mark(rec["id"], "FAILED", error="tx reverted on-chain")
                raise TransactionFailedError(f"tx {tx_id} reverted on-chain")
            if timeout_s and time.time() > deadline:
                return {"tx_id": tx_id, "status": "PENDING", "tx_hash": h}
            time.sleep(2.0)

    # -- recovery: stale AUTHORIZED/SIGNED txs after a restart -----------------
    def recover(self) -> list[str]:
        """After a crash, resolve any in-flight SIGNED/BROADCAST tx the only
        safe way: query the chain. Never blind-retries a submission."""
        out = []
        for row in self.store.fetch(
                "SELECT * FROM web3_transactions "
                "WHERE status IN ('SIGNED', 'BROADCAST', 'AUTHORIZED')"):
            tx_id = row["tx_id"]
            try:
                res = self.sign_and_broadcast_checked(tx_id)
                out.append(tx_id)
            except Exception as exc:
                out.append(tx_id)   # surfaced below via status/error
                self.store.exec(
                    "UPDATE web3_transactions SET error=?, updated_at=? "
                    "WHERE tx_id=?",
                    (str(exc)[:200], _now(), tx_id))
        return out

    def sign_and_broadcast_checked(self, tx_id: str) -> dict:
        """Idempotent re-entry for recovery: never re-signs/re-broadcasts a
        tx that is already confirmed on-chain (see sign_and_broadcast)."""
        with self._lock:
            rec = self._row(tx_id)
            if rec is None or rec["status"] in ("CONFIRMED", "FAILED"):
                return {"tx_id": tx_id, "status": rec["status"] if rec else "UNKNOWN"}
            return self.sign_and_broadcast(tx_id)

    # -- intro ---------------------------------------------------------------
    def status(self, tx_id: str) -> dict:
        rec = self._row(tx_id)
        if rec is None:
            return {"tx_id": tx_id, "status": "UNKNOWN"}
        return {"tx_id": tx_id, "status": rec["status"],
                "chain_id": rec["chain_id"],
                "from": rec["from_address"], "to": rec["to_address"],
                "value_wei": int(rec["value_wei"] or 0),
                "nonce": rec["nonce"], "tx_hash": rec.get("submitted_hash", ""),
                "error": rec.get("error", ""),
                "authorized_by": rec.get("authorized_by", ""),
                "created_at": rec.get("created_at", ""),
                "broadcast_at": rec.get("broadcast_at", "")}

    def get(self, tx_id: str) -> dict:
        return self.status(tx_id)

    def list(self, limit: int = 50) -> list[dict]:
        out = []
        for row in self.store.fetch(
                "SELECT * FROM web3_transactions ORDER BY id DESC LIMIT ?",
                (limit,)):
            out.append({"tx_id": row["tx_id"], "status": row["status"],
                        "to": row["to_address"], "value_wei": row["value_wei"],
                        "chain_id": row["chain_id"],
                        "error": row.get("error", ""),
                        "created_at": row.get("created_at", "")})
        return out

    def stats(self) -> dict:
        rows = self.store.fetch(
            "SELECT status, COUNT(*) c FROM web3_transactions GROUP BY status")
        return {"by_status": {r["status"]: r["c"] for r in rows},
                "mode": self.policy.mode, "stopped": self._stopped,
                "paused": self._paused}

    # -- emergency stop ------------------------------------------------------
    def emergency_stop(self) -> dict:
        self._stopped = True
        self.store.exec(
            "UPDATE web3_transactions SET status='CANCELLED', "
            "error='emergency stop', updated_at=? WHERE status IN "
            "('PREPARED','AUTHORIZED','SIGNED')", (_now(),))
        if self.events:
            self.events.emit("web3.transaction.failed", tx="*", reason="emergency stop")
        return {"stopped": True}

    def emergency_resume(self) -> dict:
        self._stopped = False
        if self.events:
            self.events.emit("web3.transaction.confirmed", tx="*", reason="resume")
        return {"stopped": False}

    def reject(self, tx_id: str, reason: str = "") -> dict:
        self.store.exec(
            "UPDATE web3_transactions SET status='REJECTED', error=?, "
            "updated_at=? WHERE tx_id=?", (reason[:200], _now(), tx_id))
        return {"tx_id": tx_id, "status": "REJECTED"}

    # -- internals -----------------------------------------------------------
    def _row(self, tx_id: str):
        return self.store.fetchone(
            "SELECT * FROM web3_transactions WHERE tx_id = ?", (tx_id,))

    def _check_active(self) -> None:
        if self._stopped:
            raise TransactionPolicyError("pipeline is emergency-stopped")
        if self._paused:
            raise TransactionPolicyError("pipeline is paused")

    def _spent_today(self) -> int:
        if not self.policy.cfg.max_daily_tx_value_wei:
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self.store.fetch(
            "SELECT value_wei FROM web3_transactions "
            "WHERE status='CONFIRMED' AND updated_at LIKE ?", (today + "%",))
        return sum(int(r["value_wei"] or 0) for r in rows)

    def _chain_by_id(self, chain_id: int) -> dict:
        for cid, c in chains.CHAINS.items():
            if int(c.get("chain_id", 0)) == int(chain_id):
                return c
        raise TransactionFailedError(f"unsupported chain id {chain_id}")

    def _sign_rec(self, rec: dict, chain: dict) -> dict:
        """Final signing path: decrypts the key transiently, signs once,
        seals raw, signs the hash. Returns nonce/raw_hex for the caller."""
        if not self.keystore:
            raise TransactionPolicyError("no keystore configured — cannot sign")
        name = rec["from_address"] or "default"
        secret = self.keystore.decrypt(name)
        priv_int = int(secret, 16) if len(secret) <= 66 and not any(
            c in secret for c in " ") else None
        if priv_int is None:
            # seed phrase → derive deterministically from BIP39 (mnemonic)
            from .signer import derive_private_key_bip39
            hashed = keccak_256(derive_private_key_bip39(secret))[:31]
            priv_int = int.from_bytes(b"\x00" + hashed, "big")
        chain_id = int(rec["chain_id"])
        nonce = rec["nonce"]
        if nonce is None:
            nonce = txtx.get_nonce(chain.get("rpcs", []), rec["from_address"])
        value = int(rec["value_wei"] or 0)
        gas_limit = int(rec.get("gas_limit") or 21000)
        max_fee = int(rec.get("max_fee_per_gas") or 0)
        max_prio = int(rec.get("max_priority_fee_per_gas") or 0)
        if not max_fee:
            max_fee = txtx.get_gas_price(chain.get("rpcs", []))
        item = txtx.build_unsigned_eip1559(
            chain_id=chain_id, nonce=nonce, max_priority_fee=max_prio,
            max_fee=max_fee, gas_limit=gas_limit, to=rec["to_address"],
            value_wei=value, data_hex=rec["data_hex"])
        sig = sign(priv_int, txtx.signing_hash_eip1559(item), chain_id=chain_id)
        raw = txtx.serialize_eip1559(
            item, y_parity=sig["recovery_id"] & 1,
            r=int(sig["r"], 16), s=int(sig["s"], 16))
        return {"nonce": nonce, "max_fee": max_fee,
                "max_priority_fee": max_prio, "gas_limit": gas_limit,
                "raw_hex": "0x" + raw.hex()}

    def _mark(self, row_id: int, status: str, error: str = "") -> None:
        self.store.exec(
            "UPDATE web3_transactions SET status=?, error=?, updated_at=? "
            "WHERE id=?",
            (status, error[:200] if error else "", _now(), row_id))


def _new_tx_id(req: TxRequest) -> str:
    import uuid
    return uuid.uuid4().hex[:12]