"""E2E: Terraform + keepstack + real emulator. Skipped if docker or terraform is missing.

Tests are numbered because they share state (apply -> restart -> diff -> crash).
Env vars: TERRAFORM (path to the binary), KEEPSTACK_E2E_PORT (default 9340; emulator = +1), TF_PLUGIN_CACHE_DIR.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from typing import ClassVar

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PORT = int(os.environ.get("KEEPSTACK_E2E_PORT", "9340"))
CONTAINER = f"keepstack-{PORT}"
KEEPSTACK = [sys.executable, "-m", "keepstack", "up"]
CHILD_ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))  # run without installing the package
os.environ["PYTHONIOENCODING"] = "utf-8"  # child proxies write UTF-8 on any OS
RESTORED = re.compile(r"state restored: \d+ calls replayed, (\d+) failed")


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False,
                          timeout=kw.pop("timeout", 600), **kw)


class E2E(unittest.TestCase):
    proc = None
    logs: ClassVar[list] = []  # one log file per proxy start

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keepstack-e2e-")  # before any skip: tearDownClass does not run on SkipTest
        try:
            if sh("docker", "version", "--format", "{{.Server.Version}}").returncode != 0:
                raise unittest.SkipTest("docker is not accessible")
            cls.tf = os.environ.get("TERRAFORM") or shutil.which("terraform")
            if not cls.tf:
                raise unittest.SkipTest("terraform not found (PATH or TERRAFORM env var)")
            cls.tfdir = os.path.join(cls.tmp, "tf")
            os.mkdir(cls.tfdir)
            shutil.copy(os.path.join(ROOT, "examples", "quickstart", "main.tf"), cls.tfdir)
            cls.env = dict(os.environ, AWS_ENDPOINT_URL=f"http://127.0.0.1:{PORT}", AWS_ACCESS_KEY_ID="test",
                           AWS_SECRET_ACCESS_KEY="test", AWS_REGION="us-east-1", AWS_DEFAULT_REGION="us-east-1",
                           TF_IN_AUTOMATION="1")
            sh("docker", "rm", "-f", CONTAINER)
            cls.start_proxy("--reset")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.kill()
            cls.proc.wait()
        sh("docker", "rm", "-f", CONTAINER)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def start_proxy(cls, *extra):
        path = os.path.join(cls.tmp, f"proxy{len(cls.logs)}.log")
        cls.logs.append(path)
        with open(path, "w", encoding="utf-8") as f:
            cls.proc = subprocess.Popen(
                [*KEEPSTACK, "--port", str(PORT),
                 "--emulator-port", str(PORT + 1), "--data-dir", os.path.join(cls.tmp, "data"), *extra],
                stdout=f, stderr=subprocess.STDOUT, cwd=ROOT, env=CHILD_ENV)
        cls.wait_log("ready on", 1, 300, path)  # the first run builds the image

    @classmethod
    def log_text(cls, path=None):
        with open(path or cls.logs[-1], encoding="utf-8", errors="replace") as f:
            return f.read()

    @classmethod
    def wait_log(cls, text, count, timeout, path=None):
        end = time.time() + timeout
        while time.time() < end:
            if cls.log_text(path).count(text) >= count:
                return
            if cls.proc.poll() is not None:
                break
            time.sleep(0.5)
        raise AssertionError(f"{text!r} x{count} did not appear in the log:\n{cls.log_text(path)}")

    def terraform(self, *args, expect=(0,)):
        flags = ["-no-color"] + ([] if args[0] == "output" else ["-input=false"])  # output does not accept -input
        r = sh(self.tf, args[0], *flags, *args[1:], cwd=self.tfdir, env=self.env)
        self.assertIn(r.returncode, expect, f"terraform {args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def vpc_id(self):
        return self.terraform("output", "-raw", "vpc_id").stdout.strip()

    def test_1_apply_and_clean_plan(self):
        self.terraform("init")
        self.terraform("apply", "-auto-approve")
        self.terraform("plan", "-detailed-exitcode")
        type(self).vpc = self.vpc_id()
        self.assertTrue(self.vpc.startswith("vpc-"), self.vpc)

    def test_2_hard_reboot_restores_state(self):
        self.proc.kill()
        self.proc.wait()
        sh("docker", "rm", "-f", CONTAINER)
        self.start_proxy()
        m = RESTORED.search(self.log_text())
        self.assertTrue(m and m.group(1) == "0", self.log_text())
        self.terraform("plan", "-detailed-exitcode")
        self.assertEqual(self.vpc_id(), self.vpc)

    def test_3_plan_shows_only_the_diff(self):
        r = self.terraform("plan", "-detailed-exitcode", "-var", "extra=true", expect=(2,))
        self.assertIn("1 to add, 0 to change, 0 to destroy", r.stdout)

    def test_4_container_crash_is_restored(self):
        self.assertEqual(sh("docker", "kill", CONTAINER).returncode, 0)
        self.wait_log("the emulator stopped", 1, 60)
        self.wait_log("state restored", 2, 240)  # the 1st is from startup
        self.assertEqual(RESTORED.findall(self.log_text())[-1], "0", self.log_text())
        self.terraform("plan", "-detailed-exitcode")
        self.assertEqual(self.vpc_id(), self.vpc)

    def test_5_second_instance_exits_without_touching_journal(self):
        db = os.path.join(self.tmp, "data", "journal.db")
        count = lambda: sqlite3.connect(db).execute("select count(*) from journal").fetchone()[0]
        before = count()
        self.assertGreater(before, 0)
        t0 = time.time()
        # Same journal and --reset: the typical mistake of launching it twice. It must not delete anything.
        r = sh(*KEEPSTACK, "--port", str(PORT),
               "--emulator-port", str(PORT + 1), "--data-dir", os.path.join(self.tmp, "data"), "--reset", timeout=30, cwd=ROOT, env=CHILD_ENV)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("already in use", r.stdout)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(count(), before)
        self.terraform("plan", "-detailed-exitcode")  # the original proxy is still serving


if __name__ == "__main__":
    unittest.main()
