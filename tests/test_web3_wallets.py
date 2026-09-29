"""Wallet registry: import / create / groups, and its integration with the
Web3 tool surface and transaction policy.

Every key below is a TEST-ONLY value (repeated bytes / tiny integers). None
of them ever held funds and none are real wallet secrets.

Security invariants asserted here:
- secrets never appear in list/get/describe/resolve output, in the SQLite
  file bytes (ciphertext only), in logs, stdout/stderr, or the event bus;
- validation results never echo any part of a submitted secret;
- the model-facing tool results and the AI package never touch secrets;
- the transaction policy (CONFIRM/AUTO, allowlists) still gates every send.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from astra.core.context import ToolContext
from astra.core.permissions import Policy
from astra.store import Store
from astra.tools.registry import ToolRegistry
from astra.web3.keystore import SecureKeyStore
from astra.web3.policy import PolicyConfig, TransactionPolicyEngine
from astra.web3.signer import private_to_address
from astra.web3.tools import register_web3_tools
from astra.web3.transactions import TransactionManager
from astra.web3.wallets import (MAX_IMPORT_LINES, WalletError,
                                WalletRegistry, ensure_master_key_file)

K1 = "11" * 32
K2 = "22" * 32
K3 = "33" * 32
K4 = "44" * 32
KEYS = [K1, K2, K3, K4]
A1, A2, A3, A4 = (private_to_address(int(k, 16)) for k in KEYS)
RECIPIENT = "0x" + "35" * 20


class _Env:
    """A temp DB file + master key file, reopenable to prove persistence."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "astra.db")
        self.master = os.path.join(self.dir, "data", ".master_key")

    def open(self):
        store = Store(self.db)
        reg = WalletRegistry(store, master_key_path=self.master)
        if os.path.exists(self.master):      # what bootstrap does at startup
            reg._keystore = SecureKeyStore(
                store, ensure_master_key_file(self.master))
        return store, reg


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.store, self.reg = self.env.open()

    def tearDown(self):
        self.store.close()


# ── derivation sanity ───────────────────────────────────────────────────────
class TestKnownVector(unittest.TestCase):
    def test_private_key_one_address(self):
        # The universally published address of secp256k1 private key 1.
        self.assertEqual(private_to_address(1),
                         "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf")


# ── 1. IMPORT ───────────────────────────────────────────────────────────────
class TestImport(RegistryCase):
    def test_single_wallet_import(self):
        res = self.reg.import_wallets(K1)
        self.assertEqual((res["imported"], res["rejected"]), (1, 0))
        self.assertEqual(res["results"][0]["status"], "valid")
        self.assertTrue(res["results"][0]["imported"])
        ws = self.reg.list_wallets()
        self.assertEqual([w["address"] for w in ws], [A1])
        self.assertEqual(ws[0]["source"], "imported")
        self.assertTrue(ws[0]["can_sign"])

    def test_0x_prefix_and_name(self):
        self.reg.import_wallets(f"Trading Bot, 0x{K1}")
        w = self.reg.list_wallets()[0]
        self.assertEqual(w["name"], "Trading Bot")
        self.assertEqual(w["address"], A1)

    def test_multiple_wallet_import_with_invalid_line(self):
        text = "\n".join([K1, K2, K3, "not-a-key"])
        res = self.reg.import_wallets(text)
        self.assertEqual((res["imported"], res["rejected"]), (3, 1))
        st = [r["status"] for r in res["results"]]
        self.assertEqual(st, ["valid", "valid", "valid", "invalid"])
        self.assertEqual([r["line"] for r in res["results"]], [1, 2, 3, 4])
        self.assertEqual({w["address"] for w in self.reg.list_wallets()},
                         {A1, A2, A3})

    def test_invalid_wallet_is_not_imported(self):
        res = self.reg.import_wallets("12345\n" + "zz" * 32 + "\n" + "00" * 32)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(res["rejected"], 3)
        self.assertEqual(self.reg.count(), 0)
        self.assertTrue(all(r["status"] == "invalid" and r["reason"]
                            for r in res["results"]))

    def test_zero_and_out_of_range_keys_rejected(self):
        res = self.reg.import_wallets("00" * 32 + "\n" + "ff" * 32)
        self.assertEqual(res["imported"], 0)

    def test_duplicate_within_batch_and_against_registry(self):
        res = self.reg.import_wallets(f"{K1}\n{K1}\n0x{K1}")
        self.assertEqual((res["imported"], res["rejected"]), (1, 2))
        self.assertEqual([r["status"] for r in res["results"]],
                         ["valid", "duplicate", "duplicate"])
        again = self.reg.import_wallets(K1)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["results"][0]["status"], "duplicate")
        self.assertEqual(self.reg.count(), 1)

    def test_valid_lines_import_even_when_others_fail(self):
        res = self.reg.import_wallets(f"garbage\n{K1}\n{K1}\n{K2}")
        self.assertEqual((res["imported"], res["rejected"]), (2, 2))

    def test_dry_run_validates_but_imports_nothing(self):
        res = self.reg.import_wallets(f"{K1}\nbad\n{K1}", dry_run=True)
        self.assertEqual([r["status"] for r in res["results"]],
                         ["valid", "invalid", "duplicate"])
        self.assertEqual(res["imported"], 0)
        self.assertEqual(self.reg.count(), 0)

    def test_seed_phrase_and_json_keystore_are_refused_clearly(self):
        phrase = " ".join(["abandon"] * 11 + ["about"])
        res = self.reg.import_wallets(phrase + "\n" + '{"crypto": {}}')
        self.assertEqual(res["imported"], 0)
        self.assertIn("seed phrase", res["results"][0]["reason"])
        self.assertIn("JSON keystore", res["results"][1]["reason"])

    def test_watch_only_address(self):
        res = self.reg.import_wallets(f"Cold {A2}")
        self.assertEqual(res["imported"], 1)
        w = self.reg.list_wallets()[0]
        self.assertEqual((w["source"], w["can_sign"]), ("watch", False))
        self.assertEqual(self.reg._ks() and
                         self.reg._ks().get(A2), None)   # no key stored
        with self.assertRaises(WalletError):
            self.reg.resolve(A2, need_signer=True)

    def test_limits_and_empty_input(self):
        with self.assertRaises(WalletError):
            self.reg.import_wallets("   \n# only a comment")
        with self.assertRaises(WalletError):
            self.reg.import_wallets("\n".join([K1] * (MAX_IMPORT_LINES + 1)))

    def test_import_result_never_echoes_secrets(self):
        res = self.reg.import_wallets(f"{K1}\n{K2}\nbad {K3[:20]}")
        blob = json.dumps(res) + json.dumps(self.reg.describe())
        for k in KEYS:
            self.assertNotIn(k, blob)
        self.assertNotIn(K3[:20], blob)

    def test_first_import_becomes_active_and_later_ones_do_not(self):
        self.reg.import_wallets(K1)
        self.reg.import_wallets(K2)
        self.assertEqual(self.reg.active()["address"], A1)

    def test_persistence_across_restart(self):
        self.reg.import_wallets(f"Alpha {K1}\nBeta {K2}")
        self.reg.set_active(A2)
        self.store.close()
        store2, reg2 = self.env.open()
        try:
            ws = {w["address"]: w for w in reg2.list_wallets()}
            self.assertEqual(set(ws), {A1, A2})
            self.assertEqual(ws[A1]["name"], "Alpha")
            self.assertEqual(reg2.active()["address"], A2)
            # the sealed key survived and still decrypts after restart
            self.assertEqual(reg2._ks().decrypt(A1), "0x" + K1)
        finally:
            store2.close()

    def test_import_into_group(self):
        g = self.reg.create_group("Trading")
        self.reg.import_wallets(f"{K1}\n{K2}", group_id=g["id"])
        self.assertEqual(self.reg.list_groups()[0]["count"], 2)
        with self.assertRaises(WalletError):
            self.reg.import_wallets(K3, group_id="g_missing")


