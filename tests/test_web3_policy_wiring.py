"""Regression tests: WEB3_* environment/config → runtime PolicyConfig wiring,
safe fallback for an invalid transaction mode, the configurable chain
allowlist, and the tool-level AUTO-mode auto-execution pipeline (tx_prepare
completing sign+broadcast itself when the deterministic policy already says
"allow", never asking for a second per-transaction confirmation).

These close the gap where WEB3_MAX_TX_VALUE_WEI / WEB3_MAX_DAILY_TX_VALUE_WEI /
WEB3_MAX_GAS_LIMIT / WEB3_ALLOWED_RECIPIENTS / WEB3_ALLOWED_CONTRACTS /
WEB3_ALLOWED_WALLETS / WEB3_CHAIN_IDS were documented in .env.example but
never actually reached the runtime policy engine.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from astra.core.config import Config
from astra.store import Store
from astra.bootstrap import build
from astra.web3.policy import (PolicyConfig, TransactionPolicyEngine,
                               TxRequest, normalize_mode, normalize_address)


def _build_with_config(**web3_env) -> dict:
    cfg = Config()
    for k, v in web3_env.items():
        cfg.set(k, v)
    d = tempfile.mkdtemp()
    return build(store=Store(os.path.join(d, "t.db")), config=cfg,
                with_scheduler=False)


class TestModeNormalization(unittest.TestCase):
    def test_valid_modes_pass_through(self):
        self.assertEqual(normalize_mode("confirm"), "CONFIRM")
        self.assertEqual(normalize_mode("Auto"), "AUTO")
        self.assertEqual(normalize_mode("AUTO"), "AUTO")

    def test_invalid_mode_falls_back_to_confirm_never_auto(self):
        for bad in ("", "YOLO", "true", "1", None, "  "):
            self.assertEqual(normalize_mode(bad), "CONFIRM")

    def test_default_is_confirm(self):
        self.assertEqual(PolicyConfig().mode, "CONFIRM")


class TestBootstrapWiresPolicyConfig(unittest.TestCase):
    """The most important regression: every WEB3_* var must actually reach
    the runtime PolicyConfig used by the transaction manager/policy engine —
    not just be defined in .env.example and silently dropped."""

    def test_default_build_is_confirm_and_unrestricted(self):
        stack = _build_with_config()
        cfg = stack["web3_policy"].cfg
        self.assertEqual(cfg.mode, "CONFIRM")
        self.assertEqual(cfg.max_tx_value_wei, 0)
        self.assertEqual(cfg.allowed_recipients, frozenset())

    def test_invalid_mode_env_fails_safe(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="not-a-real-mode")
        self.assertEqual(stack["web3_policy"].mode, "CONFIRM")

    def test_auto_mode_env_loads(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="auto")
        self.assertEqual(stack["web3_policy"].mode, "AUTO")

    def test_max_tx_value_env_reaches_policy_and_blocks(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_MAX_TX_VALUE_WEI="1000")
        eng = stack["web3_policy"]
        self.assertEqual(eng.cfg.max_tx_value_wei, 1000)
        ok = eng.evaluate(TxRequest("0xaa", "0x" + "11" * 20, 1000))
        blocked = eng.evaluate(TxRequest("0xaa", "0x" + "11" * 20, 1001))
        self.assertEqual(ok.verdict, "allow")
        self.assertEqual(blocked.verdict, "block")

    def test_daily_limit_env_reaches_policy(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_MAX_DAILY_TX_VALUE_WEI="5000")
        eng = stack["web3_policy"]
        self.assertEqual(eng.cfg.max_daily_tx_value_wei, 5000)
        blocked = eng.evaluate(TxRequest("0xaa", "0x" + "11" * 20, 1000),
                               spent_today_wei=4500)
        self.assertEqual(blocked.verdict, "block")

    def test_gas_limit_env_reaches_policy(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_MAX_GAS_LIMIT="21000")
        eng = stack["web3_policy"]
        self.assertEqual(eng.cfg.max_gas_limit, 21000)
        blocked = eng.evaluate(TxRequest("0xaa", "0x" + "11" * 20, 1,
                                        gas_limit=50000))
        self.assertEqual(blocked.verdict, "block")

    def test_allowed_recipients_env_reaches_policy(self):
        good = "0x" + "22" * 20
        bad = "0x" + "33" * 20
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_ALLOWED_RECIPIENTS=good)
        eng = stack["web3_policy"]
        self.assertIn(normalize_address(good), eng.cfg.allowed_recipients)
        self.assertEqual(eng.evaluate(TxRequest("0xaa", good, 1)).verdict,
                         "allow")
        self.assertEqual(eng.evaluate(TxRequest("0xaa", bad, 1)).verdict,
                         "block")

    def test_allowed_contracts_env_reaches_policy(self):
        contract = "0x" + "44" * 20
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_ALLOWED_CONTRACTS=contract)
        eng = stack["web3_policy"]
        allowed = eng.evaluate(TxRequest("0xaa", contract, 0,
                                        data_hex="0xabc"))
        blocked = eng.evaluate(TxRequest("0xaa", "0x" + "55" * 20, 0,
                                        data_hex="0xabc"))
        self.assertEqual(allowed.verdict, "allow")
        self.assertEqual(blocked.verdict, "block")

    def test_allowed_wallets_env_reaches_policy(self):
        wallet = "0x" + "66" * 20
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_ALLOWED_WALLETS=wallet)
        eng = stack["web3_policy"]
        allowed = eng.evaluate(TxRequest(wallet, "0x" + "11" * 20, 1))
        blocked = eng.evaluate(TxRequest("0x" + "77" * 20,
                                        "0x" + "11" * 20, 1))
        self.assertEqual(allowed.verdict, "allow")
        self.assertEqual(blocked.verdict, "block")

    def test_chain_ids_env_reaches_policy(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO",
                                   WEB3_CHAIN_IDS="137")
        eng = stack["web3_policy"]
        self.assertEqual(eng.cfg.allowed_chain_ids, frozenset({137}))
        allowed = eng.evaluate(TxRequest("0xaa", "0x" + "11" * 20, 1,
                                        chain_id=137))
        blocked = eng.evaluate(TxRequest("0xaa", "0x" + "11" * 20, 1,
                                        chain_id=1))
        self.assertEqual(allowed.verdict, "allow")
        self.assertEqual(blocked.verdict, "block")

    def test_unconfigured_chain_ids_keeps_default_supported_set(self):
        stack = _build_with_config(WEB3_TRANSACTION_MODE="AUTO")
        eng = stack["web3_policy"]
        self.assertEqual(eng.evaluate(
            TxRequest("0xaa", "0x" + "11" * 20, 1, chain_id=1)).verdict,
            "allow")
        self.assertEqual(eng.evaluate(
            TxRequest("0xaa", "0x" + "11" * 20, 1, chain_id=999999)).verdict,
            "block")


class TestSetModePreservesConfig(unittest.TestCase):
    """set_mode() (the only LLM-unreachable, operator-only mutator) must not
    silently drop limits/allowlists it wasn't explicitly told to change."""

    def test_set_mode_keeps_limits_and_chain_allowlist(self):
        eng = TransactionPolicyEngine(PolicyConfig(
            mode="CONFIRM", max_tx_value_wei=42,
            allowed_chain_ids=frozenset({137})))
        eng.set_mode("AUTO")
        self.assertEqual(eng.cfg.max_tx_value_wei, 42)
        self.assertEqual(eng.cfg.allowed_chain_ids, frozenset({137}))
        self.assertEqual(eng.mode, "AUTO")


