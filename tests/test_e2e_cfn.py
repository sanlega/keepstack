"""E2E (#19): names that CloudFormation generates survive a restart. Real emulator; skipped without docker.

Env vars: KEEPSTACK_E2E_PORT (default 9340; emulator = +1).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from test_e2e import CHILD_ENV, ROOT, sh

PORT = int(os.environ.get("KEEPSTACK_E2E_PORT", "9340"))
CONTAINER = f"keepstack-{PORT}"
ENDPOINT = f"http://127.0.0.1:{PORT}/"
TEMPLATE = {"Resources": {"Q": {"Type": "AWS::SQS::Queue"}}, "Outputs": {"Url": {"Value": {"Ref": "Q"}}}}


def call(service, body, headers):
    auth = f"AWS4-HMAC-SHA256 Credential=test/20261113/us-east-1/{service}/aws4_request, SignedHeaders=host, Signature=x"
    req = urllib.request.Request(ENDPOINT, data=body, headers={"Authorization": auth, **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def cfn(**params):
    return call("cloudformation", urllib.parse.urlencode({**params, "Version": "2010-05-15"}).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})


def sqs(op, **params):
    return call("sqs", json.dumps(params).encode(),
                {"X-Amz-Target": f"AmazonSQS.{op}", "Content-Type": "application/x-amz-json-1.0"})


class E2ECloudFormation(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-cfn-")
        if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("docker is not accessible")
        sh("docker", "rm", "-f", CONTAINER)

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        sh("docker", "rm", "-f", CONTAINER)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def stop(cls):
        if cls.proc:
            cls.proc.kill()
            cls.proc.wait()
            cls.proc = None

    @classmethod
    def start(cls):
        log = os.path.join(cls.tmp, f"proxy-{time.time()}.log")
        with open(log, "w", encoding="utf-8") as f:
            cls.proc = subprocess.Popen(
                [sys.executable, "-m", "keepstack", "up", "--port", str(PORT), "--emulator-port", str(PORT + 1),
                 "--data-dir", os.path.join(cls.tmp, "data")], stdout=f, stderr=subprocess.STDOUT, cwd=ROOT,
                env=CHILD_ENV)
        end = time.time() + 300  # the first run may build the image
        while time.time() < end:
            with open(log, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if "ready on" in text:
                return text
            if cls.proc.poll() is not None:
                break
            time.sleep(0.5)
        raise AssertionError(f"keepstack did not start:\n{text}")

    def queue_url(self):
        end = time.time() + 120
        while time.time() < end:
            status, body = cfn(Action="DescribeStackResources", StackName="names")
            if status == 200 and "_IN_PROGRESS<" not in body and "<PhysicalResourceId>" in body:
                return body.split("<PhysicalResourceId>")[1].split("<")[0]
            time.sleep(1)
        self.fail(f"the stack did not finish: {status} {body}")

    def test_generated_queue_name_survives_restart(self):
        self.start()
        status, body = cfn(Action="CreateStack", StackName="names", TemplateBody=json.dumps(TEMPLATE))
        self.assertEqual(status, 200, body)
        url = self.queue_url()
        self.assertIn("names-Q-", url)
        time.sleep(3)  # keepstack polls every second before journaling the snapshot

        self.stop()
        sh("docker", "rm", "-f", CONTAINER)
        log = self.start()
        self.assertIn("0 failed", log)

        self.assertEqual(self.queue_url(), url)  # translated back to the name clients know
        status, body = sqs("SendMessage", QueueUrl=url, MessageBody="hi")
        self.assertEqual(status, 200, body)
        status, body = sqs("GetQueueAttributes", QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
        self.assertEqual((status, json.loads(body)["Attributes"]["ApproximateNumberOfMessages"]), (200, "1"), body)


if __name__ == "__main__":
    unittest.main()
