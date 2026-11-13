"""E2E for `keepstack validate`: real Terraform + keepstack + emulator. Skipped if docker or terraform is missing.

Env vars: TERRAFORM (path to the binary), KEEPSTACK_E2E_VALIDATE_PORT (default 9420; uses the next 3 ports too).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_VALIDATE_PORT", "9420"))
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8")
TABLE = '''
resource "aws_dynamodb_table" "t" {
  name         = "validate-e2e"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }
}
'''
QUEUE = 'resource "aws_sqs_queue" "jobs" {\n  name = "%s"\n}\n'
OUTPUT = 'output "queue" {\n  value = aws_sqs_queue.jobs.url\n}\n'
# The emulator answers 501 for Athena's ListTagsForResource.
# A service the emulator does not have (404 UnknownService); the emulator grows, so this may need updating.
GAP = 'resource "aws_qldb_ledger" "gap" {\n  name             = "validate-gap"\n  permissions_mode = "STANDARD"\n}\n'
# Rejected by the provider before any API call: a genuine configuration error.
BROKEN = TABLE.replace('type = "S"\n  }\n}', 'type = "S"\n  }\n  attribute {\n    name = "unused"\n'
                                              '    type = "S"\n  }\n}')


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 900), **kw)


class E2EValidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-validate-")
        if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("docker is not accessible")
        cls.tf = os.environ.get("TERRAFORM") or shutil.which("terraform")
        if not cls.tf:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("terraform not found (PATH or TERRAFORM env var)")
        cls.data = os.path.join(cls.tmp, "data")      # shared, kept running: the "baseline" environment
        cls.data_b = os.path.join(cls.tmp, "data-b")  # no shims, started and stopped by validate itself
        for p in (PORT, PORT + 2):
            sh("docker", "rm", "-f", f"keepstack-{p}")

    @classmethod
    def tearDownClass(cls):
        for d in (cls.data, cls.data_b):
            sh(sys.executable, "-m", "keepstack", "down", "--data-dir", d, cwd=ROOT, env=CHILD_ENV)
        for p in (PORT, PORT + 2):
            sh("docker", "rm", "-f", f"keepstack-{p}")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def validate(self, name, files, *extra, data=None, port=PORT):
        d = os.path.join(self.tmp, name)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        for fname, text in files.items():
            with open(os.path.join(d, fname), "w") as fh:
                fh.write(text)
        report = os.path.join(self.tmp, name + ".json")
        r = sh(sys.executable, "-m", "keepstack", "validate", d, "--data-dir", data or self.data, "--port", str(port),
               "--terraform", self.tf, "--report", report, *extra, cwd=self.tmp, env=CHILD_ENV)
        self.assertNotEqual(r.returncode, 2, r.stdout + r.stderr)
        with open(report, encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertEqual(os.listdir(d), list(files), "validate left files behind in the Terraform directory")
        return r, rep

    def test_1_clean_config_passes(self):
        # The smoke command sees the Terraform outputs and the AWS variables pointing at keepstack.
        smoke = (f'"{sys.executable}" -c "import os,sys; '
                 "sys.exit(0 if os.environ['TF_OUTPUT_queue'].endswith('/validate-jobs') "
                 f"and os.environ['AWS_ENDPOINT_URL'].endswith(':{PORT}') else 3)\"")
        r, rep = self.validate("env", {"main.tf": TABLE + QUEUE % "validate-jobs" + OUTPUT},
                               "--shims", "--keep-running", "--run", smoke)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["steps"], {"init": "ok", "plan": "ok", "apply": "ok", "idempotency": "ok",
                                        "smoke": "ok"})
        self.assertEqual(sorted(c["action"] for c in rep["changes"]), ["create", "create"])

    def test_2_state_persists_between_runs(self):
        r, rep = self.validate("env", {"main.tf": TABLE + QUEUE % "validate-jobs" + OUTPUT})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["changes"], [], "second run must start from the first run's infrastructure")

    def test_3_replacement_is_reported_and_can_fail(self):
        r, rep = self.validate("env", {"main.tf": TABLE + QUEUE % "validate-jobs-v2" + OUTPUT}, "--fail-on-destroy")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn({"address": "aws_sqs_queue.jobs", "action": "replace", "type": "aws_sqs_queue"}, rep["changes"])
        self.assertIn("replace: aws_sqs_queue.jobs", r.stdout)

    def test_4_config_error_fails_with_location(self):
        r, rep = self.validate("broken", {"main.tf": BROKEN})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        f = rep["findings"][0]
        self.assertEqual((f["kind"], f["file"], f["address"]), ("config", "main.tf", "aws_dynamodb_table.t"))

    def test_5_emulator_gap_only_warns(self):
        r, rep = self.validate("gap", {"main.tf": GAP})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["steps"]["apply"], "gap")
        self.assertEqual(rep["findings"][0]["kind"], "emulator")
        r, rep = self.validate("gap", {"main.tf": GAP}, "--strict")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_6_drift_without_shims_fails_and_validate_stops_what_it_started(self):
        r, rep = self.validate("drift", {"main.tf": TABLE}, data=self.data_b, port=PORT + 2)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(rep["steps"]["idempotency"], "failed")
        self.assertEqual([(f["kind"], f["address"]) for f in rep["findings"]], [("drift", "aws_dynamodb_table.t")])
        status = sh(sys.executable, "-m", "keepstack", "status", "--json", "--data-dir", self.data_b,
                    cwd=ROOT, env=CHILD_ENV)
        self.assertFalse(json.loads(status.stdout)["running"], status.stdout)


if __name__ == "__main__":
    unittest.main()
