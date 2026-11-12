"""E2E for background mode (needs Docker). Env: KEEPSTACK_E2E_DAEMON_PORT (default 9350; emulator = +1)."""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_DAEMON_PORT", "9350"))
CONTAINER = f"keepstack-{PORT}"
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8")
AUTH = ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260101/us-east-1/sqs/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=" + "0" * 64)


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 600), **kw)


def port_open():
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
        return True
    except OSError:
        return False


class E2EDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-daemon-")
        cls.data = os.path.join(cls.tmp, "data")
        if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("docker is not accessible")
        sh("docker", "rm", "-f", CONTAINER)

    @classmethod
    def tearDownClass(cls):
        cls.ks("down")
        sh("docker", "rm", "-f", CONTAINER)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def ks(cls, *args, timeout=600):
        return sh(sys.executable, "-m", "keepstack", args[0], *args[1:], "--data-dir", cls.data,
                  cwd=ROOT, env=CHILD_ENV, timeout=timeout)

    def status(self):
        r = self.ks("status", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_background_lifecycle(self):
        r = self.ks("up", "-d", "--port", str(PORT), "--emulator-port", str(PORT + 1), "--reset", timeout=400)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ready on", r.stdout)
        self.assertTrue(port_open())

        st = self.status()
        self.assertTrue(st["running"], st)
        self.assertEqual(st["port"], PORT)
        self.assertEqual(st["journal"]["calls"], 0)

        body = json.dumps({"QueueName": "keepstack-e2e"})  # SQS JSON protocol; the emulator needs the target header
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
        c.request("POST", "/", body, {"Authorization": AUTH, "X-Amz-Date": "20260101T000000Z",
                                      "X-Amz-Target": "AmazonSQS.CreateQueue",
                                      "Content-Type": "application/x-amz-json-1.0"})
        resp = c.getresponse()
        data = resp.read()
        c.close()
        self.assertEqual(resp.status, 200, data)
        st = self.status()
        self.assertEqual(st["journal"]["calls"], 1)
        self.assertEqual(st["journal"]["services"], {"sqs": 1})

        self.assertIn("ready on", self.ks("logs").stdout)

        r = self.ks("down")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(port_open())
        self.assertNotIn(CONTAINER, sh("docker", "ps", "-a", "--format", "{{.Names}}").stdout.split())

        r = self.ks("down")
        self.assertEqual((r.returncode, "not running" in r.stdout), (0, True), r.stdout)
        st = self.status()
        self.assertFalse(st["running"])
        self.assertEqual(st["journal"]["services"], {"sqs": 1})


if __name__ == "__main__":
    unittest.main()