class TestAutoModeToolAutoExecutes(unittest.TestCase):
    """End-to-end: in AUTO mode, the tx_prepare *tool* (what the orchestrator
    actually calls) must complete sign+broadcast itself for an
    already-policy-authorized send — never leaving it stuck at PREPARED and
    never asking for a second per-transaction confirmation. In CONFIRM mode
    the same tool call must NOT sign — it stays parked for the operator."""

    RECIPIENT = "0x" + "35" * 20

    def _ctx(self, mode):
        from astra.web3.keystore import SecureKeyStore
        from astra.web3.transactions import TransactionManager
        store = Store(":memory:")
        ks = SecureKeyStore(store, master_secret="op")
        ks.store_key("default", "01" * 32)
        mgr = TransactionManager(
            store, keystore=ks,
            policy=TransactionPolicyEngine(PolicyConfig(mode=mode)))

        class Ctx:
            web3_manager = mgr

        return Ctx(), mgr

    def test_auto_mode_tool_signs_and_broadcasts_without_second_confirm(self):
        from astra.web3.tools import tool_tx_prepare
        ctx, mgr = self._ctx("AUTO")
        with patch("astra.web3.txtx.receipt", return_value=None), \
             patch("astra.web3.txtx.broadcast", return_value="0xdeadbeef"), \
             patch("astra.web3.txtx.get_nonce", return_value=0), \
             patch("astra.web3.txtx.get_gas_price", return_value=10 ** 9):
            out = tool_tx_prepare(
                {"to": self.RECIPIENT, "value_wei": 10 ** 15}, ctx)
        self.assertTrue(out["ok"])
        self.assertFalse(out.get("requires_approval"))
        self.assertTrue(out["signed"])
        self.assertEqual(out["status"], "BROADCAST")
        self.assertEqual(out["tx_hash"], "0xdeadbeef")

    def test_confirm_mode_tool_never_signs(self):
        from astra.web3.tools import tool_tx_prepare
        ctx, mgr = self._ctx("CONFIRM")
        out = tool_tx_prepare(
            {"to": self.RECIPIENT, "value_wei": 10 ** 15}, ctx)
        self.assertTrue(out["ok"])
        self.assertTrue(out["requires_approval"])
        self.assertFalse(out["signed"])
        self.assertEqual(out["status"], "PREPARED")
        # confirms the manager truly never signed anything for this tx
        self.assertEqual(mgr.status(out["tx_id"])["status"], "PREPARED")

    def test_auto_mode_tool_blocks_over_policy_limit(self):
        from astra.web3.tools import tool_tx_prepare
        store = Store(":memory:")
        from astra.web3.transactions import TransactionManager
        mgr = TransactionManager(
            store, keystore=None,
            policy=TransactionPolicyEngine(
                PolicyConfig(mode="AUTO", max_tx_value_wei=10 ** 15)))

        class Ctx:
            web3_manager = mgr

        out = tool_tx_prepare(
            {"to": self.RECIPIENT, "value_wei": 10 ** 15 + 1}, Ctx())
        self.assertFalse(out["ok"])
        self.assertEqual(out["decision"]["verdict"], "block")
        # nothing persisted for a blocked send
        self.assertEqual(mgr.list(), [])


if __name__ == "__main__":
    unittest.main()