# ── 2. CREATE ───────────────────────────────────────────────────────────────
class TestCreate(RegistryCase):
    def test_create_registers_persists_and_matches_key(self):
        w, secret = self.reg.create_wallet("Fresh")
        self.assertEqual(w["source"], "created")
        self.assertEqual(w["name"], "Fresh")
        self.assertEqual(w["address"], private_to_address(int(secret, 16)))
        self.assertRegex(w["address"], r"^0x[0-9a-f]{40}$")
        self.assertEqual(self.reg.active()["id"], w["id"])
        self.store.close()
        store2, reg2 = self.env.open()
        try:
            self.assertEqual([x["address"] for x in reg2.list_wallets()],
                             [w["address"]])
            self.assertEqual(reg2._ks().decrypt(w["address"]), secret)
        finally:
            store2.close()

    def test_unique_addresses(self):
        addrs = {self.reg.create_wallet()[0]["address"] for _ in range(6)}
        self.assertEqual(len(addrs), 6)

    def test_master_key_file_is_provisioned_private(self):
        self.assertFalse(os.path.exists(self.env.master))
        self.reg.create_wallet()
        self.assertTrue(os.path.exists(self.env.master))
        if os.name == "posix":
            self.assertEqual(os.stat(self.env.master).st_mode & 0o077, 0)

    def test_secret_returned_once_and_never_readable_again(self):
        w, secret = self.reg.create_wallet()
        blob = json.dumps([self.reg.describe(), self.reg.get(w["id"]),
                           self.reg.resolve(w["id"], need_signer=True),
                           self.reg.list_wallets(), self.reg.active()])
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret[2:], blob)

    def test_secret_absent_from_database_file_plaintext(self):
        _, secret = self.reg.create_wallet()
        self.reg.import_wallets(K1)
        self.store.exec("PRAGMA wal_checkpoint(FULL)")
        raw = b""
        for suffix in ("", "-wal"):
            p = self.env.db + suffix
            if os.path.exists(p):
                raw += open(p, "rb").read()
        for s in (secret, secret[2:], K1):
            self.assertNotIn(s.encode(), raw)

    def test_no_secret_in_logs_stdout_events(self):
        buf, out, err = io.StringIO(), io.StringIO(), io.StringIO()
        h = logging.StreamHandler(buf)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(h)
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                _, secret = self.reg.create_wallet()
                self.reg.import_wallets(f"{K1}\nbad")
                self.reg.describe()
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        haystack = buf.getvalue() + out.getvalue() + err.getvalue()
        for s in (secret, secret[2:], K1):
            self.assertNotIn(s, haystack)

    def test_create_into_group(self):
        g = self.reg.create_group("DeFi")
        w, _ = self.reg.create_wallet(group_id=g["id"])
        self.assertEqual(self.reg.list_wallets(group_id=g["id"])[0]["id"],
                         w["id"])


