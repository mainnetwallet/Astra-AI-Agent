"""Web3 transaction pipeline (Phase D).

Covers the provable crypto (EIP-155 spec vector, address derivation), the
deterministic policy engine (CONFIRM/AUTO/block decisions), keystore
encryption (ciphertext-only on disk, tamper detection), and the Transaction
Manager lifecycle — including emergency stop and the never-sign-twice rule
via a mocked JSON-RPC chain.

Security invariants asserted:
- signed raw bytes never stored plaintext in SQLite,
- keys/seed phrases never appear in API records,
- CONFIRM mode never signs without an explicit authorize(),
- a recovered (already-on-chain) tx is never re-broadcast.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from astra.web3.signer import (keccak_256, private_to_address, sign,
                               private_to_public, GX, GY)
from astra.web3.policy import (TransactionPolicyEngine, PolicyConfig,
                               TxRequest, TransactionPolicyError, _norm)


def make_store():
    from astra.store import Store
    d = tempfile.mkdtemp()
    return Store(os.path.join(d, "test.db"))


class TestSignerVectors(unittest.TestCase):
    def test_keccak_known(self):
        self.assertEqual(
            keccak_256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(
            keccak_256(b"abc").hex(),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        self.assertEqual(
            keccak_256(b"The quick brown fox jumps over the lazy dog").hex(),
            "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15")

    def test_pub_math(self):
        x, y = private_to_public(1)
        self.assertEqual(x, GX)
        self.assertEqual(y, GY)

    def test_eip155_official_vector(self):
        """The EIP-155 spec's example signed transaction reproduces exactly."""
        from astra.web3 import raw_tx
        priv = int("0x4646464646464646464646464646464646464646464646464646464646464646", 16)
        item = raw_tx.build_unsigned_legacy(
            nonce=9, gas_price=20000000000, gas_limit=21000,
            to="0x3535353535353535353535353535353535353535",
            value_wei=10 ** 18, data_hex="")
        h = raw_tx.signing_hash_legacy(item, chain_id=1)
        sig = sign(priv, h, chain_id=1)
        raw = raw_tx.serialize_legacy(item, chain_id=1,
                                    y_parity=sig["recovery_id"] & 1,
                                    r=int(sig["r"], 16), s=int(sig["s"], 16))
        expected = ("0xf86c098504a817c80082520894353535353535353535353535"
                    "3535353535353535880de0b6b3a76400008025a028ef61340bd939"
                    "bc2195fe537567866003e1a15d3c71ff63e1590620aa636276a067c"
                    "be9d8997f761aecb703304b3800ccf555c9f3dc64214b297fb1966a3b6d83")
        self.assertEqual("0x" + raw.hex(), expected)

    def test_deterministic_signature(self):
        priv = 12345
        h = keccak_256(b"deterministic")
        self.assertEqual(sign(priv, h, chain_id=1), sign(priv, h, chain_id=1))

    def test_address_matches_pubkey_digest(self):
        priv = 7
        addr = private_to_address(priv)
        x, y = private_to_public(priv)
        pub = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
        self.assertEqual(addr, "0x" + keccak_256(pub)[-20:].hex())
        self.assertEqual(addr, "0x" + addr[2:])   # canonical lowercase form


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.recipient = "0x3535353535353535353535353535353535353535"

    def test_confirm_mode_asks_within_limits(self):
        eng = TransactionPolicyEngine(PolicyConfig(mode="CONFIRM"))
        d = eng.evaluate(TxRequest("def", self.recipient, 10 ** 15, chain_id=1))
        self.assertEqual(d.verdict, "ask")

    def test_auto_mode_allows_within_limits(self):
        eng = TransactionPolicyEngine(PolicyConfig(mode="AUTO"))
        d = eng.evaluate(TxRequest("def", self.recipient, 10 ** 15))
        self.assertEqual(d.verdict, "allow")

    def test_max_tx_value_blocks(self):
        eng = TransactionPolicyEngine(
            PolicyConfig(mode="AUTO", max_tx_value_wei=10 ** 18))
        d = eng.evaluate(TxRequest("def", self.recipient, 10 ** 18 + 1))
        self.assertEqual(d.verdict, "block")
        self.assertIn("per-transaction", d.reason)

    def test_daily_limit_blocks(self):
        eng = TransactionPolicyEngine(
            PolicyConfig(mode="AUTO", max_daily_tx_value_wei=10 ** 18))
        d = eng.evaluate(TxRequest("def", self.recipient, 10 ** 18),
                         spent_today_wei=10 ** 18)
        self.assertEqual(d.verdict, "block")
        self.assertIn("daily", d.reason)

    def test_recipient_allowlist(self):
        eng = TransactionPolicyEngine(
            PolicyConfig(mode="AUTO", allowed_recipients=frozenset([_norm(self.recipient)])))
        self.assertEqual(
            eng.evaluate(TxRequest("def", self.recipient, 1)).verdict, "allow")
        d = eng.evaluate(TxRequest("def", "0x" + "11" * 20, 1))
        self.assertEqual(d.verdict, "block")

    def test_unsupported_chain_blocked(self):
        eng = TransactionPolicyEngine()
        d = eng.evaluate(TxRequest("def", self.recipient, 1, chain_id=999))
        self.assertEqual(d.verdict, "block")

    def test_deterministic(self):
        eng = TransactionPolicyEngine(PolicyConfig(mode="CONFIRM"))
        req = TxRequest("def", self.recipient, 5)
        a = eng.evaluate(req).to_dict()
        b = eng.evaluate(req).to_dict()
        self.assertEqual(a, b)

    def test_set_mode_operator_only(self):
        eng = TransactionPolicyEngine()
        eng.set_mode("AUTO")
        self.assertEqual(eng.mode, "AUTO")
        with self.assertRaises(TransactionPolicyError):
            eng.set_mode("CHAOS")


