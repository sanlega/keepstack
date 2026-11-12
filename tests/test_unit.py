import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from keepstack.proxy import IdMap, is_write, operation


class TestClassify(unittest.TestCase):
    def test_operations(self):
        self.assertEqual(operation("/", {"X-Amz-Target": "DynamoDB_20120810.CreateTable"}, b""), "CreateTable")
        self.assertEqual(operation("/", {"Content-Type": "application/x-www-form-urlencoded"},
                                   b"Action=CreateVpc&CidrBlock=10.0.0.0%2F16"), "CreateVpc")
        self.assertEqual(operation("/?Action=DescribeVpcs", {}, b""), "DescribeVpcs")
        self.assertEqual(operation("/service/X/operation/ListThings", {}, b""), "ListThings")
        self.assertIsNone(operation("/my-bucket", {}, b""))

    def test_lowercase_headers(self):  # JS SDK v3 and Rust
        self.assertEqual(operation("/", {"x-amz-target": "DynamoDB_20120810.ListTables"}, b""), "ListTables")
        self.assertEqual(operation("/", {"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
                                   b"Action=DescribeVpcs"), "DescribeVpcs")

    def test_write(self):
        self.assertTrue(is_write("POST", "CreateVpc"))
        self.assertFalse(is_write("POST", "DescribeVpcs"))
        self.assertFalse(is_write("POST", "GetCallerIdentity"))
        self.assertTrue(is_write("POST", "ReceiveMessage"))  # changes visibility and receipt handles
        self.assertTrue(is_write("PUT", None))      # S3 CreateBucket
        self.assertFalse(is_write("GET", None))


class TestIdMap(unittest.TestCase):
    def test_ec2_roundtrip(self):
        m = IdMap()
        self.assertEqual(m.learn(b"<vpcId>vpc-0aaaaaaaaaaaaaaaa</vpcId>", b"<vpcId>vpc-0bbbbbbbbbbbbbbbb</vpcId>"), 1)
        self.assertEqual(m.to_emulator(b"VpcId=vpc-0aaaaaaaaaaaaaaaa"), b"VpcId=vpc-0bbbbbbbbbbbbbbbb")
        self.assertEqual(m.to_client(b"vpc-0bbbbbbbbbbbbbbbb"), b"vpc-0aaaaaaaaaaaaaaaa")
        self.assertEqual(m.to_client(b"us-east-1 arn:aws:s3:::x"), b"us-east-1 arn:aws:s3:::x")

    def test_uuid_arn_and_url_encoded(self):  # KMS: the KeyId appears bare, inside an ARN and URL-encoded
        old, new = b"9020e6d1-1111-4222-8333-944444444444", b"df24569c-5555-4666-8777-988888888888"
        m = IdMap()
        m.learn(b'{"KeyMetadata":{"KeyId":"%s","Arn":"arn:aws:kms:us-east-1:000000000000:key/%s"}}' % (old, old),
                b'{"KeyMetadata":{"KeyId":"%s","Arn":"arn:aws:kms:us-east-1:000000000000:key/%s"}}' % (new, new))
        self.assertEqual(m.to_emulator(b'{"TargetKeyId":"%s"}' % old), b'{"TargetKeyId":"%s"}' % new)
        self.assertEqual(m.to_emulator(b"KeyId=arn%3Aaws%3Akms%3Aus-east-1%3A0%3Akey%2F" + old),
                         b"KeyId=arn%3Aaws%3Akms%3Aus-east-1%3A0%3Akey%2F" + new)

    def test_route53_and_secret_suffix(self):
        m = IdMap()
        m.learn(b"<Id>/hostedzone/Z6MRFU8VFM0J31Q</Id>", b"<Id>/hostedzone/ZQ1W2E3R4T5Y6U7</Id>")
        m.learn(b'{"ARN":"arn:aws:secretsmanager:us-east-1:0:secret:db-pass-9xGQim"}',
                b'{"ARN":"arn:aws:secretsmanager:us-east-1:0:secret:db-pass-AbCdEf"}')
        self.assertEqual(m.to_emulator(b"/2013-04-01/hostedzone/Z6MRFU8VFM0J31Q/rrset"),
                         b"/2013-04-01/hostedzone/ZQ1W2E3R4T5Y6U7/rrset")
        self.assertEqual(m.to_client(b"secret:db-pass-AbCdEf"), b"secret:db-pass-9xGQim")

    def test_ignores_timestamps_request_ids_and_short_values(self):
        m = IdMap()
        m.learn(b'{"RequestId":"00000000-0000-0000-0000-000000000001","Created":"2026-11-12T13:01:36Z","State":"on","T":1731412896.12}',
                b'{"RequestId":"00000000-0000-0000-0000-000000000009","Created":"2026-11-13T09:44:02Z","State":"no","T":1731499442.98}')
        self.assertEqual(m.to_new, {})

    def test_structure_differs_still_learns(self):
        m = IdMap()
        self.assertEqual(m.learn(b'{"Id":"abc123def456"}', b'{"Id":"zzz999yyy888","Extra":"new"}'), 1)
        self.assertEqual(m.to_emulator(b"abc123def456"), b"zzz999yyy888")

    def test_status_words_and_free_text_are_not_ids(self):  # regression: CREATING->ACTIVE broke DescribeTable
        m = IdMap()
        m.learn(b'{"TableStatus":"CREATING","Note":"bucket created properly","Size":"1.731412E9"}',
                b'{"TableStatus":"ACTIVE","Note":"profile attached","Extra":"x","Size":"1.731499E9"}')
        self.assertEqual(m.to_new, {})

    def test_base64_blob_is_one_unit(self):  # regression: old and new chunks got mixed
        m = IdMap()
        old, new = b"AAAAAAAAAA/bC+DDDDDDDDDD==", b"EEEEEEEEEE/fG+HHHHHHHHHH=="
        m.learn(b'{"CiphertextBlob":"%s"}' % old, b'{"CiphertextBlob":"%s"}' % new)
        self.assertEqual(m.to_client(b'"%s"' % new), b'"%s"' % old)
        self.assertEqual(m.to_emulator(b'"%s"' % old), b'"%s"' % new)

    def test_large_unequal_responses_are_fast(self):  # regression: quadratic difflib blocked startup
        items = b",".join(b'{"Name":"item%d","Arn":"arn:x:%d"}' % (i, i) for i in range(20000))
        t0 = time.time()
        IdMap().learn(b"[" + items + b"]", b"[" + items + b',{"Name":"extra1x"}]')
        self.assertLess(time.time() - t0, 2)

    def test_collision_gets_unique_alias(self):
        # After a restart, the emulator gives a NEW resource an ID the client already associates with another.
        m = IdMap()
        m.learn(b"igw-0000000000000008e", b"igw-0000000000000008d")
        alias = m.to_client(b"igw-0000000000000008e")
        self.assertNotIn(alias, (b"igw-0000000000000008e", b"igw-0000000000000008d"))
        self.assertTrue(alias.startswith(b"igw-"))
        self.assertEqual(m.to_emulator(alias), b"igw-0000000000000008e")             # the new one
        self.assertEqual(m.to_emulator(b"igw-0000000000000008e"), b"igw-0000000000000008d")  # the original
        self.assertEqual(m.to_client(b"igw-0000000000000008e"), alias)               # stable


if __name__ == "__main__":
    unittest.main()