# ── 3. GROUPS ───────────────────────────────────────────────────────────────
class TestGroups(RegistryCase):
    def setUp(self):
        super().setUp()
        self.reg.import_wallets("\n".join([K1, K2, K3, K4]))
        self.w = {w["address"]: w["id"] for w in self.reg.list_wallets()}

    def test_create_group_and_unique_name(self):
        g = self.reg.create_group("Main Wallets")
        self.assertEqual(g["name"], "Main Wallets")
        self.assertEqual(g["count"], 0)
        with self.assertRaises(WalletError):
            self.reg.create_group("main   wallets")     # case/space-insensitive
        with self.assertRaises(WalletError):
            self.reg.create_group("   ")

    def test_rename_group(self):
        g = self.reg.create_group("Old")
        other = self.reg.create_group("Other")
        self.assertEqual(self.reg.rename_group(g["id"], "New")["name"], "New")
        with self.assertRaises(WalletError):
            self.reg.rename_group(g["id"], "other")
        self.assertEqual(self.reg.rename_group(g["id"], "NEW")["name"], "NEW")
        self.assertEqual(other["name"], "Other")

    def test_delete_group_keeps_wallets(self):
        g = self.reg.create_group("Temp")
        self.reg.set_members(g["id"], add=[self.w[A1], A2])
        self.reg.delete_group(g["id"])
        self.assertEqual(self.reg.list_groups(), [])
        self.assertEqual(self.reg.count(), 4)
        self.assertEqual(self.reg.get(A1)["group_ids"], [])
        with self.assertRaises(WalletError):
            self.reg.delete_group(g["id"])

    def test_add_and_remove_wallets(self):
        g = self.reg.create_group("Trading")
        out = self.reg.set_members(g["id"], add=[A1, self.w[A2]])
        self.assertEqual(out["count"], 2)
        out = self.reg.set_members(g["id"], add=[A1])          # idempotent
        self.assertEqual(out["count"], 2)
        out = self.reg.set_members(g["id"], remove=[A1])
        self.assertEqual(out["wallet_ids"], [self.w[A2]])
        with self.assertRaises(WalletError):
            self.reg.set_members(g["id"], add=[A3, "0x" + "ab" * 20])
        self.assertEqual(self.reg.list_groups()[0]["count"], 1)  # atomic-ish

    def test_move_wallet_between_groups(self):
        a, b = self.reg.create_group("A"), self.reg.create_group("B")
        self.reg.set_members(a["id"], add=[A1])
        w = self.reg.move_wallet(A1, a["id"], b["id"])
        self.assertEqual(w["group_ids"], [b["id"]])
        groups = {g["name"]: g["count"] for g in self.reg.list_groups()}
        self.assertEqual(groups, {"A": 0, "B": 1})
        with self.assertRaises(WalletError):
            self.reg.move_wallet(A1, a["id"], "g_missing")

    def test_group_filtering_and_view(self):
        main, trading = (self.reg.create_group("Main Wallets"),
                         self.reg.create_group("Trading"))
        self.reg.set_members(main["id"], add=[A1, A2, A3])
        self.reg.set_members(trading["id"], add=[A4])
        self.assertEqual({w["address"] for w in
                          self.reg.list_wallets(group_id=main["id"])},
                         {A1, A2, A3})
        self.assertEqual([w["address"] for w in
                          self.reg.list_wallets(group_id=trading["id"])], [A4])
        self.assertEqual(self.reg.count(), 4)                  # unfiltered

    def test_wallet_in_multiple_groups(self):
        a, b = self.reg.create_group("A"), self.reg.create_group("B")
        self.reg.set_members(a["id"], add=[A1])
        self.reg.set_members(b["id"], add=[A1])
        self.assertEqual(set(self.reg.get(A1)["group_ids"]),
                         {a["id"], b["id"]})

    def test_groups_persist_across_restart(self):
        g = self.reg.create_group("Persisted")
        self.reg.set_members(g["id"], add=[A1, A3])
        self.store.close()
        store2, reg2 = self.env.open()
        try:
            got = reg2.list_groups()
            self.assertEqual(got[0]["name"], "Persisted")
            self.assertEqual(got[0]["count"], 2)
        finally:
            store2.close()


