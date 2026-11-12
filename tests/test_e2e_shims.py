"""E2E for `--shims`: Terraform + keepstack + real emulator. Skipped if docker or terraform is missing.

Env vars: TERRAFORM (path to the binary), KEEPSTACK_E2E_SHIMS_PORT (default 9380; emulator = +1).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_SHIMS_PORT", "9380"))
CONTAINER = f"keepstack-{PORT}"
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8")
RESTORED = re.compile(r"state restored: \d+ calls replayed, (\d+) failed")
# On-demand table with no ignore_changes workaround: the plan must be clean thanks to the shim.
DDB = '''
resource "aws_dynamodb_table" "t" {
  name         = "shims-demo"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }
}
'''


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 600), **kw)


class E2EShims(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-shims-")
        try:
            if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
                raise unittest.SkipTest("docker is not accessible")
            cls.tf = os.environ.get("TERRAFORM") or shutil.which("terraform")
            if not cls.tf:
                raise unittest.SkipTest("terraform not found (PATH or TERRAFORM env var)")
            cls.tfdir = os.path.join(cls.tmp, "tf")
            os.mkdir(cls.tfdir)
            shutil.copy(os.path.join(ROOT, "tests", "compat", "network-sg", "main.tf"), cls.tfdir)
            with open(os.path.join(cls.tfdir, "ddb.tf"), "w") as f:
                f.write(DDB)
            cls.env = dict(os.environ, AWS_ENDPOINT_URL=f"http://127.0.0.1:{PORT}", AWS_ACCESS_KEY_ID="test",
                           AWS_SECRET_ACCESS_KEY="test", AWS_REGION="us-east-1", AWS_DEFAULT_REGION="us-east-1",
                           TF_IN_AUTOMATION="1")
            sh("docker", "rm", "-f", CONTAINER)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def stop(cls):
        if cls.proc:
            cls.proc.kill()
            cls.proc.wait()
            cls.proc = None
        sh("docker", "rm", "-f", CONTAINER)

    @classmethod
    def start(cls, data, *extra):
        cls.stop()
        cls.log = os.path.join(cls.tmp, f"{os.path.basename(data)}.log")
        with open(cls.log, "w", encoding="utf-8") as f:
            cls.proc = subprocess.Popen(
                [sys.executable, "-m", "keepstack", "up", "--port", str(PORT), "--emulator-port", str(PORT + 1),
                 "--data-dir", os.path.join(cls.tmp, data), *extra],
                stdout=f, stderr=subprocess.STDOUT, cwd=ROOT, env=CHILD_ENV)
        end = time.time() + 300
        while "ready on" not in cls.text() and time.time() < end:
            if cls.proc.poll() is not None:
                break
            time.sleep(0.5)
        assert "ready on" in cls.text(), cls.text()

    @classmethod
    def text(cls):
        with open(cls.log, encoding="utf-8", errors="replace") as f:
            return f.read()

    def terraform(self, *args, expect=(0,)):
        r = sh(self.tf, args[0], "-no-color", "-input=false", *args[1:], cwd=self.tfdir, env=self.env)
        self.assertIn(r.returncode, expect, f"terraform {args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def test_1_without_shims_security_group_fails(self):
        self.start("plain", "--reset")
        self.terraform("init")
        r = self.terraform("apply", "-auto-approve", expect=(1,))
        self.assertIn("InvalidPermission.NotFound", r.stdout + r.stderr)

    def test_2_shims_apply_clean_plan_and_remembered_on_restore(self):
        self.start("shimmed", "--shims", "--reset")
        self.terraform("apply", "-auto-approve")
        self.assertIn("compatibility shims enabled", self.text())  # logged on the first request
        self.assertIn("shim ec2-ipprotocol-all-ports applied", self.text())
        self.terraform("plan", "-detailed-exitcode")
        self.start("shimmed")  # no --shims: the journal remembers it, otherwise the replay would fail
        m = RESTORED.search(self.text())
        self.assertTrue(m and m.group(1) == "0", self.text())
        self.terraform("plan", "-detailed-exitcode")
        self.assertIn("remembered", self.text())
        self.terraform("destroy", "-auto-approve")


if __name__ == "__main__":
    unittest.main()
