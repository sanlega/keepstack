"""Unit tests for the dashboard (no Docker): a fake app over a temp journal, calling dashboard.handle directly."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from argparse import Namespace

from keepstack import compact, dashboard, proxy, snapshot

PORT = 9391
HOST = {"Host": f"127.0.0.1:{PORT}"}
VPC = "vpc-0a1b2c3d4e5f60718"


def auth(service):
    return {"Authorization": f"AWS4-HMAC-SHA256 Credential=AKID/20260101/us-east-1/{service}/aws4_request, "
                             "SignedHeaders=host, Signature=abc"}


def ec2(action, params, resp):
    body = f"Action={action}&Version=2016-11-15&" + params
    return (action, "POST", "/", json.dumps(dict(auth("ec2"), **{"Content-Type": "application/x-www-form-urlencoded"})),
            body.encode(), 200, resp.encode())


ROWS = [
    ec2("CreateVpc", "CidrBlock=10.0.0.0%2F16", f"<CreateVpcResponse><vpc><vpcId>{VPC}</vpcId></vpc></CreateVpcResponse>"),
    ec2("CreateSubnet", f"VpcId={VPC}&CidrBlock=10.0.1.0%2F24",
        "<CreateSubnetResponse><subnet><subnetId>subnet-0123456789abcdef0</subnetId></subnet></CreateSubnetResponse>"),
    ec2("CreateVpc", "CidrBlock=10.9.0.0%2F16", "<r><vpc><vpcId>vpc-0deadbeefdeadbeef</vpcId></vpc></r>"),
    ec2("DeleteVpc", "VpcId=vpc-0deadbeefdeadbeef", "<DeleteVpcResponse><return>true</return></DeleteVpcResponse>"),
    ("CreateQueue", "POST", "/", json.dumps(dict(auth("sqs"), **{"X-Amz-Target": "AmazonSQS.CreateQueue"})),
     b'{"QueueName": "orders-queue"}', 200, b'{"QueueUrl": "http://localhost:9391/000000000000/orders-queue"}'),
    (None, "PUT", "/my-bucket", json.dumps(auth("s3")), b"", 200, b""),
    (None, "PUT", "/my-bucket/key.txt", json.dumps(auth("s3")), b"hello", 200, b""),
]


class FakeApp:
    def __init__(self, data_dir):
        self.args = Namespace(port=PORT, data_dir=data_dir)
        self.db_path = os.path.join(data_dir, "journal.db")
        self.ready = threading.Event()
        self.ready.set()
        self.ids = proxy.IdMap()
        self.ids.to_new[b"vpc-0a1b2c3d4e5f60718"] = b"vpc-9999999999999999"
        db = sqlite3.connect(self.db_path)
        db.execute("create table journal(seq integer primary key autoincrement, ts real, op text, method text,"
                   " path text, headers text, body blob, status int, resp blob)")
        db.execute("create table meta(key text primary key, value text)")
        for i, (op, method, path, headers, body, status, resp) in enumerate(ROWS):
            db.execute("insert into journal(ts, op, method, path, headers, body, status, resp) values(?,?,?,?,?,?,?,?)",
                       (1700000000 + i, op, method, path, headers, body, status, resp))
        db.execute("insert into meta values('last_restore', ?)",
                   (json.dumps({"ts": 1700000000.0, "replayed": 7, "failed": 0, "translated": 1}),))
        db.commit()
        db.close()


def get(app, path, host=HOST, method="GET"):
    status, _, headers, body = dashboard.handle(app, method, path, host)
    return status, dict(headers), body


def api(app, path):
    status, _, body = get(app, dashboard.API + path)
    assert status == 200, (status, body)
    return json.loads(body)


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = FakeApp(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_and_security_headers(self):
        status, h, body = get(self.app, "/__keepstack/")
        self.assertEqual(status, 200)
        self.assertIn(b"keepstack", body)
        self.assertTrue(h["Content-Type"].startswith("text/html"))
        self.assertNotIn("Access-Control-Allow-Origin", h)
        self.assertEqual((h["X-Content-Type-Options"], h["Referrer-Policy"], h["Cache-Control"]),
                         ("nosniff", "no-referrer", "no-store"))
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertNotIn("http", h["Content-Security-Policy"])
        status, h, _ = get(self.app, "/__keepstack")
        self.assertEqual((status, h["Location"]), (301, "/__keepstack/"))
        self.assertEqual(get(self.app, "/__keepstack/nope")[0], 404)

    def test_host_check(self):
        for host in ({"Host": "evil.example:9391"}, {"Host": "127.0.0.1:1"}, {"Host": "127.0.0.1"}, {}):
            self.assertEqual(get(self.app, "/__keepstack/", host)[0], 403)
            self.assertEqual(get(self.app, dashboard.API + "status", host)[0], 403)
        self.assertEqual(get(self.app, "/__keepstack/", {"host": f"localhost:{PORT}"})[0], 200)

    def test_only_get(self):
        for m in ("POST", "PUT", "DELETE"):
            status, h, _ = get(self.app, dashboard.API + "status", method=m)
            self.assertEqual(status, 405)
            self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_status(self):
        st = api(self.app, "status")
        self.assertEqual((st["state"], st["port"]), ("running", PORT))
        self.assertEqual(st["journal"]["calls"], len(ROWS))
        self.assertEqual(st["services"], {"ec2": 4, "sqs": 1, "s3": 2})
        self.assertEqual(st["last_restore"]["replayed"], 7)
        self.assertFalse(st["shims"])
        self.app.ready.clear()
        self.assertEqual(api(self.app, "status")["state"], "restoring")

    def test_resources_exclude_deleted(self):
        res = api(self.app, "resources")["resources"]
        by_kind = {r["kind"]: r for r in res}
        self.assertEqual(sorted(by_kind), ["Bucket", "Queue", "Subnet", "Vpc"])
        self.assertEqual(by_kind["Vpc"]["ids"][0], VPC)  # the deleted VPC is gone
        self.assertEqual(by_kind["Vpc"]["calls"], 2)  # CreateVpc + the CreateSubnet that mentions it
        self.assertIn("orders-queue", by_kind["Queue"]["ids"])
        self.assertEqual((by_kind["Bucket"]["service"], by_kind["Bucket"]["ids"]), ("s3", ["my-bucket"]))
        self.assertEqual(by_kind["Bucket"]["calls"], 2)
        self.assertEqual(by_kind["Queue"]["created"], 1700000004)

    def test_journal_pagination_and_filter(self):
        j = api(self.app, "journal?limit=3&offset=0")
        self.assertEqual((j["total"], [r["seq"] for r in j["rows"]]), (len(ROWS), [7, 6, 5]))
        self.assertNotIn("body", j["rows"][0])
        self.assertEqual(j["rows"][0]["label"], "PUT /my-bucket/key.txt")
        self.assertEqual(api(self.app, "journal?limit=3&offset=6")["rows"][0]["seq"], 1)
        j = api(self.app, "journal?service=ec2")
        self.assertEqual((j["total"], [r["label"] for r in j["rows"]]),
                         (4, ["DeleteVpc", "CreateVpc", "CreateSubnet", "CreateVpc"]))
        self.assertEqual(api(self.app, "journal?limit=abc")["limit"], 100)

    def test_journal_detail(self):
        d = api(self.app, "journal/5")
        self.assertEqual((d["op"], d["service"], d["status"]), ("CreateQueue", "sqs", 200))
        self.assertIn("orders-queue", d["request"])
        self.assertIn("QueueUrl", d["response"])
        self.assertEqual(get(self.app, dashboard.API + "journal/999")[0], 404)
        db = sqlite3.connect(self.app.db_path)
        db.execute("update journal set resp=? where seq=7", (b"x" * 100_000,))
        db.commit()
        db.close()
        d = api(self.app, "journal/7")
        self.assertEqual((len(d["response"]), d["response_truncated"], d["request_truncated"]),
                         (dashboard.DETAIL_LIMIT, True, False))

    def test_ids_and_snapshots(self):
        self.assertEqual(api(self.app, "ids"), {"total": 1, "rows": [{"original": VPC, "current": "vpc-9999999999999999"}]})
        self.assertEqual(api(self.app, "snapshots"), {"snapshots": []})
        snapshot.save_snapshot(self.tmp.name, "before", force=True)
        s = api(self.app, "snapshots")["snapshots"]
        self.assertEqual((s[0]["name"], s[0]["calls"]), ("before", len(ROWS)))

    def test_nothing_is_written(self):
        def digest():
            with open(self.app.db_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        before, mtime = digest(), os.path.getmtime(self.app.db_path)
        time.sleep(0.05)
        for p in ("status", "resources", "journal", "journal/1", "ids", "snapshots"):
            api(self.app, p)
        get(self.app, "/__keepstack/")
        self.assertEqual(digest(), before)
        self.assertEqual(os.path.getmtime(self.app.db_path), mtime)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith("journal.db-")], [])


if __name__ == "__main__":
    unittest.main()


class Identifiers(unittest.TestCase):
    """Regression: enums (HASH, PAY_PER_REQUEST) and descriptions were shown as resource identifiers."""

    def row(self, op, body, resp):
        return compact.Row(1, op, "POST", "/", json.dumps({}), body, 200, resp)

    def test_dynamodb_table_uses_table_name_and_arn(self):
        body = json.dumps({"TableName": "orders", "BillingMode": "PAY_PER_REQUEST",
                           "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}]}).encode()
        resp = json.dumps({"TableDescription": {"TableName": "orders", "BillingModeSummary": {
            "BillingMode": "PAY_PER_REQUEST"}, "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
            "TableArn": "arn:aws:dynamodb:us-east-1:000000000000:table/orders"}}).encode()
        self.assertEqual(dashboard._identifiers(self.row("CreateTable", body, resp), "Table"),
                         ["orders", "arn:aws:dynamodb:us-east-1:000000000000:table/orders"])

    def test_kms_key_uses_arn_not_description(self):
        body = json.dumps({"Description": "app"}).encode()
        resp = json.dumps({"KeyMetadata": {"KeyId": "3c99699e-5b80-4832-8d42-383765c46ed8", "Description": "app",
                           "Arn": "arn:aws:kms:us-east-1:000000000000:key/3c99699e-5b80-4832-8d42-383765c46ed8"}})
        ids = dashboard._identifiers(self.row("CreateKey", body, resp.encode()), "Key")
        self.assertNotIn("app", ids)
        self.assertEqual(ids, ["arn:aws:kms:us-east-1:000000000000:key/3c99699e-5b80-4832-8d42-383765c46ed8"])

    def test_query_protocol_name_field(self):
        body = b"Action=CreateQueue&QueueName=orders-queue&Version=2012-11-05"
        resp = b"<QueueUrl>https://sqs.us-east-1.amazonaws.com/000000000000/orders-queue</QueueUrl>"
        self.assertEqual(dashboard._identifiers(self.row("CreateQueue", body, resp), "Queue"), ["orders-queue"])