# ── 5-8. ACTIVE WALLET + TOOL / POLICY INTEGRATION ─────────────────────────
class TestSelectionAndTools(unittest.TestCase):
    """Real ToolRegistry.execute() + real TransactionManager + real policy."""

    def _build(self, mode="CONFIRM", **policy_kw):
        self.env = _Env()
        store = Store(self.env.db)
        self.addCleanup(store.close)
        ks = SecureKeyStore(store, master_secret="test-master")
        mgr = TransactionManager(
            store, keystore=ks, policy=TransactionPolicyEngine(
                PolicyConfig(mode=mode, **policy_kw)))
        reg = WalletRegistry(store, keystore=ks,
                             master_key_path=self.env.master)
        reg.import_wallets(f"Alpha {K1}\nBeta {K2}\nWatcher {A3}")
        tools = ToolRegistry(policy=Policy(granted=[
            "read", "low_risk_write", "browser_action", "financial_action"]))
        # Deliberately NO wallets/manager on the ToolContext: the tools must
        # resolve both from what was bound at registration (that is exactly
        # the situation of the agent tool loop's context).
        register_web3_tools(tools, manager=mgr, wallets=reg)
        return tools, mgr, reg, ToolContext()

    def test_active_wallet_selection_switches(self):
        _, _, reg, _ = self._build()
        self.assertEqual(reg.active()["address"], A1)
        reg.set_active(A2)
        self.assertEqual(reg.active()["address"], A2)
        self.assertEqual(sum(1 for w in reg.list_wallets() if w["active"]), 1)
        reg.set_active(reg.get(A1)["id"])
        self.assertEqual(reg.active()["address"], A1)
        with self.assertRaises(WalletError):
            reg.set_active("w_missing")

    def test_tx_prepare_uses_selected_wallet_confirm_mode(self):
        tools, mgr, reg, ctx = self._build("CONFIRM")
        reg.set_active(A2)
        out = tools.execute("tx_prepare", {"to": RECIPIENT,
                                           "value_wei": 10 ** 15}, ctx=ctx)
        rec = out["result"]
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["from"], A2)
        self.assertEqual(rec["wallet"]["address"], A2)
        self.assertEqual(rec["wallet"]["name"], "Beta")
        # CONFIRM policy preserved: parked, never signed
        self.assertTrue(rec["requires_approval"])
        self.assertFalse(rec["signed"])
        self.assertEqual(mgr.status(rec["tx_id"])["status"], "PREPARED")

    def test_explicit_wallet_overrides_selection(self):
        tools, mgr, reg, ctx = self._build("CONFIRM")
        out = tools.execute("tx_prepare", {"to": RECIPIENT, "value_wei": 1,
                                           "from": reg.get(A2)["id"]},
                            ctx=ctx)
        self.assertEqual(out["result"]["from"], A2)

    def test_auto_mode_signs_with_selected_wallet_key(self):
        tools, mgr, reg, ctx = self._build("AUTO")
        reg.set_active(A2)
        real_decrypt = SecureKeyStore.decrypt
        asked = []

        def spy(ks, name):
            asked.append(name)
            return real_decrypt(ks, name)
        with patch("astra.web3.raw_tx.receipt", return_value=None), \
                patch("astra.web3.raw_tx.broadcast", return_value="0xfeed"), \
                patch("astra.web3.raw_tx.get_nonce", return_value=7) as gn, \
                patch("astra.web3.raw_tx.get_gas_price", return_value=10 ** 9), \
                patch.object(SecureKeyStore, "decrypt", spy):
            out = tools.execute("tx_prepare", {"to": RECIPIENT,
                                               "value_wei": 10 ** 15}, ctx=ctx)
        rec = out["result"]
        self.assertTrue(rec["signed"], rec)
        self.assertEqual(rec["status"], "BROADCAST")
        self.assertEqual(rec["from"], A2)
        # the SELECTED wallet's key (and only that one) was unsealed ...
        self.assertEqual(asked, [A2])
        # ... and the nonce was read for the selected wallet's address
        self.assertEqual(gn.call_args[0][1], A2)
        self.assertEqual(mgr.status(rec["tx_id"])["nonce"], 7)

    def test_policy_allowlist_blocks_unlisted_wallet(self):
        tools, mgr, reg, ctx = self._build("AUTO", allowed_wallets=frozenset(
            {A1.lower()[2:]}))
        reg.set_active(A2)
        out = tools.execute("tx_prepare", {"to": RECIPIENT, "value_wei": 1},
                            ctx=ctx)
        rec = out["result"]
        self.assertFalse(rec["ok"])
        self.assertFalse(rec.get("signed"))
        self.assertIn("allowlisted", rec["error"])
        # the allowlisted wallet is still permitted
        reg.set_active(A1)
        with patch("astra.web3.raw_tx.receipt", return_value=None), \
                patch("astra.web3.raw_tx.broadcast", return_value="0x1"), \
                patch("astra.web3.raw_tx.get_nonce", return_value=0), \
                patch("astra.web3.raw_tx.get_gas_price", return_value=10 ** 9):
            ok = tools.execute("tx_prepare", {"to": RECIPIENT,
                                              "value_wei": 1}, ctx=ctx)
        self.assertTrue(ok["result"]["signed"])

    def test_policy_value_limit_still_enforced(self):
        tools, _, _, ctx = self._build("AUTO", max_tx_value_wei=100)
        out = tools.execute("tx_prepare", {"to": RECIPIENT, "value_wei": 101},
                            ctx=ctx)
        self.assertFalse(out["result"]["ok"])
        self.assertFalse(out["result"].get("signed"))

    def test_watch_only_and_unknown_wallets_are_refused(self):
        tools, mgr, reg, ctx = self._build("AUTO")
        for frm in (A3, "0x" + "ab" * 20, "w_missing"):
            out = tools.execute("tx_prepare", {"to": RECIPIENT, "value_wei": 1,
                                               "from": frm}, ctx=ctx)
            rec = out["result"]
            self.assertFalse(rec["ok"], frm)
            self.assertFalse(rec["signed"])
        self.assertEqual(mgr.list(), [])          # nothing was even prepared

    def test_no_active_wallet_is_refused_not_defaulted(self):
        tools, mgr, reg, ctx = self._build("AUTO")
        reg.store.exec("UPDATE web3_wallets SET active=0")
        out = tools.execute("tx_prepare", {"to": RECIPIENT, "value_wei": 1},
                            ctx=ctx)
        self.assertFalse(out["result"]["ok"])
        self.assertIn("no active wallet", out["result"]["error"])

    def test_token_balance_defaults_to_active_wallet(self):
        tools, _, reg, ctx = self._build()
        reg.set_active(A2)
        with patch("astra.web3.rpc.erc20_balance", return_value=5 * 10 ** 18) as eb:
            out = tools.execute("token_balance", {"token": "0x" + "cc" * 20,
                                                  "network": "ethereum"},
                                ctx=ctx)
        self.assertTrue(out["result"]["ok"], out)
        self.assertEqual(out["result"]["address"], A2)
        self.assertEqual(eb.call_args[0][2], A2)

    def test_tool_results_never_contain_secrets(self):
        tools, mgr, reg, ctx = self._build("CONFIRM")
        blobs = []
        for name, args in (
                ("tx_prepare", {"to": RECIPIENT, "value_wei": 1}),
                ("tx_status", {"tx_id": "tx-none"}),
                ("token_balance", {"token": "0x" + "cc" * 20})):
            with patch("astra.web3.rpc.erc20_balance", return_value=1):
                blobs.append(json.dumps(tools.execute(name, args, ctx=ctx),
                                        default=str))
        blobs.append(json.dumps(tools.list() if hasattr(tools, "list") else [],
                                default=str))
        joined = "".join(blobs)
        for k in KEYS:
            self.assertNotIn(k, joined)

    def test_legacy_default_keystore_path_still_works_without_wallets(self):
        """No registered wallets → the pre-registry 'default' behaviour is
        unchanged (existing deployments keep working)."""
        env = _Env()
        store = Store(env.db)
        self.addCleanup(store.close)
        ks = SecureKeyStore(store, master_secret="m")
        ks.store_key("default", "01" * 32)
        mgr = TransactionManager(store, keystore=ks,
                                 policy=TransactionPolicyEngine(
                                     PolicyConfig(mode="CONFIRM")))
        reg = WalletRegistry(store, keystore=ks, master_key_path=env.master)
        tools = ToolRegistry(policy=Policy(granted=[
            "read", "low_risk_write", "browser_action", "financial_action"]))
        register_web3_tools(tools, manager=mgr, wallets=reg)
        out = tools.execute("tx_prepare", {"to": RECIPIENT, "value_wei": 1},
                            ctx=ToolContext())
        self.assertTrue(out["result"]["ok"])
        self.assertEqual(out["result"]["from"], "default")


