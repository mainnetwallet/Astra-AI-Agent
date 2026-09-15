"""Airdrop plugin tests — storage helpers + NL chat (ported from the tested
AirdropAgent suite of 32 storage/agent assertions)."""
import unittest
from datetime import date, timedelta

from helpers import make_agent, make_plugin
from plugins.airdrop import parse_date


class TestParseDate(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(parse_date("2026-12-31"), "2026-12-31")

    def test_ddmmyyyy(self):
        self.assertEqual(parse_date("31/12/2026"), "2026-12-31")
        self.assertEqual(parse_date("15/10/26"), "2026-10-15")

    def test_relative(self):
        self.assertEqual(parse_date("tomorrow"),
                         (date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(parse_date("today"), date.today().isoformat())

    def test_month_name(self):
        self.assertEqual(parse_date("31 dec"), "2026-12-31")
        self.assertEqual(parse_date("15 oct 2026"), "2026-10-15")

    def test_bad(self):
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date("40/40/2026"))


class TestPluginStorage(unittest.TestCase):
    def setUp(self):
        self.p = make_plugin()

    # ---- airdrops ---------------------------------------------------------
    def test_create_and_get_airdrop(self):
        a = self.p.create_airdrop("Hamster", deadline="2026-12-31",
                                  network="TON", reward_type="token")
        got = self.p.get_airdrop(a["id"])
        self.assertEqual(got["name"], "Hamster")
        self.assertEqual(got["deadline"], "2026-12-31")
        self.assertEqual(got["network"], "TON")
        self.assertEqual(got["status"], "active")

    def test_find_by_name_case_insensitive(self):
        self.p.create_airdrop("Notcoin")
        self.assertIsNotNone(self.p.find_airdrop_by_name("notcoin"))
        self.assertIsNotNone(self.p.find_airdrop_by_name("NOTCOIN"))

    def test_update_airdrop(self):
        a = self.p.create_airdrop("X")
        up = self.p.update_airdrop(a["id"], status="farming", estimated_value="500")
        self.assertEqual(up["status"], "farming")
        self.assertEqual(up["estimated_value"], "500")

    def test_delete_cascades_tasks(self):
        a = self.p.create_airdrop("Y")
        self.p.add_task(a["id"], "join telegram")
        self.p.delete_airdrop(a["id"])
        self.assertEqual(self.p.list_tasks(), [])

    def test_count_by_status(self):
        self.p.create_airdrop("a", status="active")
        self.p.create_airdrop("b", status="farming")
        self.p.create_airdrop("c", status="done")
        c = self.p.count_airdrops_by_status()
        self.assertEqual(c["active"], 1)
        self.assertEqual(c["farming"], 1)
        self.assertEqual(c["done"], 1)

    # ---- deadlines --------------------------------------------------------
    def test_upcoming_deadlines(self):
        self.p.create_airdrop("soon", deadline="2026-12-31")
        self.p.create_airdrop("long", deadline="2030-01-01")
        rows = self.p.upcoming_deadlines(1500)
        self.assertEqual([r["name"] for r in rows], ["soon", "long"])

    # ---- tasks ------------------------------------------------------------
    def test_task_crud(self):
        a = self.p.create_airdrop("Z")
        t = self.p.add_task(a["id"], "follow X", "social", "https://x.com")
        self.assertEqual(t["airdrop_id"], a["id"])
        self.assertEqual(t["status"], "pending")
        done = self.p.set_task_status(t["id"], "done")
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["done_at"])
        self.assertEqual(self.p.count_pending_tasks(), 0)

    def test_list_tasks_joins_name(self):
        a = self.p.create_airdrop("Alpha")
        self.p.add_task(a["id"], "join tg")
        self.assertEqual(self.p.list_tasks()[0]["airdrop_name"], "Alpha")

    # ---- wallets ----------------------------------------------------------
    def test_wallet_add_and_validate(self):
        w = self.p.add_wallet("0x7A1234FF00AAAAAA567890", label="main100", network="ETH")
        self.assertEqual(w["label"], "main100")
        self.assertTrue(self.p.validate_address("0x7A1234FF00AAAAAA567890"))
        self.assertTrue(self.p.validate_address("9zbZ31LqVrLAZNMHmvndepF9jnCp7PQPABj5eMZCrUk4"))  # SOL
        self.assertFalse(self.p.validate_address("abc"))
        self.assertFalse(self.p.validate_address("not an 0x at all"))

    def test_wallet_delete(self):
        w = self.p.add_wallet("0x7A1234FF00AAAAAA567890")
        self.p.delete_wallet(w["id"])
        self.assertEqual(self.p.list_wallets(), [])

    # ---- export / import --------------------------------------------------
    def test_roundtrip_export_import(self):
        a = self.p.create_airdrop("Imp", reward_type="points")
        self.p.add_task(a["id"], "mint nft")
        self.p.add_wallet("0x7A1234FF00AAAAAA567890", label="l1")
        payload = self.p.export()
        p2 = make_plugin()
        r = p2.import_data(payload)
        self.assertEqual(r["added_airdrops"], 1)
        self.assertEqual(r["added_tasks"], 1)
        self.assertEqual(r["added_wallets"], 1)
        self.assertEqual(p2.list_airdrops()[0]["reward_type"], "points")
        r2 = p2.import_data(payload)
        self.assertEqual(r2["added_airdrops"], 0)

    def test_dashboard_counts(self):
        self.p.create_airdrop("One", status="active")
        d = self.p.dashboard()
        self.assertEqual(d["total_airdrops"], 1)
        self.assertEqual(d["active"], 1)


class TestAgentChat(unittest.TestCase):
    def setUp(self):
        self.store, self.p, self.agent = make_agent()

    def reply(self, msg):
        return self.agent.handle(msg)

    # ---- add airdrop ------------------------------------------------------
    def test_add_airdrop_basic(self):
        r = self.reply("add airdrop Hamster")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "airdrop")
        self.assertEqual(self.p.list_airdrops()[0]["name"], "Hamster")

    def test_add_airdrop_with_fields(self):
        r = self.reply("add airdrop Notcoin deadline 30 dec reward token value 500 network TON")
        self.assertTrue(r["ok"], r["reply"])
        aid = r["data"]["airdrop"]["id"]
        a = self.p.get_airdrop(aid)
        self.assertEqual(a["deadline"], "2026-12-30")
        self.assertEqual(a["reward_type"], "token")
        self.assertEqual(a["estimated_value"], "500")
        self.assertEqual(a["network"], "TON")

    def test_add_airdrop_iso_deadline(self):
        self.reply("add airdrop Layer deadline 2027-05-01")
        self.assertEqual(self.p.list_airdrops()[0]["deadline"], "2027-05-01")

    def test_duplicate_names_ok(self):
        self.reply("add airdrop Same")
        self.reply("add airdrop Same")
        self.assertEqual(len(self.p.list_airdrops()), 2)

    # ---- delete -----------------------------------------------------------
    def test_delete_airdrop(self):
        self.reply("add airdrop Ghost")
        r = self.reply("delete airdrop Ghost")
        self.assertTrue(r["ok"])
        self.assertEqual(self.p.list_airdrops(), [])

    def test_delete_missing(self):
        self.assertFalse(self.reply("delete airdrop Nope")["ok"])

    # ---- tasks ------------------------------------------------------------
    def test_add_task(self):
        self.reply("add airdrop Coin")
        r = self.reply('add task "join telegram" to Coin')
        self.assertTrue(r["ok"], r["reply"])
        t = self.p.list_tasks()[0]
        self.assertEqual(t["title"], "join telegram")
        self.assertEqual(t["airdrop_name"], "Coin")

    def test_mark_done_by_title(self):
        self.reply("add airdrop Coin")
        self.reply('add task "join tg" to Coin')
        r = self.reply('mark "join tg" in Coin done')
        self.assertTrue(r["ok"], r["reply"])
        self.assertEqual(self.p.count_pending_tasks(), 0)

    def test_mark_done_by_id(self):
        self.reply("add airdrop Coin")
        self.p.add_task(self.p.list_airdrops()[0]["id"], "follow X")
        r = self.reply("mark task 1 done")
        self.assertTrue(r["ok"], r["reply"])
        self.assertEqual(r["data"]["task"]["status"], "done")

    def test_add_task_fails_unknown_airdrop(self):
        self.assertFalse(self.reply('add task "join tg" to Missing')["ok"])

    # ---- wallets ----------------------------------------------------------
    def test_add_wallet(self):
        r = self.reply("add wallet 0x7A1234FF00AAAAAA567890 label main100 network ETH")
        self.assertTrue(r["ok"], r["reply"])
        w = self.p.list_wallets()[0]
        self.assertEqual(w["label"], "main100")
        self.assertEqual(w["network"], "ETH")

    def test_add_wallet_rejects_short(self):
        self.assertFalse(self.reply("add wallet 0x123 label x")["ok"])

    def test_list_wallets_empty(self):
        self.assertFalse(self.reply("list wallets")["ok"])

    # ---- deadlines / summary ---------------------------------------------
    def test_deadlines_this_week(self):
        soon = (date.today() + timedelta(days=2)).isoformat()
        self.p.create_airdrop("Fast", deadline=soon)
        r = self.reply("deadlines this week")
        self.assertTrue(r["ok"], r["reply"])
        self.assertIn("Fast", r["reply"])

    def test_summary(self):
        self.p.create_airdrop("One")
        r = self.reply("progress")
        self.assertTrue(r["ok"])
        self.assertIn("1", r["reply"])

    # ---- free text fallback ----------------------------------------------
    def test_unknown_falls_back_offline(self):
        r = self.reply("ete bojhi")
        self.assertFalse(r["ok"])
        self.assertIn("help", r["reply"].lower())

    def test_help(self):
        r = self.reply("help")
        self.assertTrue(r["ok"])
        self.assertIn("airdrop", r["reply"].lower())


if __name__ == "__main__":
    unittest.main()