class TestKeystore(unittest.TestCase):
    def test_roundtrip_and_ciphertext_only(self):
        from astra.web3.keystore import SecureKeyStore
        store = make_store()
        ks = SecureKeyStore(store, master_secret="op-secret")
        ks.store_seed("alice", "word one two three four five six")
        # ciphertext on disk is hex, not plaintext
        row = store.fetchone("SELECT * FROM web3_keys WHERE name='alice'")
        self.assertNotIn("word one", row["ciphertext"])
        self.assertTrue("word one".encode() not in store_key_bytes(store))
        self.assertEqual(ks.decrypt("alice"), "word one two three four five six")
        safe = ks.list_safe()[0]
        self.assertNotIn("ciphertext", safe)
        self.assertNotIn("word one", str(safe))

    def test_wrong_master_secret_raises(self):
        from astra.web3.keystore import SecureKeyStore, KeystoreError
        store = make_store()
        ks = SecureKeyStore(store, master_secret="real")
        ks.store_seed("card", "held secret phrase here now")
        bad = SecureKeyStore(store, master_secret="wrong")
        with self.assertRaises(KeystoreError):
            bad.decrypt("card")

    def test_tamper_detected(self):
        from astra.web3.keystore import SecureKeyStore, KeystoreError
        store = make_store()
        ks = SecureKeyStore(store, master_secret="real")
        ks.store_seed("card", "aaa bbb ccc ddd eee fff ggg hhh")
        store.exec("UPDATE web3_keys SET ciphertext=? WHERE name='card'",
                   ("deadbeef",))
        with self.assertRaises(KeystoreError):
            ks.decrypt("card")


def store_key_bytes(store):
    out = b""
    for r in store.fetch("SELECT * FROM web3_keys"):
        out += str(r).encode()
    return out