class TestAiLayerNeverSeesSecrets(unittest.TestCase):
    """Structural guard: the Gateway / AgentRouter / ChatPipeline code paths
    (astra/ai) have no dependency on the wallet registry or keystore."""

    def test_ai_package_has_no_wallet_or_keystore_access(self):
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "astra", "ai")
        pat = re.compile(r"wallet_registry|WalletRegistry|web3\.wallets|"
                         r"SecureKeyStore|keystore|\.decrypt\(|private_key")
        hits = []
        for dp, _, files in os.walk(root):
            for f in files:
                if f.endswith(".py"):
                    src = open(os.path.join(dp, f), encoding="utf-8").read()
                    if pat.search(src):
                        hits.append(os.path.join(dp, f))
        self.assertEqual(hits, [])


# ── Astra Wallets: single wallet vs one group per multi-wallet import ───────
class TestImportGrouping(RegistryCase):
    def test_single_import_is_standalone_and_creates_no_group(self):
        res = self.reg.import_wallets(K1, group_name="Ignored")
        self.assertEqual(res["imported"], 1)
        self.assertNotIn("group", res)
        d = self.reg.describe()
        self.assertEqual((len(d["wallets"]), d["groups"]), (1, []))
        self.assertEqual(d["wallets"][0]["group_ids"], [])

    def test_multi_import_creates_exactly_one_group_with_all_wallets(self):
        res = self.reg.import_wallets(f"{K1}\n{K2}\n{K3}", group_name="Binance Wallets")
        self.assertEqual(res["imported"], 3)
        self.assertEqual(res["group"]["name"], "Binance Wallets")
        d = self.reg.describe()
        self.assertEqual(len(d["groups"]), 1)
        g = d["groups"][0]
        self.assertEqual((g["name"], g["count"]), ("Binance Wallets", 3))
        self.assertEqual(sorted(g["wallet_ids"]), sorted(w["id"] for w in d["wallets"]))
        self.assertTrue(all(w["group_ids"] == [g["id"]] for w in d["wallets"]))

    def test_multi_import_without_a_name_stays_backward_compatible(self):
        self.reg.import_wallets(f"{K1}\n{K2}")
        self.assertEqual(self.reg.list_groups(), [])

    def test_group_is_only_made_for_wallets_that_really_import(self):
        self.reg.import_wallets(K1)
        # one duplicate + one invalid + one new => a single NEW wallet: no group
        res = self.reg.import_wallets(f"{K1}\nbad\n{K2}", group_name="Nope")
        self.assertEqual(res["imported"], 1)
        self.assertEqual(self.reg.list_groups(), [])

    def test_dry_run_never_creates_a_group(self):
        res = self.reg.import_wallets(f"{K1}\n{K2}", dry_run=True, group_name="Dry")
        self.assertTrue(res["dry_run"])
        self.assertEqual((self.reg.list_groups(), self.reg.count()), ([], 0))

    def test_existing_wallets_and_groups_are_kept(self):
        self.reg.import_wallets(K1)
        old = self.reg.create_group("Old")
        self.reg.import_wallets(f"{K2}\n{K3}", group_name="Exchange Wallets")
        d = self.reg.describe()
        self.assertEqual([g["name"] for g in d["groups"]], ["Old", "Exchange Wallets"])
        self.assertEqual(len(d["wallets"]), 3)
        self.assertEqual(d["groups"][0]["id"], old["id"])

    def test_clashing_or_bad_group_name_imports_nothing(self):
        self.reg.create_group("Taken")
        with self.assertRaises(WalletError):
            self.reg.import_wallets(f"{K1}\n{K2}", group_name="taken")
        with self.assertRaises(WalletError):
            self.reg.import_wallets(f"{K1}\n{K2}", group_name="x" * 500)
        self.assertEqual(self.reg.count(), 0)

    def test_deleting_the_group_keeps_the_wallets_as_standalone(self):
        res = self.reg.import_wallets(f"{K1}\n{K2}", group_name="Temp")
        self.reg.delete_group(res["group"]["id"])
        d = self.reg.describe()
        self.assertEqual((len(d["wallets"]), d["groups"]), (2, []))
        self.assertTrue(all(w["group_ids"] == [] for w in d["wallets"]))
        self.assertEqual(self.reg._ks().decrypt(A1), "0x" + K1)   # keys untouched

    def test_created_wallet_is_standalone_and_listed(self):
        w, _ = self.reg.create_wallet("Fresh")
        d = self.reg.describe()
        self.assertEqual([x["id"] for x in d["wallets"]], [w["id"]])
        self.assertEqual((d["wallets"][0]["group_ids"], d["groups"]), ([], []))

    def test_groups_and_membership_survive_a_restart(self):
        self.reg.import_wallets(f"{K1}\n{K2}", group_name="Persisted")
        before = self.reg.describe()
        self.store.close()
        store2, reg2 = self.env.open()
        try:
            self.assertEqual(reg2.describe(), before)
        finally:
            store2.close()
        self.store, self.reg = self.env.open()      # for tearDown

    def test_no_secret_in_the_wallet_list_payload(self):
        self.reg.import_wallets(f"{K1}\n{K2}", group_name="G")
        _, secret = self.reg.create_wallet("Fresh")
        blob = json.dumps(self.reg.describe())
        for s in (K1, K2, secret, secret[2:], "keystore_name", "private"):
            self.assertNotIn(s, blob)


