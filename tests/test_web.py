"""Full-stack HTTP tests: boot the real Astra server on an ephemeral port and
drive it over localhost exactly like the browser would (the way run.py boots)."""
import json
import threading
import unittest
import urllib.request

from tests.helpers import make_agent
from astra.web import AstraServer, AGENT_NAME


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.plugin, cls.agent = make_agent()
        cls.server = AstraServer(("127.0.0.1", 0), cls.store, cls.agent,
                                 cls.agent.plugins)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def setUp(self):
        for a in self.plugin.list_airdrops():
            self.plugin.delete_airdrop(a["id"])

    @staticmethod
    def req(method, path, body=None, base=None):
        url = (base or TestWeb.base) + path
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8")
                result = (e.code, (json.loads(raw) if raw else {}))
            finally:
                e.close()
            return result

    # ---- static + manifest -------------------------------------------------
    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            html = resp.read().decode()
            self.assertEqual(resp.status, 200)
            self.assertIn("Astra", html)
            self.assertIn("dash-blocks", html)  # core dashboard container
        with urllib.request.urlopen(self.base + "/static/js/astra.js") as resp:
            self.assertTrue(resp.read().decode().startswith("/* Astra AI Agent"))

    def test_manifest(self):
        s, body = self.req("GET", "/api/manifest")
        self.assertEqual(s, 200)
        self.assertEqual(body["data"]["name"], AGENT_NAME)
        slugs = [p["slug"] for p in body["data"]["plugins"]]
        self.assertIn("airdrop", slugs)
        # manifest lists a tab per plugin + the core tabs
        tabs = [t["tab"] for t in body["data"]["tabs"]]
        self.assertIn("Dashboard", "".join(tabs).title())
        self.assertIn("assistant", tabs)
        self.assertIn("airdrop", tabs)

    def test_unknown_api_404(self):
        s, body = self.req("GET", "/api/nope")
        self.assertEqual(s, 404)
        self.assertFalse(body["ok"])

    # ---- airdrop CRUD over HTTP -------------------------------------------
    def test_airdrop_lifecycle(self):
        s, body = self.req("POST", "/api/airdrops",
                           {"name": "Hamster", "deadline": "2026-12-31",
                            "network": "TON"})
        self.assertEqual(s, 201)
        aid = body["data"]["id"]
        s, body = self.req("GET", "/api/airdrops")
        self.assertEqual(len(body["data"]), 1)
        s, body = self.req("PATCH", f"/api/airdrops/{aid}", {"status": "farming"})
        self.assertEqual(body["data"]["status"], "farming")
        s, body = self.req("DELETE", f"/api/airdrops/{aid}")
        self.assertTrue(body["ok"])
        s, body = self.req("GET", "/api/airdrops")
        self.assertEqual(body["data"], [])

    # ---- tasks + dashboard ------------------------------------------------
    def test_task_flow_and_dashboard(self):
        _, ad = self.req("POST", "/api/airdrops", {"name": "Coin"})
        aid = ad["data"]["id"]
        _, tk = self.req("POST", "/api/tasks",
                         {"airdrop_id": aid, "title": "join tg", "category": "social"})
        tid = tk["data"]["id"]
        s, body = self.req("PATCH", f"/api/tasks/{tid}", {"status": "done"})
        self.assertEqual(body["data"]["status"], "done")
        s, dash = self.req("GET", "/api/dashboard")
        airdrop_block = next(b for b in dash["data"] if b["slug"] == "airdrop")
        self.assertEqual(airdrop_block["title"], "Airdrops")
        cards = airdrop_block["data"]["cards"]
        self.assertEqual(cards[0]["v"], 1)  # "Total airdrops" card

    # ---- wallet -----------------------------------------------------------
    def test_wallet_validate_and_add(self):
        s, body = self.req("GET",
                           "/api/wallet/validate?address=0x7A1234FF00AAAAAA567890")
        self.assertTrue(body["data"]["valid"])
        s, body = self.req("POST", "/api/wallets",
                           {"address": "0x7A1234FF00AAAAAA567890",
                            "label": "main100", "network": "ETH"})
        self.assertEqual(s, 201)
        s, body = self.req("GET", "/api/wallets")
        self.assertEqual(len(body["data"]), 1)
        s, err = self.req("POST", "/api/wallets", {"address": "abc"})
        self.assertEqual(s, 400)

    # ---- chat -------------------------------------------------------------
    @unittest.skip(
        "Agent.handle() no longer regex-matches chat text against plugins "
        "(see astra/agent.py module docstring) — every message goes "
        "straight to Orchestrator -> Planner -> Gateway -> Provider. "
        "Re-enable once AirdropPlugin exposes its commands as AI-callable "
        "tools() so the Provider can invoke them from a real chat message.")
    def test_chat_creates_airdrop(self):
        s, body = self.req("POST", "/api/chat",
                           {"message": "add airdrop ChatTest deadline 2026-10-01 reward token"})
        self.assertTrue(body["data"]["ok"], body["data"]["reply"])
        self.assertEqual(body["data"]["action"], "airdrop")
        self.assertIn("ChatTest", body["data"]["reply"])

    def test_chat_unknown_but_graceful(self):
        s, body = self.req("POST", "/api/chat", {"message": "xblargh 99"})
        self.assertEqual(s, 200)  # server never 500s on a weird message
        self.assertFalse(body["data"]["ok"])

    # ---- export / import --------------------------------------------------
    def test_export_import_roundtrip(self):
        self.req("POST", "/api/airdrops", {"name": "E", "reward_type": "points"})
        s, body = self.req("GET", "/api/export")
        payload = body["data"]
        self.assertEqual(payload["_app"], AGENT_NAME)
        self.assertIn("airdrop", payload["_exports"])
        s, imp = self.req("POST", "/api/import", {"data": payload})
        self.assertTrue(imp["ok"])
        self.assertEqual(imp["data"].get("airdrop_added_airdrops", 0), 0)


if __name__ == "__main__":
    unittest.main()