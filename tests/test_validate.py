"""Unit tests for keepstack validate: classification, parsing, rendering and the step flow (Terraform faked)."""
import argparse
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from keepstack import validate as v

COMPAT = {"aws_security_group": {"status": "broken", "workaround": "keepstack up --shims"},
          "aws_dynamodb_table": {"status": "partial", "workaround": "ignore_changes"}}


def diag(summary, address="aws_sqs_queue.q", line=3):
    return {"severity": "error", "summary": summary, "detail": "", "address": address,
            "range": {"filename": "main.tf", "start": {"line": line}}}


def jsonl(*events):
    return "\n".join(json.dumps(e) for e in events)


def planned(addr, action):
    rtype = v._type_of(addr)
    return {"type": "planned_change", "change": {"action": action, "resource": {"addr": addr, "resource_type": rtype}}}


def args(**kw):
    return argparse.Namespace(**{"path": ".", "data_dir": ".keepstack", "port": 9297, "image": "img",
                                 "shims": False, "keep_running": False, "terraform": "terraform", "timeout": 60,
                                 "destroy": False, "strict": False, "fail_on_destroy": False, **kw})


class Classify(unittest.TestCase):
    def test_501_is_an_emulator_gap(self):
        f = v.classify(diag("operation error Athena: ListTagsForResource, https response error StatusCode: 501, "
                            "api error NotImplemented: scaffolded but not implemented in this emulator"), COMPAT)
        self.assertEqual((f["kind"], f["file"], f["line"]), ("emulator", "main.tf", 3))

    def test_known_broken_resource_is_an_emulator_gap_with_its_workaround(self):
        f = v.classify(diag("InvalidPermission.NotFound", "aws_security_group.web"), COMPAT)
        self.assertEqual((f["kind"], f["hint"]), ("emulator", "keepstack up --shims"))

    def test_anything_else_is_a_config_error(self):
        f = v.classify(diag("StatusCode: 400, ValidationException: bad name"), COMPAT)
        self.assertEqual(f["kind"], "config")

    def test_type_of_handles_modules_data_and_keys(self):
        self.assertEqual(v._type_of('module.net.module.x.aws_subnet.a["eu"]'), "aws_subnet")
        self.assertEqual(v._type_of("data.aws_caller_identity.me"), "aws_caller_identity")
        self.assertEqual(v._type_of("aws_vpc.main"), "aws_vpc")


class Parsing(unittest.TestCase):
    def test_changes_skip_noop_and_read(self):
        out = jsonl(planned("aws_vpc.a", "create"), planned("aws_vpc.b", "noop"), planned("data.aws_x.y", "read"),
                    planned("aws_vpc.c", "replace"), {"type": "change_summary"}) + "\nnot json"
        self.assertEqual([(c["address"], c["action"]) for c in v.changes(v.parse_events(out))],
                         [("aws_vpc.a", "create"), ("aws_vpc.c", "replace")])

    def test_state_key_is_filesystem_safe(self):
        self.assertEqual(v.state_key(os.path.join("infra", "prod env")), "infra_prod_env")
        self.assertEqual(v.state_key("."), "root")

    def test_state_key_survives_another_windows_drive(self):
        # GitHub's Windows runners keep the checkout on D: and temp files on C:, where relpath raises.
        with mock.patch.object(v.os.path, "relpath", side_effect=ValueError("path is on mount 'C:'")):
            self.assertTrue(v.state_key("somewhere"))
            self.assertEqual(v.relpath("x"), os.path.abspath("x"))

    def test_locate_finds_root_resources_only(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "net.tf"), "w") as fh:
            fh.write('# vpc\n\nresource "aws_vpc" "main" {\n}\ndata "aws_region" "r" {}\n')
        self.assertEqual(v.locate(d, "aws_vpc.main"), ("net.tf", 3))
        self.assertEqual(v.locate(d, 'aws_vpc.main["x"]'), ("net.tf", 3))
        self.assertEqual(v.locate(d, "data.aws_region.r"), ("net.tf", 5))
        self.assertEqual(v.locate(d, "module.m.aws_vpc.main"), (None, None))

    def test_override_uses_forward_slashes(self):
        self.assertIn('path = "C:/state/x.tfstate"', v.override_text("C:\\state\\x.tfstate"))


class StepStatus(unittest.TestCase):
    def test_statuses(self):
        found = []
        self.assertEqual(v.step_status(0, "", "", COMPAT, found, "apply"), "ok")
        self.assertEqual(v.step_status(2, "", "", COMPAT, found, "idempotency"), "ok")
        self.assertEqual(v.step_status(1, jsonl({"type": "diagnostic", "diagnostic": diag("StatusCode: 501")}), "",
                                       COMPAT, found, "apply"), "gap")
        self.assertEqual(v.step_status(1, "", "Error: Invalid block", COMPAT, found, "init"), "failed")
        self.assertEqual([f["kind"] for f in found], ["emulator", "config"])
        self.assertIn("Invalid block", found[1]["message"])