# ── delete one wallet ───────────────────────────────────────────────────────
class TestDeleteWallet(RegistryCase):
    def _keys(self):
        return [r["name"] for r in self.store.fetch(
            "SELECT name FROM web3_keys ORDER BY id")]

    def _seed(self):
        self.reg.import_wallets(f"Alpha {K1}\nBeta {K2}\nGamma {K3}",
                                group_name="Exchange")
        self.reg.import_wallets(f"Solo {K4}")
        ids = {w["name"]: w["id"] for w in self.reg.list_wallets()}
        return ids

    def test_delete_removes_wallet_memberships_and_key_record(self):
        ids = self._seed()
        self.assertEqual(sorted(self._keys()), sorted([A1, A2, A3, A4]))
        out = self.reg.delete_wallet(ids["Beta"])
        self.assertEqual([w["name"] for w in out["wallets"]],
                         ["Alpha", "Gamma", "Solo"])
        self.assertIsNone(self.reg.get(ids["Beta"]))
        self.assertIsNone(self.reg.get(A2))
        self.assertNotIn(A2, self._keys(), "no orphaned encrypted key record")
        self.assertEqual(sorted(self._keys()), sorted([A1, A3, A4]))
        rows = self.store.fetch(
            "SELECT * FROM web3_wallet_group_members WHERE wallet_id=?",
            (ids["Beta"],))
        self.assertEqual(rows, [], "its group memberships are gone")

    def test_delete_keeps_group_and_every_other_wallet(self):
        ids = self._seed()
        out = self.reg.delete_wallet(ids["Beta"])
        (g,) = out["groups"]
        self.assertEqual((g["name"], g["count"]), ("Exchange", 2))
        self.assertEqual(g["wallet_ids"], [ids["Alpha"], ids["Gamma"]])
        for n in ("Alpha", "Gamma", "Solo"):
            self.assertIsNotNone(self.reg.get(ids[n]))
        # other wallets can still be decrypted (their keys were not touched)
        ks = self.reg._ks()
        self.assertEqual(ks.decrypt(A1), "0x" + K1)
        self.assertEqual(ks.decrypt(A4), "0x" + K4)
        with self.assertRaises(Exception):
            ks.decrypt(A2)

    def test_delete_last_member_leaves_an_empty_group_not_a_deleted_one(self):
        ids = self._seed()
        for n in ("Alpha", "Beta", "Gamma"):
            out = self.reg.delete_wallet(ids[n])
        self.assertEqual([(g["name"], g["count"]) for g in out["groups"]],
                         [("Exchange", 0)])

    def test_delete_by_address_and_watch_only(self):
        self.reg.import_wallets(f"Keyed {K1}\nWatch {RECIPIENT}")
        w = [x for x in self.reg.list_wallets() if x["source"] == "watch"][0]
        self.reg.delete_wallet(RECIPIENT.upper().replace("0X", "0x"))
        self.assertIsNone(self.reg.get(w["id"]))
        self.assertEqual(self._keys(), [A1], "watch-only had no key; others kept")
        self.reg.delete_wallet(A1)
        self.assertEqual((self.reg.count(), self._keys()), (0, []))

    def test_unknown_wallet_is_refused_and_changes_nothing(self):
        self._seed()
        for ref in ("w_nope", "", "0x" + "9" * 40):
            with self.assertRaises(WalletError):
                self.reg.delete_wallet(ref)
        self.assertEqual(self.reg.count(), 4)
        self.assertEqual(len(self._keys()), 4)

    def test_deleting_the_active_wallet_does_not_promote_another(self):
        ids = self._seed()
        self.assertEqual(self.reg.active()["id"], ids["Alpha"])
        out = self.reg.delete_wallet(ids["Alpha"])
        self.assertIsNone(out["active_id"])
        self.assertIsNone(out["active_address"])
        with self.assertRaises(WalletError):
            self.reg.resolve("")               # signing tools refuse, not default
        # deleting a NON-active wallet keeps the selection
        self.reg.set_active(ids["Gamma"])
        self.reg.delete_wallet(ids["Solo"])
        self.assertEqual(self.reg.active()["id"], ids["Gamma"])

    def test_deleted_wallet_cannot_be_resolved_for_signing(self):
        ids = self._seed()
        self.reg.delete_wallet(ids["Beta"])
        with self.assertRaises(WalletError):
            self.reg.resolve(A2, need_signer=True)

    def test_same_address_can_be_imported_again_after_delete(self):
        ids = self._seed()
        self.reg.delete_wallet(ids["Beta"])
        res = self.reg.import_wallets(f"Beta again {K2}")
        self.assertEqual((res["imported"], res["rejected"]), (1, 0))
        self.assertEqual(self.reg._ks().decrypt(A2), "0x" + K2)

    def test_delete_is_atomic(self):
        ids = self._seed()
        real = self.store.table_exists

        def boom(name):
            raise RuntimeError("disk on fire " + K2)
        self.store.table_exists = boom
        try:
            with self.assertRaises(WalletError) as cm:
                self.reg.delete_wallet(ids["Beta"])
        finally:
            self.store.table_exists = real
        self.assertNotIn(K2, str(cm.exception))          # no exception text
        self.assertIsNotNone(self.reg.get(ids["Beta"]))  # wallet still there
        self.assertIn(A2, self._keys())                  # ...with its key
        self.assertEqual(self.reg.list_groups()[0]["count"], 3)   # ...and membership

    def test_persists_across_restart(self):
        ids = self._seed()
        self.reg.delete_wallet(ids["Beta"])
        self.reg.delete_group(self.reg.list_groups()[0]["id"])
        self.store.close()
        self.store, self.reg = self.env.open()
        d = self.reg.describe()
        self.assertEqual([w["name"] for w in d["wallets"]],
                         ["Alpha", "Gamma", "Solo"])
        self.assertEqual(d["groups"], [])

    def test_result_and_state_never_contain_secrets(self):
        ids = self._seed()
        blob = json.dumps(self.reg.delete_wallet(ids["Beta"]))
        for k in KEYS:
            self.assertNotIn(k, blob)
        self.assertNotIn("keystore", blob)
        self.assertNotIn("private", blob)

    def test_group_delete_and_wallet_delete_are_separate_operations(self):
        ids = self._seed()
        gid = self.reg.list_groups()[0]["id"]
        self.reg.delete_group(gid)
        self.assertEqual(self.reg.count(), 4)              # group delete: wallets stay
        self.assertEqual(len(self._keys()), 4)
        self.reg.delete_wallet(ids["Alpha"])
        self.assertEqual(self.reg.count(), 3)              # wallet delete: one wallet
        self.assertEqual(self.reg.list_groups(), [])


