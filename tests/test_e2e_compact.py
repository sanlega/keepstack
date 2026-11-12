"""E2E: `keepstack compact` against the real emulator. Skipped if docker is missing.

Env: KEEPSTACK_E2E_COMPACT_PORT (default 9360; emulator = +1; the verification emulator uses 9362+).
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_COMPACT_PORT", "9360"))
CONTAINER = f"keepstack-{PORT}"
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
                 KEEPSTACK_COMPACT_VERIFY_PORT=str(PORT + 2))
AUTH = "AWS4-HMAC-SHA256 Credential=test/20260101/us-east-1/{}/aws4_request, SignedHeaders=host, Signature=00"


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 600), **kw)


class CompactE2E(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-compact-")
        cls.data = os.path.join(cls.tmp, "data")
        if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("docker is not accessible")

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def start(cls):
        cls.log = os.path.join(cls.tmp, f"proxy{time.time_ns()}.log")
        with open(cls.log, "w", encoding="utf-8") as f:
            cls.proc = subprocess.Popen([sys.executable, "-m", "keepstack", "up", "--port", str(PORT),
                                         "--emulator-port", str(PORT + 1), "--data-dir", cls.data],
                                        stdout=f, stderr=subprocess.STDOUT, cwd=ROOT, env=CHILD_ENV)
        end = time.time() + 300
        while time.time() < end and "ready on" not in cls.read_log():
            if cls.proc.poll() is not None:
                break
            time.sleep(0.5)
        assert "ready on" in cls.read_log(), cls.read_log()

    @classmethod
    def stop(cls):
        if cls.proc:
            cls.proc.kill()
            cls.proc.wait()
            cls.proc = None
        sh("docker", "rm", "-f", CONTAINER)

    @classmethod
    def read_log(cls):
        with open(cls.log, encoding="utf-8", errors="replace") as f:
            return f.read()

    def call(self, service, content_type, body, target=None):
        headers = {"Content-Type": content_type, "Authorization": AUTH.format(service)}
        if target:
            headers["X-Amz-Target"] = target
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/", data=body.encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode()

    def ec2(self, action, **params):
        body = "&".join([f"Action={action}", "Version=2016-11-15"] + [f"{k}={v}" for k, v in params.items()])
        return self.call("ec2", "application/x-www-form-urlencoded", body)

    def sqs(self, op, payload):
        return self.call("sqs", "application/x-amz-json-1.0", json.dumps(payload), f"AmazonSQS.{op}")

    def count(self):
        db = sqlite3.connect(os.path.join(self.data, "journal.db"))
        try:
            return db.execute("select count(*) from journal").fetchone()[0]
        finally:
            db.close()

    def test_compact_keeps_state(self):
        sh("docker", "rm", "-f", CONTAINER)
        self.start()
        vpc = re.search(r"<vpcId>(vpc-\w+)</vpcId>", self.ec2("CreateVpc", CidrBlock="10.0.0.0/16")).group(1)
        subnet = re.search(r"<subnetId>(subnet-\w+)</subnetId>",
                           self.ec2("CreateSubnet", VpcId=vpc, CidrBlock="10.0.1.0/24")).group(1)
        self.ec2("DeleteSubnet", SubnetId=subnet)
        self.ec2("DeleteVpc", VpcId=vpc)
        keep = re.search(r"<vpcId>(vpc-\w+)</vpcId>", self.ec2("CreateVpc", CidrBlock="10.1.0.0/16")).group(1)
        url = json.loads(self.sqs("CreateQueue", {"QueueName": "tmp-queue"}))["QueueUrl"]
        self.sqs("DeleteQueue", {"QueueUrl": url})
        before = self.count()
        self.stop()

        r = sh(sys.executable, "-m", "keepstack", "compact", "--data-dir", self.data, cwd=ROOT, env=CHILD_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.count()
        self.assertLess(after, before, r.stdout)
        self.assertEqual(after, 1, r.stdout)  # only the surviving VPC remains
        self.assertTrue(os.path.isdir(os.path.join(self.data, "snapshots", "pre-compact")))
        self.assertEqual(sh("docker", "ps", "-a", "--filter", "name=keepstack-", "--format", "{{.Names}}").stdout
                         .split().count(f"keepstack-{PORT + 2}"), 0, "throwaway container was not cleaned up")

        self.start()
        self.assertRegex(self.read_log(), r"state restored: \d+ calls replayed, 0 failed")
        vpcs = self.ec2("DescribeVpcs")
        self.assertIn(keep, vpcs)
        self.assertNotIn(vpc, vpcs)


if __name__ == "__main__":
    unittest.main()
