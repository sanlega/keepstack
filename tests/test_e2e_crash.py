"""E2E for crash safety (needs Docker). Env: KEEPSTACK_E2E_CRASH_PORT (default 9470; emulator = +1).

Regression for #34: a write that waits while keepstack restores the emulator, and is abandoned by its client
meanwhile (an SDK that timed out and will retry), must not be executed afterwards.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_CRASH_PORT", "9470"))
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8")
AUTH = ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260101/us-east-1/sqs/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=" + "0" * 64)


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 600), **kw)


def sqs(target, body, timeout=30):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        c.request("POST", "/", json.dumps(body), {"X-Amz-Target": f"AmazonSQS.{target}", "Authorization": AUTH,
                                                  "Content-Type": "application/x-amz-json-1.0"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


class E2ECrash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-crash-")
        cls.data = os.path.join(cls.tmp, "data")
        if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("docker is not accessible")
        sh("docker", "rm", "-f", f"keepstack-{PORT}")

    @classmethod
    def tearDownClass(cls):
        cls.ks("down")
        sh("docker", "rm", "-f", f"keepstack-{PORT}")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def ks(cls, *args, timeout=600):
        return sh(sys.executable, "-m", "keepstack", args[0], *args[1:], "--data-dir", cls.data,
                  cwd=ROOT, env=CHILD_ENV, timeout=timeout)

    def test_write_abandoned_during_restore_is_not_executed(self):
        r = self.ks("up", "-d", "--port", str(PORT), "--emulator-port", str(PORT + 1), timeout=900)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for i in range(200):  # a journal long enough that restoring it outlasts the client's patience
            self.assertEqual(sqs("CreateQueue", {"QueueName": f"filler-{i}"})[0], 200)
        sh("docker", "kill", f"keepstack-{PORT}")
        # A fixed sleep raced the watcher (#47): before it notices, writes hit the dead emulator and fail at once.
        deadline = time.time() + 30
        while "the emulator stopped" not in self.ks("logs").stdout and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)  # the watcher is restoring the emulator now; writes wait
        with self.assertRaises((socket.timeout, TimeoutError), msg="restore finished too fast to test"):
            sqs("CreateQueue", {"QueueName": "abandoned"}, timeout=1)
        deadline = time.time() + 120
        while time.time() < deadline:  # wait for the restore to finish
            try:
                status = sqs("GetQueueUrl", {"QueueName": "filler-199"}, timeout=5)[0]
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        status, _ = sqs("GetQueueUrl", {"QueueName": "abandoned"})
        self.assertEqual(status, 400, "the abandoned CreateQueue was executed after its client left")
        log = self.ks("logs").stdout
        self.assertIn("client gave up waiting; not executing CreateQueue", log)
        # The client retries, as an SDK would: now it goes through normally.
        self.assertEqual(sqs("CreateQueue", {"QueueName": "abandoned"})[0], 200)


if __name__ == "__main__":
    unittest.main()
