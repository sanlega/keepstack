import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from keepstack import snapshot

SCHEMA = ("create table journal(seq integer primary key autoincrement, ts real, op text, method text, path text, "
          "headers text, body blob, status int, resp blob); create table meta(key text primary key, value text)")


def make_journal(path, n=3):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    for i in range(n):
        db.execute("insert into journal(ts, op, method, path, headers, body, status, resp) values(1,'CreateVpc','POST',"
                   "'/','{}',?,200,?)", (f"Action=CreateVpc&n={i}".encode(), b"<vpcId>vpc-0abc12</vpcId>"))
    db.commit()
    db.close()


def calls(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("select count(*) from journal").fetchone()[0]
    finally:
        db.close()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.db = os.path.join(self.data, "journal.db")
        make_journal(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fn(Namespace(data_dir=self.data, **kw))
        return rc, out.getvalue()

    def test_save_list_restore_delete(self):
        rc, _ = self.call(snapshot.cmd_save, name="one", include=[], force=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.call(snapshot.cmd_save, name="one", include=[], force=False)[0], 1)  # exists
        self.assertEqual(self.call(snapshot.cmd_save, name="one", include=[], force=True)[0], 0)
        make_journal(self.db + ".x", 7)
        shutil.move(self.db + ".x", self.db)  # state changes after the snapshot
        rc, out = self.call(snapshot.cmd_list)
        self.assertIn("one", out)
        self.assertIn("3", out)
        self.assertEqual(self.call(snapshot.cmd_restore, name="one")[0], 0)
        self.assertEqual(calls(self.db), 3)
        self.assertEqual(self.call(snapshot.cmd_delete, name="one")[0], 0)
        self.assertEqual(self.call(snapshot.cmd_delete, name="one")[0], 1)

    def test_pre_restore_safety_snapshot(self):
        self.call(snapshot.cmd_save, name="small", include=[], force=False)
        make_journal(self.db + ".x", 9)
        shutil.move(self.db + ".x", self.db)
        rc, out = self.call(snapshot.cmd_restore, name="small")
        self.assertEqual(rc, 0)
        self.assertIn("pre-restore", out)
        self.assertEqual(calls(self.db), 3)
        self.assertEqual(self.call(snapshot.cmd_restore, name="pre-restore")[0], 0)  # undo
        self.assertEqual(calls(self.db), 9)

    def test_include_round_trip(self):
        f = os.path.join(self.tmp, "terraform.tfstate")
        with open(f, "w") as fh:
            fh.write("v1")
        self.call(snapshot.cmd_save, name="withfile", include=[f], force=False)
        with open(f, "w") as fh:
            fh.write("v2")
        rc, out = self.call(snapshot.cmd_restore, name="withfile")
        self.assertEqual(rc, 0)
        self.assertIn(f, out)
        with open(f) as fh:
            self.assertEqual(fh.read(), "v1")
        self.assertIn(f, self.call(snapshot.cmd_list)[1])
        # the file overwritten by the restore is kept in pre-restore
        self.assertEqual(self.call(snapshot.cmd_restore, name="pre-restore")[0], 0)
        with open(f) as fh:
            self.assertEqual(fh.read(), "v2")

    def test_missing_include_refused(self):
        rc, out = self.call(snapshot.cmd_save, name="x", include=[os.path.join(self.tmp, "nope")], force=False)
        self.assertEqual(rc, 1)
        self.assertIn("not found", out)

    def test_name_validation(self):
        for bad in ("../evil", "a/b", "", "-x", "a b", "x" * 65):
            self.assertEqual(self.call(snapshot.cmd_save, name=bad, include=[], force=False)[0], 1, bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil")))

    def test_restore_refused_while_running(self):
        self.call(snapshot.cmd_save, name="s", include=[], force=False)
        with open(os.path.join(self.data, "run.json"), "w") as fh:
            json.dump({"pid": os.getpid()}, fh)
        rc, out = self.call(snapshot.cmd_restore, name="s")
        self.assertEqual(rc, 1)
        self.assertIn("keepstack down", out)
        exp = os.path.join(self.tmp, "e.db")
        self.assertEqual(self.call(snapshot.cmd_export, file=exp)[0], 0)  # export/save are fine while running
        self.assertEqual(self.call(snapshot.cmd_import, file=exp)[0], 1)
        self.assertFalse(os.path.exists(os.path.join(self.data, "snapshots", "pre-restore")))

    def test_stale_run_file_does_not_block(self):
        self.call(snapshot.cmd_save, name="s", include=[], force=False)
        with open(os.path.join(self.data, "run.json"), "w") as fh:
            json.dump({"pid": 2 ** 22 + 12345}, fh)  # not a live pid
        self.assertEqual(self.call(snapshot.cmd_restore, name="s")[0], 0)

    def test_export_import(self):
        exp = os.path.join(self.tmp, "state.kstack")
        self.assertEqual(self.call(snapshot.cmd_export, file=exp)[0], 0)
        db = sqlite3.connect(exp)
        info = json.loads(db.execute("select value from meta where key='export_info'").fetchone()[0])
        db.close()
        self.assertIn("keepstack_version", info)
        self.assertEqual(calls(self.db), 3)  # the source journal is untouched
        make_journal(self.db + ".x", 5)
        shutil.move(self.db + ".x", self.db)
        rc, out = self.call(snapshot.cmd_import, file=exp)
        self.assertEqual(rc, 0)
        self.assertIn("pre-restore", out)
        self.assertEqual(calls(self.db), 3)

    def test_import_into_empty_data_dir(self):
        exp = os.path.join(self.tmp, "state.kstack")
        self.call(snapshot.cmd_export, file=exp)
        self.data = os.path.join(self.tmp, "fresh")
        self.assertEqual(self.call(snapshot.cmd_import, file=exp)[0], 0)
        self.assertEqual(calls(os.path.join(self.data, "journal.db")), 3)

    def test_import_rejects_non_keepstack_files(self):
        junk = os.path.join(self.tmp, "junk")
        with open(junk, "wb") as fh:
            fh.write(b"this is not sqlite at all" * 10)
        other = os.path.join(self.tmp, "other.db")
        db = sqlite3.connect(other)
        db.execute("create table journal(a, b)")
        db.commit()
        db.close()
        for f in (junk, other, os.path.join(self.tmp, "missing")):
            rc = self.call(snapshot.cmd_import, file=f)[0]
            self.assertEqual(rc, 1, f)
        self.assertEqual(calls(self.db), 3)
        self.assertFalse(os.path.exists(os.path.join(self.data, "snapshots", "pre-restore")))


if __name__ == "__main__":
    unittest.main()