# ── HTTP API (neutral router, no socket) ────────────────────────────────────
class TestWalletApi(unittest.TestCase):
    def setUp(self):
        from astra.web import AstraSite, Request, WebApp
        from tests.helpers import make_stack
        self.Request = Request
        self.stack = make_stack()
        self.env = _Env()
        self.stack["wallet_registry"]._master_key_path = self.env.master
        self.site = AstraSite(("127.0.0.1", 0), self.stack["store"],
                              self.stack["agent"], stack=self.stack)
        self.app = WebApp(self.site)
        self.addCleanup(self.stack["store"].close)

    def call(self, method, path, body=None):
        resp = self.app.handle(self.Request(method, path, body=body or {}))
        return resp, json.loads(resp.body.decode("utf-8"))

    def test_import_list_select_flow(self):
        r, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": f"{K1}\n{K2}\nnope"})
        self.assertEqual(r.status, 200)
        self.assertEqual((j["data"]["imported"], j["data"]["rejected"]), (2, 1))
        self.assertEqual(len(j["data"]["wallets"]), 2)     # refreshed list
        r, j = self.call("GET", "/api/v1/web3/wallets")
        addrs = [w["address"] for w in j["data"]["wallets"]]
        self.assertEqual(addrs, [A1, A2])
        wid = j["data"]["wallets"][1]["id"]
        r, j = self.call("POST", f"/api/v1/web3/wallets/{wid}/select")
        self.assertEqual(j["data"]["active_address"], A2)
        r, j = self.call("POST", "/api/v1/web3/wallets/w_nope/select")
        self.assertEqual(r.status, 400)

    def test_import_dry_run_endpoint(self):
        r, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": f"{K1}\nbad", "dry_run": True})
        self.assertEqual([x["status"] for x in j["data"]["results"]],
                         ["valid", "invalid"])
        _, j = self.call("GET", "/api/v1/web3/wallets")
        self.assertEqual(j["data"]["wallets"], [])

    def test_create_reveals_key_once_no_store(self):
        r, j = self.call("POST", "/api/v1/web3/wallets/create",
                         {"name": "Fresh"})
        self.assertEqual(r.status, 201)
        secret = j["data"]["reveal"]["private_key"]
        self.assertEqual(private_to_address(int(secret, 16)),
                         j["data"]["wallet"]["address"])
        self.assertEqual(r.cache, "no-store")
        # never again, on any read path
        for method, path, body in (
                ("GET", "/api/v1/web3/wallets", None),
                ("POST", "/api/v1/web3/wallets/import", {"text": K1}),
                ("POST", "/api/v1/web3/wallet-groups", {"name": "G"})):
            _, j2 = self.call(method, path, body)
            self.assertNotIn(secret, json.dumps(j2))
            self.assertNotIn(secret[2:], json.dumps(j2))

    def test_group_endpoints(self):
        self.call("POST", "/api/v1/web3/wallets/import",
                  {"text": f"{K1}\n{K2}"})
        r, j = self.call("POST", "/api/v1/web3/wallet-groups",
                         {"name": "Trading"})
        self.assertEqual(r.status, 201)
        gid = j["data"]["group"]["id"]
        r, j = self.call("POST", f"/api/v1/web3/wallet-groups/{gid}/members",
                         {"add": [A1, A2]})
        self.assertEqual(j["data"]["groups"][0]["count"], 2)
        r, j = self.call("POST", f"/api/v1/web3/wallet-groups/{gid}/members",
                         {"remove": [A2]})
        self.assertEqual(j["data"]["groups"][0]["count"], 1)
        r, j = self.call("POST", f"/api/v1/web3/wallet-groups/{gid}/rename",
                         {"name": "Trading 2"})
        self.assertEqual(j["data"]["groups"][0]["name"], "Trading 2")
        r, j = self.call("POST", "/api/v1/web3/wallet-groups", {"name": "DeFi"})
        gid2 = j["data"]["group"]["id"]
        r, j = self.call("POST", f"/api/v1/web3/wallets/{A1}/move",
                         {"from_group": gid, "to_group": gid2})
        counts = {g["name"]: g["count"] for g in j["data"]["groups"]}
        self.assertEqual(counts, {"Trading 2": 0, "DeFi": 1})
        r, j = self.call("POST", f"/api/v1/web3/wallet-groups/{gid}/delete")
        self.assertEqual([g["name"] for g in j["data"]["groups"]], ["DeFi"])
        self.assertEqual(len(j["data"]["wallets"]), 2)
        r, j = self.call("POST", "/api/v1/web3/wallet-groups", {"name": "defi"})
        self.assertEqual(r.status, 400)                    # duplicate name

    def test_http_multi_import_makes_one_named_group_and_get_rebuilds_it(self):
        r, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": f"{K1}\n{K2}\n{K3}", "group_name": "Binance Wallets"})
        self.assertEqual(r.status, 200)
        self.assertEqual(j["data"]["group"]["name"], "Binance Wallets")
        self.assertEqual([g["count"] for g in j["data"]["groups"]], [3])
        _, j2 = self.call("GET", "/api/v1/web3/wallets")           # canonical read
        self.assertEqual(len(j2["data"]["wallets"]), 3)
        self.assertEqual([(g["name"], g["count"]) for g in j2["data"]["groups"]],
                         [("Binance Wallets", 3)])
        for k in (K1, K2, K3):
            self.assertNotIn(k, json.dumps(j2))

    def test_http_single_import_and_create_are_standalone(self):
        _, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": K1, "group_name": "Whatever"})
        self.assertEqual(j["data"]["groups"], [])
        r, j = self.call("POST", "/api/v1/web3/wallets/create", {"name": "Fresh"})
        self.assertEqual(r.status, 201)
        self.assertEqual(j["data"]["groups"], [])
        self.assertEqual(len(j["data"]["wallets"]), 2)             # nothing was lost
        _, j2 = self.call("GET", "/api/v1/web3/wallets")
        self.assertNotIn(j["data"]["reveal"]["private_key"], json.dumps(j2))

    def test_http_group_delete_keeps_wallets(self):
        _, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": f"{K1}\n{K2}", "group_name": "Temp"})
        gid = j["data"]["groups"][0]["id"]
        _, j = self.call("POST", f"/api/v1/web3/wallet-groups/{gid}/delete")
        self.assertEqual((j["data"]["groups"], len(j["data"]["wallets"])), ([], 2))


    def _seed_http(self):
        _, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": f"A {K1}\nB {K2}\nC {K3}",
                          "group_name": "Exchange Wallets"})
        self.assertEqual(j["data"]["group"]["name"], "Exchange Wallets")
        self.call("POST", "/api/v1/web3/wallets/import", {"text": f"Solo {K4}"})
        _, j = self.call("GET", "/api/v1/web3/wallets")
        return {w["name"]: w["id"] for w in j["data"]["wallets"]}

    def test_http_delete_wallet_returns_registry_and_persists(self):
        ids = self._seed_http()
        r, j = self.call("DELETE", f"/api/v1/web3/wallets/{ids['B']}")
        self.assertEqual(r.status, 200)
        self.assertEqual([w["name"] for w in j["data"]["wallets"]],
                         ["A", "C", "Solo"])
        self.assertEqual([(g["name"], g["count"]) for g in j["data"]["groups"]],
                         [("Exchange Wallets", 2)])
        for k in KEYS:
            self.assertNotIn(k, json.dumps(j))
        _, j2 = self.call("GET", "/api/v1/web3/wallets")          # "reload"
        self.assertEqual(j2["data"]["wallets"], j["data"]["wallets"])
        self.assertEqual(j2["data"]["groups"], j["data"]["groups"])
        keys = self.stack["store"].fetch("SELECT name FROM web3_keys")
        self.assertNotIn(A2, [k["name"] for k in keys])
        self.assertEqual(len(keys), 3)

    def test_http_delete_wallet_post_alias_and_by_address(self):
        ids = self._seed_http()
        r, j = self.call("POST", f"/api/v1/web3/wallets/{ids['A']}/delete")
        self.assertEqual((r.status, len(j["data"]["wallets"])), (200, 3))
        r, j = self.call("DELETE", f"/api/v1/web3/wallets/{A4}")
        self.assertEqual((r.status, len(j["data"]["wallets"])), (200, 2))

    def test_http_delete_unknown_wallet_is_a_generic_400(self):
        self._seed_http()
        r, j = self.call("DELETE", "/api/v1/web3/wallets/w_nope")
        self.assertEqual(r.status, 400)
        self.assertEqual((j["ok"], j["error"], j["error_code"]),
                         (False, "wallet not found", "wallet_error"))
        _, j2 = self.call("GET", "/api/v1/web3/wallets")
        self.assertEqual(len(j2["data"]["wallets"]), 4)

    def test_http_delete_wallet_then_group_flows_stay_separate(self):
        ids = self._seed_http()
        _, j = self.call("GET", "/api/v1/web3/wallets")
        gid = j["data"]["groups"][0]["id"]
        _, j = self.call("POST", f"/api/v1/web3/wallet-groups/{gid}/delete")
        self.assertEqual((j["data"]["groups"], len(j["data"]["wallets"])), ([], 4))
        _, j = self.call("DELETE", f"/api/v1/web3/wallets/{ids['C']}")
        self.assertEqual([w["name"] for w in j["data"]["wallets"]],
                         ["A", "B", "Solo"])

    def test_http_delete_wallet_requires_the_operator_token(self):
        ids = self._seed_http()
        self.site.operator_token = "tok"
        r, _ = self.call("DELETE", f"/api/v1/web3/wallets/{ids['A']}")
        self.assertEqual(r.status, 401)
        self.site.operator_token = None
        _, j = self.call("GET", "/api/v1/web3/wallets")
        self.assertEqual(len(j["data"]["wallets"]), 4, "unauthorised call deleted nothing")

    def test_http_delete_wallet_emits_no_secret_to_events_or_logs(self):
        ids = self._seed_http()
        before = self.stack["events"].last_id()
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        logging.getLogger().addHandler(h)
        old = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            self.call("DELETE", f"/api/v1/web3/wallets/{ids['B']}")
        finally:
            logging.getLogger().removeHandler(h)
            logging.getLogger().setLevel(old)
        ev = json.dumps(self.stack["events"].since(before), default=str)
        for k in KEYS:
            self.assertNotIn(k, ev)
            self.assertNotIn(k, buf.getvalue())

    def test_errors_are_generic_and_do_not_echo_input(self):
        r, j = self.call("POST", "/api/v1/web3/wallets/import",
                         {"text": "\n".join([K1] * (MAX_IMPORT_LINES + 1))})
        self.assertEqual(r.status, 400)
        self.assertNotIn(K1, json.dumps(j))

    def test_operator_token_gate_applies(self):
        self.site.operator_token = "tok"
        r, _ = self.call("GET", "/api/v1/web3/wallets")
        self.assertEqual(r.status, 401)
        r, _ = self.call("POST", "/api/v1/web3/wallets/create")
        self.assertEqual(r.status, 401)

    def test_no_secret_reaches_events_or_logs_through_api(self):
        before = self.stack["events"].last_id()
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        logging.getLogger().addHandler(h)
        old = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            _, j = self.call("POST", "/api/v1/web3/wallets/create")
            secret = j["data"]["reveal"]["private_key"]
            self.call("POST", "/api/v1/web3/wallets/import", {"text": K1})
        finally:
            logging.getLogger().removeHandler(h)
            logging.getLogger().setLevel(old)
        ev = json.dumps(self.stack["events"].since(before), default=str)
        for s in (secret, secret[2:], K1):
            self.assertNotIn(s, ev)
            self.assertNotIn(s, buf.getvalue())

    def test_redaction_net_still_masks_keys_on_every_other_endpoint(self):
        """The create-wallet reveal is the ONLY hand-built response; the
        generic redact() net must still mask a key on the normal path."""
        from astra.web import json_response
        resp = json_response({"ok": True, "private_key": K1, "x": "0x" + K1})
        body = resp.body.decode()
        self.assertNotIn(K1, body)


if __name__ == "__main__":
    unittest.main()
