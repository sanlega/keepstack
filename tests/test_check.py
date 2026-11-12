import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

from keepstack import check

TF = 'resource "aws_vpc" "a" {}\nresource "aws_subnet" "s1" {}\nresource "aws_subnet" "s2" {}\n'


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def args(path, **kw):
    return argparse.Namespace(path=path, plan=kw.get("plan"), offline=kw.get("offline", True), json=kw.get("json", False))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def run_check(self, a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = check.cmd_check(a)
        return code, buf.getvalue()

    def test_scan_nested_ignores_terraform_dir_and_reports_remote_modules(self):
        write(self.root, "main.tf", TF + 'module "r" {\n  source = "terraform-aws-modules/vpc/aws"\n}\n'
              'module "l" {\n  source = "./local"\n}\n')
        write(self.root, "sub/more.tf", 'resource "aws_sqs_queue" "q" {}\n')
        write(self.root, ".terraform/modules/x/main.tf", 'resource "aws_instance" "ignored" {}\n')
        types, remote = check.scan_dir(self.root)
        self.assertEqual(dict(types), {"aws_vpc": 1, "aws_subnet": 2, "aws_sqs_queue": 1})
        self.assertEqual(remote, [("r", "terraform-aws-modules/vpc/aws")])

    def test_plan_with_child_modules(self):
        plan = {"planned_values": {"root_module": {
            "resources": [{"type": "aws_vpc"}],
            "child_modules": [{"resources": [{"type": "aws_subnet"}, {"type": "random_id"}],
                               "child_modules": [{"resources": [{"type": "aws_subnet"}]}]}]}}}
        types, remote = check.scan_plan(write(self.root, "plan.json", json.dumps(plan)))
        self.assertEqual(dict(types), {"aws_vpc": 1, "aws_subnet": 2})
        self.assertEqual(remote, [])

    def test_plan_resource_changes_skip_deletes(self):
        plan = {"resource_changes": [{"type": "aws_vpc", "change": {"actions": ["create"]}},
                                     {"type": "aws_subnet", "change": {"actions": ["delete"]}}]}
        types, _ = check.scan_plan(write(self.root, "p.json", json.dumps(plan)))
        self.assertEqual(dict(types), {"aws_vpc": 1})

    def test_service_mapping(self):
        expected = {"aws_vpc": "ec2", "aws_subnet": "ec2", "aws_route": "ec2", "aws_route_table": "ec2",
                    "aws_vpc_security_group_egress_rule": "ec2", "aws_eip": "ec2",
                    "aws_cloudwatch_log_group": "logs", "aws_cloudwatch_event_rule": "events",
                    "aws_apigatewayv2_api": "apigatewayv2", "aws_iam_role": "iam", "aws_lambda_permission": "lambda",
                    "aws_s3_bucket_policy": "s3", "aws_route53_zone": "route53", "aws_sqs_queue": "sqs"}
        for rtype, svc in expected.items():
            self.assertEqual(check.service_of(rtype), svc, rtype)

    def test_classification(self):
        write(self.root, "main.tf", 'resource "aws_security_group" "g" {}\nresource "aws_dynamodb_table" "t" {}\n'
              'resource "aws_vpc" "v" {}\nresource "aws_mq_broker" "b" {}\n')
        rep = check.build_report(args(self.root))
        status = {r["type"]: r["status"] for r in rep["resources"]}
        self.assertEqual(status, {"aws_security_group": "broken", "aws_dynamodb_table": "partial",
                                  "aws_vpc": "ok", "aws_mq_broker": "untested"})
        ddb = next(r for r in rep["resources"] if r["type"] == "aws_dynamodb_table")
        self.assertIn("ignore_changes", ddb["workaround"])

    def test_parse_issue(self):
        self.assertEqual(check.parse_issue("20261022T193451Z-ec2-createsubnet-stub.md"), ("ec2", "createsubnet stub"))
        self.assertEqual(check.parse_issue("20261101T000000Z-ds-directory-creation-skips-x.md"),
                         ("ds", "directory creation skips x"))
        self.assertIsNone(check.parse_issue("README.md"))

    def test_gaps_only_for_used_services_with_example_cap(self):
        names = [f"20261022T19345{i}Z-ec2-gap-{i}.md" for i in range(7)] + ["20261022T193451Z-ds-other.md"]
        gaps, note = check.known_gaps({"ec2"}, lambda: names)
        self.assertIsNone(note)
        self.assertEqual(gaps["ec2"]["count"], 7)
        self.assertEqual(len(gaps["ec2"]["examples"]), 5)
        self.assertNotIn("ds", gaps)

    def test_fetch_failure_degrades_to_note(self):
        def boom():
            raise OSError("offline")
        write(self.root, "main.tf", TF)
        rep = check.build_report(args(self.root, offline=False), fetch=boom)
        self.assertIsNone(rep["gaps"])
        self.assertIn("offline", rep["gaps_note"])

    def test_offline_never_fetches(self):
        write(self.root, "main.tf", TF)

        def boom():
            raise AssertionError("network used")
        rep = check.build_report(args(self.root, offline=True), fetch=boom)
        self.assertIsNone(rep["gaps"])
        self.assertIsNone(rep["gaps_note"])

    def test_json_output_and_exit_code(self):
        write(self.root, "main.tf", 'resource "aws_security_group" "g" {}\n')
        code, out = self.run_check(args(self.root, json=True))
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(data["summary"]["broken"], 1)
        self.assertEqual(set(data), {"source", "path", "emulator_version", "resources", "modules_not_analysed",
                                     "gaps", "gaps_note", "summary"})

    def test_exit_zero_and_human_output(self):
        write(self.root, "main.tf", TF)
        code, out = self.run_check(args(self.root))
        self.assertEqual(code, 0)
        self.assertIn("aws_subnet", out)
        self.assertIn("x2", out)

    def test_missing_path(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check.cmd_check(args(os.path.join(self.root, "nope"))), 2)


if __name__ == "__main__":
    unittest.main()
