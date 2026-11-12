"""E2E for the dashboard (needs Docker). Env: KEEPSTACK_E2E_DASHBOARD_PORT (default 9390; emulator = +1)."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_DASHBOARD_PORT", "9390"))
CONTAINER = f"keepstack-{PORT}"
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8")
AUTH = ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260101/us-east-1/sqs/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=" + "0" * 64)


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 600), **kw)


def request(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    c.request(method, path, body, headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


class E2EDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-dashboard-")
        cls.data = os.path.join(cls.tmp, "data")
        if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("docker is not accessible")
        sh("docker", "rm", "-f", CONTAINER)

    @classmethod
    def tearDownClass(cls):
        sh(sys.executable, "-m", "keepstack", "down", "--data-dir", cls.data, cwd=ROOT, env=CHILD_ENV)
        sh("docker", "rm", "-f", CONTAINER)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def api(self, name):
        status, data = request("GET", f"/__keepstack/api/{name}")
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def test_dashboard(self):
        r = sh(sys.executable, "-m", "keepstack", "up", "-d", "--port", str(PORT), "--emulator-port", str(PORT + 1),
               "--data-dir", self.data, "--reset", cwd=ROOT, env=CHILD_ENV, timeout=400)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        status, data = request("POST", "/", json.dumps({"QueueName": "dash-e2e"}),
                               {"Authorization": AUTH, "X-Amz-Date": "20260101T000000Z",
                                "X-Amz-Target": "AmazonSQS.CreateQueue", "Content-Type": "application/x-amz-json-1.0"})
        self.assertEqual(status, 200, data)

        st = self.api("status")
        self.assertEqual((st["state"], st["port"], st["journal"]["calls"]), ("running", PORT, 1))
        self.assertEqual(st["services"], {"sqs": 1})
        res = self.api("resources")["resources"]
        self.assertTrue(any(x["kind"] == "Queue" and "dash-e2e" in x["ids"] for x in res), res)
        rows = self.api("journal")["rows"]
        self.assertEqual((rows[0]["label"], rows[0]["service"]), ("CreateQueue", "sqs"))
        self.assertIn(b"keepstack", request("GET", "/__keepstack/")[1])
        # the dashboard is never journaled
        self.assertEqual(self.api("status")["journal"]["calls"], 1)

        r = sh(sys.executable, "-m", "keepstack", "ui", "--no-browser", "--port", str(PORT),
               cwd=ROOT, env=CHILD_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"http://127.0.0.1:{PORT}/__keepstack/", r.stdout)


if __name__ == "__main__":
    unittest.main()
