"""Regression tests: the REAL ToolRegistry.execute() path for `tx_prepare`,
not just the tool function called directly (see test_web3_policy_wiring.py
for the latter). These close the gap where the generic FINANCIAL_ACTION /
requires_confirmation ask-gate in ToolRegistry silently overrode the
deterministic Web3 transaction policy, so AUTO mode "worked" only when
tool_tx_prepare() was invoked directly and never through the orchestrator's
actual execution path.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from astra.core.context import ToolContext
from astra.core.permissions import Level, Policy
from astra.store import Store
from astra.tools.registry import ToolRegistry
from astra.tools.schemas import Tool
from astra.web3.keystore import SecureKeyStore
from astra.web3.policy import PolicyConfig, TransactionPolicyEngine
from astra.web3.tools import register_web3_tools
from astra.web3.transactions import TransactionManager

RECIPIENT = "0x" + "35" * 20


def _build_registry(mode: str, **policy_config_kw):
    """A ToolRegistry wired exactly like bootstrap.build() wires one, minus
    the unrelated AI-provider/orchestrator plumbing: real Policy, real
    ToolRegistry.execute(), real web3 tools, real TransactionManager."""
    store = Store(":memory:")
    keystore = SecureKeyStore(store, master_secret="test-master")
    keystore.store_key("default", "01" * 32)
    mgr = TransactionManager(
        store, keystore=keystore,
        policy=TransactionPolicyEngine(PolicyConfig(mode=mode, **policy_config_kw)))
    # financial_action must be explicitly granted (operator-controlled,
    # unrelated to WEB3_TRANSACTION_MODE) for tx_prepare to be reachable at
    # all — this is intentional defense in depth, not part of the bug.
    policy = Policy(granted=["read", "low_risk_write", "browser_action",
                             "financial_action"])
    registry = ToolRegistry(policy=policy)
    register_web3_tools(registry, manager=mgr)
    ctx = ToolContext(web3_manager=mgr)
    return registry, mgr, ctx


class TestAutoModeRealToolRegistryPath(unittest.TestCase):
    """Item 12: AUTO + valid tx, through ToolRegistry.execute() (not the
    tool function directly) → no confirmation, tx signed, tx broadcast."""

    def test_auto_valid_tx_signs_and_broadcasts_via_registry_execute(self):
        registry, mgr, ctx = _build_registry("AUTO")
        with patch("astra.web3.txtx.receipt", return_value=None), \
             patch("astra.web3.txtx.broadcast", return_value="0xdeadbeef"), \
             patch("astra.web3.txtx.get_nonce", return_value=0), \
             patch("astra.web3.txtx.get_gas_price", return_value=10 ** 9):
            out = registry.execute(
                "tx_prepare", {"to": RECIPIENT, "value_wei": 10 ** 15}, ctx=ctx)
        # the generic layer let the call through without an "ask" detour
        self.assertEqual(out["decision"], "allow")
        self.assertTrue(out["ok"])
        rec = out["result"]
        self.assertTrue(rec["ok"])
        self.assertFalse(rec.get("requires_approval"))
        self.assertTrue(rec["signed"])
        self.assertEqual(rec["status"], "BROADCAST")
        self.assertEqual(rec["tx_hash"], "0xdeadbeef")
        # and the manager's own record agrees — nothing left dangling
        self.assertEqual(mgr.status(rec["tx_id"])["status"], "BROADCAST")


class TestConfirmModeRealToolRegistryPath(unittest.TestCase):
    """Item 13: CONFIRM + valid tx, through ToolRegistry.execute() →
    confirmation required, no signing, no broadcast."""

    def test_confirm_valid_tx_waits_for_approval_via_registry_execute(self):
        registry, mgr, ctx = _build_registry("CONFIRM")
        out = registry.execute(
            "tx_prepare", {"to": RECIPIENT, "value_wei": 10 ** 15}, ctx=ctx)
        self.assertEqual(out["decision"], "allow")  # tx_prepare itself ran
        self.assertTrue(out["ok"])
        rec = out["result"]
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["requires_approval"])
        self.assertFalse(rec["signed"])
        self.assertEqual(rec["status"], "PREPARED")
        # confirms the manager truly never signed anything for this tx
        self.assertEqual(mgr.status(rec["tx_id"])["status"], "PREPARED")

    def test_flipping_mode_to_auto_never_auto_authorizes_a_waiting_tx(self):
        """Item 9: a tx parked while CONFIRM was in effect must stay gated
        even after the operator later flips the global mode to AUTO."""
        registry, mgr, ctx = _build_registry("CONFIRM")
        out = registry.execute(
            "tx_prepare", {"to": RECIPIENT, "value_wei": 10 ** 15}, ctx=ctx)
        tx_id = out["result"]["tx_id"]
        self.assertEqual(mgr.status(tx_id)["status"], "PREPARED")

        mgr.policy.set_mode("AUTO")   # operator flips the global mode later

        # nothing in the system re-visits this old tx on its own; and even a
        # direct low-level attempt to sign it must still be refused, because
        # the gate is the tx's OWN persisted verdict, not the live mode.
        from astra.web3.policy import TransactionPolicyError
        with self.assertRaises(TransactionPolicyError):
            mgr.sign_and_broadcast(tx_id)
        self.assertEqual(mgr.status(tx_id)["status"], "PREPARED")


class TestAutoModePolicyBlockRealToolRegistryPath(unittest.TestCase):
    """Item 14: AUTO + over-limit / allowlist-violating tx → blocked, no
    signing, no broadcast — even though it reaches ToolRegistry.execute()."""

    def test_auto_over_value_limit_is_blocked(self):
        registry, mgr, ctx = _build_registry(
            "AUTO", max_tx_value_wei=10 ** 15)
        out = registry.execute(
            "tx_prepare", {"to": RECIPIENT, "value_wei": 10 ** 15 + 1}, ctx=ctx)
        self.assertEqual(out["decision"], "allow")  # generic layer let it run
        rec = out["result"]
        self.assertFalse(rec["ok"])                 # web3 policy blocked it
        self.assertEqual(rec["decision"]["verdict"], "block")
        self.assertFalse(rec.get("signed", False))
        self.assertEqual(mgr.list(), [])             # nothing persisted

    def test_auto_recipient_not_allowlisted_is_blocked(self):
        other = "0x" + "99" * 20
        registry, mgr, ctx = _build_registry(
            "AUTO", allowed_recipients=frozenset({"22" * 20}))
        out = registry.execute(
            "tx_prepare", {"to": other, "value_wei": 1}, ctx=ctx)
        rec = out["result"]
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["decision"]["verdict"], "block")
        self.assertFalse(rec.get("signed", False))
        self.assertEqual(mgr.list(), [])


class TestPermissionRegressionUnaffectedTools(unittest.TestCase):
    """Item 15: fixing tx_prepare must NOT accidentally make unrelated
    FINANCIAL_ACTION / SYSTEM_ACTION / ADMIN tools auto-executable. Only a
    tool that explicitly sets confirmation_delegate gets the new behavior;
    everything else keeps the plain ask/deny gate."""

    def _registry(self, granted):
        policy = Policy(granted=granted)
        registry = ToolRegistry(policy=policy)
        registry.register(Tool(
            name="plain_financial_tool", fn=lambda args, ctx: {"ok": True},
            risk=Level.FINANCIAL_ACTION, requires_confirmation=True))
        registry.register(Tool(
            name="plain_system_tool", fn=lambda args, ctx: {"ok": True},
            risk=Level.SYSTEM_ACTION, requires_confirmation=True))
        registry.register(Tool(
            name="plain_admin_tool", fn=lambda args, ctx: {"ok": True},
            risk=Level.ADMIN, requires_confirmation=False))
        return registry

    def test_other_financial_action_tool_still_asks(self):
        registry = self._registry(
            ["read", "low_risk_write", "browser_action", "financial_action"])
        out = registry.execute("plain_financial_tool", {})
        self.assertEqual(out["decision"], "ask")
        self.assertFalse(out["ok"])

    def test_system_action_tool_still_asks_when_granted(self):
        registry = self._registry(
            ["read", "low_risk_write", "browser_action", "system_action"])
        out = registry.execute("plain_system_tool", {})
        self.assertEqual(out["decision"], "ask")

    def test_admin_tool_still_denied_without_grant(self):
        registry = self._registry(["read", "low_risk_write"])
        with self.assertRaises(Exception):
            registry.execute("plain_admin_tool", {})

    def test_tx_prepare_itself_still_denied_without_financial_action_grant(self):
        """The delegate only skips the boolean ask-gate — the underlying
        permission-level check (is financial_action granted at all?) is
        untouched, in either mode."""
        store = Store(":memory:")
        keystore = SecureKeyStore(store, master_secret="test-master")
        keystore.store_key("default", "01" * 32)
        mgr = TransactionManager(
            store, keystore=keystore,
            policy=TransactionPolicyEngine(PolicyConfig(mode="AUTO")))
        policy = Policy(granted=["read", "low_risk_write", "browser_action"])
        registry = ToolRegistry(policy=policy)
        register_web3_tools(registry, manager=mgr)
        ctx = ToolContext(web3_manager=mgr)
        with self.assertRaises(Exception):
            registry.execute(
                "tx_prepare", {"to": RECIPIENT, "value_wei": 1}, ctx=ctx)


if __name__ == "__main__":
    unittest.main()