class Result(unittest.TestCase):
    def rep(self, kinds=(), actions=()):
        return {"findings": [{"kind": k} for k in kinds],
                "changes": [{"action": a, "address": "x"} for a in actions]}

    def test_rules(self):
        self.assertEqual(v.result_of(self.rep(), args()), "passed")
        self.assertEqual(v.result_of(self.rep(["emulator"]), args()), "passed")
        self.assertEqual(v.result_of(self.rep(["emulator"]), args(strict=True)), "failed")
        self.assertEqual(v.result_of(self.rep(["config"]), args()), "failed")
        self.assertEqual(v.result_of(self.rep(["drift"]), args()), "failed")
        self.assertEqual(v.result_of(self.rep(actions=["delete"]), args()), "passed")
        self.assertEqual(v.result_of(self.rep(actions=["replace"]), args(fail_on_destroy=True)), "failed")


class Flow(unittest.TestCase):
    """validate() with Terraform and keepstack faked: checks the order of steps and the report."""

    def run_validate(self, script, **kw):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        tf = os.path.join(d, "tf")
        os.mkdir(tf)
        with open(os.path.join(tf, "main.tf"), "w") as fh:
            fh.write('resource "aws_dynamodb_table" "t" {\n}\n')
        calls = []

        def fake_tf(runner, *a):
            name = "idempotency" if a[0] == "plan" and "-detailed-exitcode" in a else a[0]
            if a[:2] == ("apply", "-destroy"):
                name = "destroy"
            calls.append(name)
            self.assertTrue(os.path.exists(os.path.join(tf, v.OVERRIDE)), "override missing while Terraform runs")
            return script.get(name, (0, "", ""))

        with mock.patch.object(v.Runner, "tf", fake_tf), \
                mock.patch.object(v, "running_keepstack", lambda a: contextlib.nullcontext(9297)):
            rep = v.validate(args(path=tf, data_dir=os.path.join(d, "data"), **kw))
        self.assertFalse(os.path.exists(os.path.join(tf, v.OVERRIDE)), "override left behind")
        return rep, calls

    def test_clean_run(self):
        rep, calls = self.run_validate({"plan": (0, jsonl(planned("aws_dynamodb_table.t", "create")), "")})
        self.assertEqual(calls, ["init", "plan", "apply", "idempotency"])
        self.assertEqual(rep["result"], "passed")
        self.assertEqual(rep["steps"]["idempotency"], "ok")
        self.assertEqual(len(rep["changes"]), 1)

    def test_drift_fails_and_points_at_the_resource(self):
        drift = jsonl(planned("aws_dynamodb_table.t", "update"))
        rep, _ = self.run_validate({"idempotency": (2, drift, "")}, destroy=True)
        self.assertEqual(rep["result"], "failed")
        f = rep["findings"][0]
        self.assertEqual((f["kind"], f["file"], f["line"]), ("drift", "main.tf", 1))
        self.assertIn("ignore_changes", f["hint"])  # workaround from the bundled compat.json
        self.assertEqual(rep["steps"]["destroy"], "ok")

    def test_apply_with_only_gaps_passes_and_skips_idempotency(self):
        gap = jsonl({"type": "diagnostic", "diagnostic": diag("StatusCode: 501 NotImplemented")})
        rep, calls = self.run_validate({"apply": (1, gap, "")})
        self.assertEqual(calls, ["init", "plan", "apply"])
        self.assertEqual((rep["steps"]["apply"], rep["steps"]["idempotency"], rep["result"]),
                         ("gap", "skipped", "passed"))

    def test_init_failure_stops_early(self):
        rep, calls = self.run_validate({"init": (1, "", "Error: Unsupported argument")})
        self.assertEqual(calls, ["init"])
        self.assertEqual(rep["result"], "failed")


REPORT = {"path": "infra", "result": "failed", "duration_seconds": 1.5,
          "steps": {"init": "ok", "plan": "ok", "apply": "failed"},
          "changes": [{"address": "aws_vpc.a", "action": "replace", "type": "aws_vpc"}],
          "findings": [{"kind": "config", "address": "aws_vpc.a", "message": "bad | pipe", "hint": "h",
                        "file": "main.tf", "line": 4}]}


class Render(unittest.TestCase):

    def test_text(self):
        out = v.render_text(REPORT)
        self.assertIn("FAILED", out)
        self.assertIn("! replace: aws_vpc.a", out)
        self.assertIn("config error: aws_vpc.a (main.tf:4)", out)

    def test_markdown_escapes_and_warns_about_destruction(self):
        out = v.render_markdown(REPORT)
        self.assertIn("❌ failed", out)
        self.assertIn("bad \\| pipe", out)
        self.assertIn("will be **replaced**", out)


if __name__ == "__main__":
    unittest.main()
