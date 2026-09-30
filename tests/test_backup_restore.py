"""Total Data Backup & Import: portability, secret exclusion, safe restore."""
from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile

from astra import backup
from astra.bootstrap import build
from astra.store import Store
from astra.web import AstraSite, Request, WebApp
from astra.workflows.scheduler import SchedulerManager

SECRET = "sk-ABCDEFGHIJKLMNOP123456"


def make():
    store = Store(":memory:")
    stack = build(store=store)
    SchedulerManager(store, stack["workflows"], stack["events"])
    site = AstraSite(("127.0.0.1", 0), store, stack["agent"], stack=stack)
    return store, stack, WebApp(site)


def seed(store):
    store.exec("INSERT INTO astra_memories(content, category, created_at) VALUES(?,?,?)",
               ("remember this", "note", "2026-09-30"))
    store.exec("INSERT INTO astra_memories(content, category, created_at) VALUES(?,?,?)",
               ("token is " + SECRET, "note", "2026-09-30"))
    store.exec("INSERT INTO astra_experiences(pattern, strategy, result, created_at) "
               "VALUES('p','s','r','2026-09-30')")
    store.exec("INSERT INTO workflow_definitions(name, steps, created_at) VALUES('wf1','[]','x')")
    wid = store.fetchone("SELECT id FROM workflow_definitions")["id"]
    store.exec("INSERT INTO schedules(name, kind, value, workflow_id) VALUES('s1','interval','60',?)", (wid,))
    store.exec("INSERT INTO web3_wallets(id,address,name,source,keystore_name,active) "
               "VALUES('w1',?, 'main','created','ks-main',1)", ("0x" + "ab" * 20,))


def repack(blob, mutate):
    zin = zipfile.ZipFile(io.BytesIO(blob))
    files = {n: zin.read(n) for n in zin.namelist()}
    mutate(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return out.getvalue()


def call(app, path, files=None, fields=None, body=None):
    r = app.handle(Request("POST", path, headers={}, body=body or {}, fields=fields or {},
                           files=files or [], rid="t"))
    return r


class BackupContents(unittest.TestCase):
    def setUp(self):
        self.store, self.stack, self.app = make()
        seed(self.store)
        self.blob, self.manifest = backup.create_backup(self.store, self.stack)

    def text(self):
        z = zipfile.ZipFile(io.BytesIO(self.blob))
        return "".join(z.read(n).decode() for n in z.namelist())

    def test_no_raw_secret_anywhere_in_the_archive(self):
        self.assertNotIn(SECRET, self.text())
        self.assertFalse(self.manifest["secrets_included"])

    def test_no_keystore_keys_or_machine_paths(self):
        t = self.text()
        self.assertNotIn("ks-main", t)             # keystore record name
        self.assertNotIn("web3_keys", t)
        self.assertNotIn("master_key", t.replace("ASTRA_MASTER_SECRET, .master_key", ""))
        self.assertNotIn(self.store.path or "\0", "\0")

    def test_manifest_is_versioned_and_integrity_checked(self):
        m = self.manifest
        self.assertEqual((m["format"], m["format_version"]), ("astra-backup", 1))
        for name, meta in m["files"].items():
            raw = zipfile.ZipFile(io.BytesIO(self.blob)).read(name)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])

    def test_schedules_reference_workflows_by_name_not_id(self):
        z = zipfile.ZipFile(io.BytesIO(self.blob))
        row = json.loads(z.read("data/schedules.json"))["tables"]["schedules"][0]
        self.assertEqual(row["workflow_name"], "wf1")
        self.assertNotIn("workflow_id", row)
        self.assertNotIn("id", row)

    def test_create_endpoint_returns_a_zip_download(self):
        r = call(self.app, "/api/security/backup/create")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.content_type, "application/zip")
        hdr = dict(r.headers)
        self.assertIn("astra_backup_", hdr["Content-Disposition"])
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(r.body)))
        meta = json.loads(hdr["X-Astra-Backup"])
        self.assertNotIn(SECRET, json.dumps(meta))


class Validation(unittest.TestCase):
    def setUp(self):
        self.store, self.stack, self.app = make()
        seed(self.store)
        self.blob, _ = backup.create_backup(self.store, self.stack)

    def rejects(self, blob, code=None):
        with self.assertRaises(backup.BackupError) as cm:
            backup.inspect_backup(self.store, blob)
        if code:
            self.assertEqual(cm.exception.code, code)
        return str(cm.exception)

    def test_garbage_and_empty_rejected(self):
        self.rejects(b"not a zip")
        self.rejects(b"")

    def test_missing_manifest_rejected(self):
        self.rejects(repack(self.blob, lambda f: f.pop("manifest.json")))

    def test_tampered_data_fails_integrity_check(self):
        def mut(f):
            f["data/memory.json"] = f["data/memory.json"].replace(b"remember", b"forgotten")
        self.assertIn("integrity", self.rejects(repack(self.blob, mut)))

    def test_newer_format_is_incompatible_not_a_crash(self):
        def mut(f):
            m = json.loads(f["manifest.json"]); m["format_version"] = 99
            f["manifest.json"] = json.dumps(m).encode()
        self.rejects(repack(self.blob, mut), "backup_incompatible")

    def test_unexpected_entries_rejected_zip_slip(self):
        self.rejects(repack(self.blob, lambda f: f.update({"../evil.txt": b"x"})))

    def test_hand_edited_secret_is_refused(self):
        def mut(f):
            f["data/memory.json"] = f["data/memory.json"].replace(b"remember this", SECRET.encode())
            m = json.loads(f["manifest.json"])
            m["files"]["data/memory.json"]["sha256"] = hashlib.sha256(f["data/memory.json"]).hexdigest()
            f["manifest.json"] = json.dumps(m).encode()
        self.rejects(repack(self.blob, mut), "backup_unsafe")

    def test_endpoints_map_errors_to_structured_responses(self):
        bad = [{"filename": "x.zip", "data": b"junk", "content_type": "x"}]
        r = call(self.app, "/api/security/backup/inspect", files=bad)
        self.assertEqual(r.status, 400)
        self.assertEqual(json.loads(r.body)["error_code"], "backup_invalid")
        r = call(self.app, "/api/security/backup/inspect")
        self.assertEqual(r.status, 400)

    def test_restore_requires_confirm(self):
        f = [{"filename": "b.zip", "data": self.blob, "content_type": "application/zip"}]
        r = call(self.app, "/api/security/backup/restore", files=f, fields={})
        self.assertEqual(r.status, 400)


