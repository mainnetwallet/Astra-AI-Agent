"""Multi-agent system (Phase B).

The AgentManager selects a task specialist per goal; the Orchestrator persists
that selection and a per-step execution_steps trail; AI routing stays in the
AstraRouter (agents are not providers).
"""
from __future__ import annotations

import os
import tempfile
import unittest

from astra.agents import SPECIALISTS, AgentManager
from astra.ai.router import classify


class TestAgentManager(unittest.TestCase):
    def setUp(self):
        self.am = AgentManager()
        self.am.register_many(SPECIALISTS)

    def test_all_specialists_registered(self):
        names = {a["name"] for a in self.am.list()}
        for n in ("general", "research", "browser", "coding",
                  "files", "web3", "airdrop"):
            self.assertIn(n, names)

    def test_selects_web3_for_transaction(self):
        a = self.am.select("send 0.1 eth from my wallet to 0xabc", "web3")
        self.assertEqual(a.name, "web3")

    def test_selects_browser_for_navigation(self):
        a = self.am.select("open the website and fill the form", "browser")
        self.assertEqual(a.name, "browser")

    def test_selects_airdrop_for_campaign(self):
        a = self.am.select("claim the airdrop task on hamster", "web3")
        self.assertEqual(a.name, "airdrop")

    def test_general_is_fallback(self):
        a = self.am.select("hello there", "simple_chat")
        self.assertEqual(a.name, "general")

    def test_route_hint_is_secret_free_and_deterministic(self):
        h1 = self.am.route_hint("check my token balance")
        h2 = self.am.route_hint("check my token balance")
        self.assertEqual(h1, h2)
        self.assertIn("task_type", h1)

    def test_flatten_ok(self):
        flat = SpecialistFlatten(self.am)
        self.assertEqual(len(flat), 7)


class SpecialistFlatten:
    """Len-wrapps the manager to assert __len__."""

    def __init__(self, am):
        self.am = am

    def __len__(self):
        return len(self.am)


class TestTaskClassify(unittest.TestCase):
    def test_task_types(self):
        self.assertEqual(classify("hello"), "simple_chat")
        self.assertEqual(classify("research the top airdrops and write a report"),
                         "research")


class TestOrchestratorPersistsSelection(unittest.TestCase):
    def test_selected_agent_and_steps_persisted(self):
        os.environ["DATA_DIR"] = tempfile.mkdtemp()
        from astra.bootstrap import build
        b = build()
        o = b["orchestrator"]
        res = o.submit("summarize the readme file", sync=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(res["selected_agent"])
        self.assertGreaterEqual(len(res["steps_detail"]), 1)
        self.assertTrue(all(s["status"] for s in res["steps_detail"]))
        self.assertIn("selected_agent", res)
        os.environ.pop("DATA_DIR", None)


if __name__ == "__main__":
    unittest.main()