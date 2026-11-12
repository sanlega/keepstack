import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from keepstack import shims

EGRESS = ("Action=RevokeSecurityGroupEgress&GroupId=sg-1&IpPermissions.1.FromPort=0&IpPermissions.1.IpProtocol=-1"
          "&IpPermissions.1.IpRanges.1.CidrIp=0.0.0.0%2F0&IpPermissions.1.ToPort=0&Version=2016-11-15")
MULTI = ("Action=AuthorizeSecurityGroupIngress&GroupId=sg-1"
         "&IpPermissions.1.IpProtocol=tcp&IpPermissions.1.FromPort=443&IpPermissions.1.ToPort=443"
         "&IpPermissions.2.IpProtocol=-1&IpPermissions.2.FromPort=0&IpPermissions.2.ToPort=0"
         "&IpPermissions.3.IpProtocol=udp&IpPermissions.3.FromPort=53&IpPermissions.3.ToPort=53")


class FakeApp:
    def __init__(self, flag=False, meta=None):
        self.args = SimpleNamespace(shims=flag)
        self.meta = dict(meta or {})

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value


def req(app, op, body):
    return shims.on_request(app, op, "POST", "/", {}, body.encode())[3].decode()


def quiet(fn, *a):
    with redirect_stdout(io.StringIO()) as out:
        fn(*a)
    return out.getvalue()


class RequestShim(unittest.TestCase):
    def test_all_protocol_ports_removed_order_kept(self):
        self.assertEqual(req(FakeApp(True), "RevokeSecurityGroupEgress", EGRESS),
                         "Action=RevokeSecurityGroupEgress&GroupId=sg-1&IpPermissions.1.IpProtocol=-1"
                         "&IpPermissions.1.IpRanges.1.CidrIp=0.0.0.0%2F0&Version=2016-11-15")

    def test_only_minus_one_permissions_change(self):
        out = req(FakeApp(True), "AuthorizeSecurityGroupIngress", MULTI)
        self.assertEqual(out, MULTI.replace("&IpPermissions.2.FromPort=0&IpPermissions.2.ToPort=0", ""))

    def test_other_services_and_actions_untouched(self):
        app = FakeApp(True)
        self.assertEqual(req(app, "DescribeSecurityGroups", EGRESS), EGRESS)
        self.assertEqual(req(app, None, '{"IpProtocol":"-1"}'), '{"IpProtocol":"-1"}')

    def test_disabled_is_passthrough(self):
        self.assertEqual(req(FakeApp(False), "RevokeSecurityGroupEgress", EGRESS), EGRESS)


def table(**extra):
    return {"Table": {"TableName": "t", "TableStatus": "ACTIVE", **extra}}


ODT = {"OnDemandThroughput": {"MaxReadRequestUnits": -1, "MaxWriteRequestUnits": -1}}


def resp(app, op, doc):
    raw = json.dumps(doc).encode()
    out = shims.on_response(app, op, 200, raw)
    return raw, out


class ResponseShim(unittest.TestCase):
    def test_both_wrappers_and_gsi(self):
        on = FakeApp(True)
        for op, doc in (("DescribeTable", table(**ODT)), ("CreateTable", {"TableDescription": {"TableName": "t", **ODT}}),
                        ("UpdateTable", {"TableDescription": {"GlobalSecondaryIndexes": [{"IndexName": "i", **ODT}]}})):
            _, out = resp(on, op, doc)
            self.assertNotIn(b"OnDemandThroughput", out, op)
        self.assertEqual(json.loads(resp(on, "DescribeTable", table(**ODT))[1]), table())

    def test_real_limits_untouched(self):
        raw, out = resp(FakeApp(True), "DescribeTable",
                        table(OnDemandThroughput={"MaxReadRequestUnits": 100, "MaxWriteRequestUnits": -1}))
        self.assertEqual(raw, out)

    def test_disabled_and_other_ops_untouched(self):
        raw, out = resp(FakeApp(False), "DescribeTable", table(**ODT))
        self.assertEqual(raw, out)
        raw, out = resp(FakeApp(True), "GetItem", table(**ODT))
        self.assertEqual(raw, out)
        self.assertEqual(shims.on_response(FakeApp(True), "DescribeTable", 200, b"not json OnDemandThroughput"),
                         b"not json OnDemandThroughput")


class Enablement(unittest.TestCase):
    def test_first_use_persists_and_logs(self):
        app = FakeApp(True)
        out = quiet(shims.enabled, app)
        self.assertEqual(app.meta, {"shims": "1"})
        self.assertIn("compatibility shims enabled: ec2-ipprotocol-all-ports", out)
        self.assertEqual(quiet(shims.enabled, app), "")  # cached: no second log

    def test_remembered_without_flag(self):
        app = FakeApp(False, {"shims": "1"})
        out = quiet(shims.enabled, app)
        self.assertTrue(app.shims_on)
        self.assertIn("remembered", out)

    def test_off_by_default_writes_nothing(self):
        app = FakeApp()
        self.assertEqual(quiet(shims.enabled, app), "")
        self.assertFalse(app.shims_on)
        self.assertEqual(app.meta, {})

    def test_shim_logs_once_per_process(self):
        shims._logged.clear()
        app = FakeApp(True)
        quiet(shims.enabled, app)
        first = quiet(req, app, "RevokeSecurityGroupEgress", EGRESS)
        self.assertIn("shim ec2-ipprotocol-all-ports applied", first)
        self.assertEqual(quiet(req, app, "RevokeSecurityGroupEgress", EGRESS), "")


if __name__ == "__main__":
    unittest.main()
