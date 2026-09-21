"""Generic Store tests — the shared SQLite base under every subsystem."""
import unittest

from astra.store import Store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.s.install("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, v TEXT DEFAULT '')")

    def tearDown(self):
        self.s.close()

    def test_insert_and_fetch(self):
        hid = self.s.insert("t", name="a", v="1")
        self.assertIsInstance(hid, int)
        rows = self.s.fetch("SELECT * FROM t")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "a")

    def test_fetchone(self):
        self.s.insert("t", name="x")
        self.assertIsNotNone(self.s.fetchone("SELECT * FROM t WHERE name=?", ("x",)))
        self.assertIsNone(self.s.fetchone("SELECT * FROM t WHERE name=?", ("zzz",)))

    def test_exec_parametrised(self):
        # no string interpolation of args -> injection-safe by construction
        self.s.exec("INSERT INTO t (name) VALUES (?)", ("' OR 1=1 --",))
        rows = self.s.fetch("SELECT * FROM t")
        self.assertEqual(rows, [{"id": 1, "name": "' OR 1=1 --", "v": ""}])

    def test_exec_returns_lastrowid(self):
        rid = self.s.exec("INSERT INTO t (name) VALUES (?)", ("b",))
        self.assertEqual(rid, 1)

    def test_table_exists(self):
        self.assertTrue(self.s.table_exists("t"))
        self.assertFalse(self.s.table_exists("nope"))

    def test_install_is_idempotent(self):
        self.s.install("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, name TEXT, v TEXT DEFAULT '')")
        self.s.insert("t", name="y")
        self.assertEqual(len(self.s.fetch("SELECT * FROM t")), 1)


if __name__ == "__main__":
    unittest.main()
