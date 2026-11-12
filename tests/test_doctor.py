import argparse
import contextlib
import io
import os
import tempfile
import unittest

from keepstack import doctor

ARGS = argparse.Namespace(port=9370, data_dir="does-not-exist-keepstack")


def runner(outputs):
    """Fake command runner: maps a command prefix to (rc, out); anything else is 'not found'."""
    def run(cmd, timeout=5):
        for key, val in outputs.items():
            if " ".join(cmd).startswith(key):
                return val
        return None, "not found"
    return run


def have(_):
    return "/bin/x"


def missing(_):
    return None


def levels(results):
    return {name: level for level, name, _ in results}


def details(results):
    return {name: d for _, name, d in results}


class DoctorTests(unittest.TestCase):
    def test_python(self):
        self.assertEqual(doctor.check_python((3, 11, 0))[0], doctor.OK)
        self.assertEqual(doctor.check_python((3, 9, 7))[0], doctor.FAIL)

    def test_docker_states(self):
        self.assertEqual(doctor.check_docker(runner({}), missing)[0][0], doctor.FAIL)
        down = doctor.check_docker(runner({"docker version": (1, "Cannot connect to the Docker daemon")}), have)
        self.assertEqual(down[0][0], doctor.FAIL)
        no_image = doctor.check_docker(runner({"docker version": (0, "27.0.1"), "docker image": (1, "")}), have)
        self.assertEqual([r[0] for r in no_image], [doctor.OK, doctor.WARN])
        self.assertIn("built on first", no_image[1][2])
        full = doctor.check_docker(runner({"docker version": (0, "27.0.1"), "docker image": (0, "sha")}), have)
        self.assertEqual([r[0] for r in full], [doctor.OK, doctor.OK])

    def test_port_states(self):
        self.assertEqual(doctor.check_port(1, lambda p: "free")[0], doctor.OK)
        self.assertEqual(doctor.check_port(1, lambda p: "keepstack")[0], doctor.OK)
        self.assertEqual(doctor.check_port(1, lambda p: "other")[0], doctor.FAIL)

    def test_aws_cli_versions(self):
        v1 = doctor.check_aws_cli(runner({"aws": (0, "aws-cli/1.32.0 Python/3.11 Linux")}), have)
        self.assertEqual(v1[0], doctor.FAIL)
        self.assertIn("ENDPOINT_URL", v1[2])
        v2 = doctor.check_aws_cli(runner({"aws": (0, "aws-cli/2.15.30 Python/3.11 Linux")}), have)
        self.assertEqual(v2[0], doctor.OK)
        self.assertEqual(doctor.check_aws_cli(runner({}), missing)[0], doctor.WARN)

    def test_endpoint_env(self):
        self.assertEqual(levels(doctor.check_env(9370, {}))["endpoint"], doctor.WARN)
        ok = {"AWS_ENDPOINT_URL": "http://127.0.0.1:9370"}
        self.assertEqual(levels(doctor.check_env(9370, ok))["endpoint"], doctor.OK)
        other = {"AWS_ENDPOINT_URL": "http://x:1"}
        self.assertEqual(levels(doctor.check_env(9370, other))["endpoint"], doctor.WARN)

    def test_real_credentials_flagged_and_masked(self):
        secret = "AKIAABCDEFGHIJKLMNOP"
        res = doctor.check_env(9370, {"AWS_ACCESS_KEY_ID": secret, "AWS_PROFILE": "prod"})
        self.assertEqual(levels(res)["credentials"], doctor.FAIL)
        self.assertNotIn(secret, details(res)["credentials"])
        self.assertIn("AKIA****", details(res)["credentials"])
        self.assertEqual(levels(doctor.check_env(9370, {"AWS_SESSION_TOKEN": "t"}))["credentials"], doctor.FAIL)
        self.assertEqual(levels(doctor.check_env(9370, {"AWS_ACCESS_KEY_ID": "test"}))["credentials"], doctor.OK)

    def test_terraform_and_state(self):
        tf = runner({"terraform": (0, "Terraform v1.9.0\non linux")})
        self.assertEqual(doctor.check_terraform(tf, have)[0], doctor.OK)
        self.assertEqual(doctor.check_terraform(runner({}), missing)[0], doctor.WARN)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(doctor.check_state(os.path.join(d, "none"))[0], doctor.OK)
            self.assertIn("no journal", doctor.check_state(d)[2])

    def test_cmd_doctor_never_crashes_without_tools(self):
        res = doctor.run_checks(ARGS, run=runner({}), env={}, which=missing, probe=lambda p: "free")
        self.assertEqual(levels(res)["docker"], doctor.FAIL)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor.cmd_doctor(ARGS)
        self.assertIn("docker:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