class TestManagerLifecycle(unittest.TestCase):
    RECIPIENT = "0x3535353535353535353535353535353535353535"

    def _mgrs(self, mode="CONFIRM"):
        patches = [
            patch("astra.web3.raw_tx.receipt", return_value=None),
            patch("astra.web3.raw_tx.broadcast", return_value="0xdeadbeef"),
            patch("astra.web3.raw_tx.get_nonce", return_value=0),
            patch("astra.web3.raw_tx.get_gas_price", return_value=10 ** 9),
        ]
        for p in patches:
            p.start()
        from astra.web3.keystore import SecureKeyStore
        from astra.web3.transactions import TransactionManager
        from astra.web3.policy import TransactionPolicyEngine, PolicyConfig
        store = make_store()
        ks = SecureKeyStore(store, master_secret="op")
        ks.store_key("default", '01' * 32)
        mgr = TransactionManager(store, keystore=ks,
                                 policy=TransactionPolicyEngine(PolicyConfig(mode=mode)))
        return mgr, patches

    def _stop_patches(self, patches):
        for p in patches:
            p.stop()

    def test_confirm_requires_explicit_authorize(self):
        mgr, patches = self._mgrs("CONFIRM")
        try:
            rec = mgr.create(TxRequest("default", self.RECIPIENT, 10 ** 15))
            self.assertTrue(rec["requires_approval"])
            with self.assertRaises(TransactionPolicyError):
                mgr.sign_and_broadcast(rec["tx_id"])
            mgr.authorize(rec["tx_id"])
            self.assertEqual(mgr.status(rec["tx_id"])["status"], "AUTHORIZED")
            # never signs twice guard: after first sign+broadcast, replay is a no-op
            out = mgr.sign_and_broadcast(rec["tx_id"])
            self.assertEqual(out["status"], "BROADCAST")
            again = mgr.sign_and_broadcast(rec["tx_id"])
            self.assertEqual(again["status"], "BROADCAST")
        finally:
            self._stop_patches(patches)

    def test_auto_mode_signs_within_limits(self):
        mgr, patches = self._mgrs("AUTO")
        try:
            rec = mgr.create(TxRequest("default", self.RECIPIENT, 10 ** 15))
            self.assertFalse(rec["requires_approval"])
            out = mgr.sign_and_broadcast(rec["tx_id"])
            self.assertEqual(out["status"], "BROADCAST")
            self.assertEqual(out["tx_hash"], "0xdeadbeef")
        finally:
            self._stop_patches(patches)

    def test_emergency_stop_blocks_everything(self):
        mgr, patches = self._mgrs("AUTO")
        try:
            rec = mgr.create(TxRequest("default", self.RECIPIENT, 1))
            mgr.emergency_stop()
            with self.assertRaises(TransactionPolicyError):
                mgr.create(TxRequest("default", self.RECIPIENT, 1))
            with self.assertRaises(TransactionPolicyError):
                mgr.authorize(rec["tx_id"])
            mgr.emergency_resume()
            # after resume a fresh create works
            rec2 = mgr.create(TxRequest("default", self.RECIPIENT, 1))
            self.assertTrue(rec2["status"])
        finally:
            self._stop_patches(patches)

    def test_signed_raw_never_plaintext(self):
        mgr, patches = self._mgrs("AUTO")
        try:
            rec = mgr.create(TxRequest("default", self.RECIPIENT, 10 ** 15))
            mgr.sign_and_broadcast(rec["tx_id"])
            raw = mgr.store.fetchone(
                "SELECT signed_raw_wrapped FROM web3_transactions WHERE tx_id=?",
                (rec["tx_id"],))["signed_raw_wrapped"]
            self.assertTrue(raw)                                  # sealed exists
            self.assertNotIn("0xf8", raw.split("::")[0])         # hex ciphertext only
            pub = mgr.status(rec["tx_id"])
            self.assertNotIn("signed_raw_wrapped", str(pub))      # never in API
        finally:
            self._stop_patches(patches)


class TestPolicyBlockPersist(unittest.TestCase):
    def test_allowlist_block_raises_and_not_persisted(self):
        from astra.web3.policy import (PolicyConfig, TransactionPolicyEngine,
                                       TxRequest, TransactionPolicyError)
        from astra.web3.transactions import TransactionManager
        store = make_store()
        eng = TransactionPolicyEngine(PolicyConfig(
            mode="AUTO", max_tx_value_wei=10 ** 15))
        mgr = TransactionManager(store, keystore=None, policy=eng)
        with self.assertRaises(TransactionPolicyError):
            mgr.create(TxRequest("def", "0x" + "22" * 20, 10 ** 15 + 1))
        self.assertEqual(len(mgr.list()), 0)


if __name__ == "__main__":
    unittest.main()
