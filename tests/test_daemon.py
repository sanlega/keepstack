"""Unit tests for background mode (no Docker)."""
import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from keepstack import daemon


def auth(service):
    return {"Authorization": f"AWS4-HMAC-SHA256 Credential=AKID/20260101/us-east-1/{service}/aws4_request, "
                             "SignedHeaders=host, Signature=abc"}


class Daemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make_db(self, headers_list):
        db = sqlite3.connect(os.path.join(self.dir, "journal.db"))
        db.execute("create table journal(seq integer primary key autoincrement, ts real, op text, method text,"
                   " path text, headers text, body blob, status int, resp blob)")
        db.execute("create table meta(key text primary key, value text)")
        for h in headers_list:
            db.execute("insert into journal(headers) values(?)", (json.dumps(h),))
        db.execute("insert into meta values('last_restore', ?)",
                   (json.dumps({"ts": 1.0, "replayed": 3, "failed": 0, "translated": 2}),))
        db.commit()
        db.close()

    def test_service_breakdown(self):
        rows = [(json.dumps(auth("sqs")),), (json.dumps({"authorization": auth("s3")["Authorization"]}),),
                (json.dumps(auth("sqs")),), (json.dumps({}),), ("not json",), (None,)]
        self.assertEqual(daemon.service_breakdown(rows), {"sqs": 2, "unknown": 3, "s3": 1})

    def test_run_state_roundtrip_and_clear(self):
        class App:
            container = "keepstack-1"
        args = Namespace(data_dir=self.dir, port=1, emulator_port=2)
        daemon.write_run_state(args, App())
        run = daemon.read_run(self.dir)
        self.assertEqual((run["pid"], run["port"], run["container"]), (os.getpid(), 1, "keepstack-1"))
        self.assertTrue(daemon.pid_alive(run["pid"]))
        daemon.clear_run_state(args)
        self.assertIsNone(daemon.read_run(self.dir))

    def test_stale_pid_is_stopped_and_cleaned(self):
        with open(daemon.run_path(self.dir), "w") as f:
            json.dump({"pid": 2 ** 22 + 12345, "port": 1, "container": ""}, f)
        self.assertFalse(daemon.pid_alive(2 ** 22 + 12345))
        self.assertTrue(daemon.collect_status(self.dir)["stale"])
        self.assertEqual(daemon.cmd_down(Namespace(data_dir=self.dir)), 0)
        self.assertIsNone(daemon.read_run(self.dir))

    def test_run_file_from_another_machine_names_a_live_unrelated_pid(self):  # regression #31
        # A restored cache carried run.json over; on this machine that pid is some other live process (here: us)
        # and nothing listens on the port. It is not keepstack: up -d must start, down must not signal it.
        with open(daemon.run_path(self.dir), "w") as f:
            json.dump({"pid": os.getpid(), "port": 1, "container": ""}, f)
        self.assertTrue(daemon.pid_alive(os.getpid()))
        self.assertFalse(daemon.is_running(daemon.read_run(self.dir)))
        self.assertFalse(daemon.collect_status(self.dir)["running"])
        with mock.patch.object(daemon.os, "kill") as kill:
            self.assertEqual(daemon.cmd_down(Namespace(data_dir=self.dir)), 0)
        # os.kill(pid, 0) is only pid_alive() probing on POSIX; any other signal would hit the unrelated process.
        self.assertEqual([c for c in kill.call_args_list if c.args[1] != 0], [])
        self.assertIsNone(daemon.read_run(self.dir))

    def test_down_without_run_file_is_noop(self):
        self.assertEqual(daemon.cmd_down(Namespace(data_dir=self.dir)), 0)

    def test_status_shape_stopped_with_journal(self):
        self.make_db([auth("sqs"), auth("sqs"), auth("dynamodb")])
        st = daemon.collect_status(self.dir)
        self.assertFalse(st["running"])
        j = st["journal"]
        self.assertEqual((j["calls"], j["services"]), (3, {"sqs": 2, "dynamodb": 1}))
        self.assertGreater(j["size_bytes"], 0)
        self.assertEqual(j["last_restore"]["replayed"], 3)
        json.dumps(st)  # serializable

    def test_status_without_journal(self):
        st = daemon.collect_status(self.dir)
        self.assertIsNone(st["journal"])
        self.assertEqual(daemon.cmd_status(Namespace(data_dir=self.dir, json=False)), 0)

    def test_logs_without_log(self):
        self.assertEqual(daemon.cmd_logs(Namespace(data_dir=self.dir, follow=False)), 1)


if __name__ == "__main__":
    unittest.main()