class Portability(unittest.TestCase):
    """Old PC -> new PC: restore into a completely fresh install."""

    def setUp(self):
        s1, st1, _ = make(); seed(s1)
        self.blob, _ = backup.create_backup(s1, st1)
        self.store, self.stack, self.app = make()

    def files(self):
        return [{"filename": "b.zip", "data": self.blob, "content_type": "application/zip"}]

    def restore(self, strategy="keep_existing"):
        r = call(self.app, "/api/security/backup/restore", files=self.files(),
                 fields={"confirm": "true", "strategy": strategy})
        self.assertEqual(r.status, 200, r.body)
        return json.loads(r.body)["data"]

    def test_inspect_previews_without_changing_anything(self):
        r = call(self.app, "/api/security/backup/inspect", files=self.files())
        d = json.loads(r.body)["data"]
        self.assertTrue(d["valid"])
        cats = {c["id"]: c for c in d["categories"]}
        self.assertEqual(cats["memory"]["items"], 2)
        self.assertFalse(cats["providers"]["restorable"])
        self.assertEqual(self.store.fetch("SELECT * FROM astra_memories"), [])

    def test_full_restore_and_relink(self):
        out = self.restore()
        self.assertTrue(all(out["verified"].values()))
        self.assertEqual(len(self.store.fetch("SELECT * FROM astra_memories")), 2)
        link = self.store.fetchone(
            "SELECT s.name, w.name wf FROM schedules s JOIN workflow_definitions w ON w.id=s.workflow_id")
        self.assertEqual((link["name"], link["wf"]), ("s1", "wf1"))
        self.assertTrue(out["safety_snapshot"].startswith("pre-import_"))

    def test_restored_wallet_is_watch_only_never_keyed_or_active(self):
        self.restore()
        w = self.store.fetchone("SELECT * FROM web3_wallets")
        self.assertEqual((w["source"], w["keystore_name"], w["active"]), ("watch", "", 0))
        if self.store.table_exists("web3_keys"):
            self.assertEqual(self.store.fetch("SELECT * FROM web3_keys"), [])

    def test_restore_twice_is_idempotent(self):
        self.restore(); again = self.restore()
        self.assertEqual(len(self.store.fetch("SELECT * FROM astra_memories")), 2)
        self.assertEqual(again["results"]["memory"]["added"], 0)
        self.assertEqual(len(self.store.fetch("SELECT * FROM workflow_definitions")), 1)

    def test_existing_data_is_never_silently_destroyed(self):
        self.store.exec("INSERT INTO workflow_definitions(name, steps, description) "
                        "VALUES('wf1','[1]','MINE')")
        self.store.exec("INSERT INTO astra_memories(content, category) VALUES('local only','note')")
        self.restore("keep_existing")
        wf = self.store.fetchone("SELECT * FROM workflow_definitions WHERE name='wf1'")
        self.assertEqual(wf["description"], "MINE")
        self.assertEqual(len(self.store.fetch("SELECT * FROM astra_memories")), 3)

    def test_merge_keeps_both_workflows_under_distinct_names(self):
        self.store.exec("INSERT INTO workflow_definitions(name, steps) VALUES('wf1','[1]')")
        out = self.restore("merge")
        names = sorted(r["name"] for r in self.store.fetch("SELECT name FROM workflow_definitions"))
        self.assertEqual(names, ["wf1", "wf1 (imported)"])
        self.assertEqual(out["results"]["workflows"]["renamed"], 1)

    def test_replace_overwrites_only_matching_workflow(self):
        self.store.exec("INSERT INTO workflow_definitions(name, steps, description) VALUES('wf1','[1]','old')")
        self.store.exec("INSERT INTO workflow_definitions(name, steps) VALUES('other','[]')")
        self.restore("replace")
        self.assertEqual(len(self.store.fetch("SELECT * FROM workflow_definitions")), 2)

    def test_failed_restore_rolls_back_completely(self):
        real = backup._restore_schedules
        backup._restore_schedules = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                backup.restore_backup(self.store, self.blob, "keep_existing", None, self.stack)
        finally:
            backup._restore_schedules = real
        self.assertEqual(self.store.fetch("SELECT * FROM astra_memories"), [])
        self.assertEqual(self.store.fetch("SELECT * FROM workflow_definitions"), [])

    def test_only_selected_categories_are_restored(self):
        backup.restore_backup(self.store, self.blob, "keep_existing", ["memory"], self.stack)
        self.assertEqual(len(self.store.fetch("SELECT * FROM astra_memories")), 2)
        self.assertEqual(self.store.fetch("SELECT * FROM workflow_definitions"), [])

    def test_bad_strategy_rejected(self):
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.store, self.blob, "obliterate", None, self.stack)


if __name__ == "__main__":
    unittest.main()
