import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from keepstack.compact import Row, compact_rows

FORM = json.dumps({"Content-Type": "application/x-www-form-urlencoded"})


class Journal:
    def __init__(self):
        self.rows = []

    def add(self, op, body=b"", resp=b"", method="POST", path="/", headers=FORM, status=200):
        self.rows.append(Row(len(self.rows) + 1, op, method, path, headers, body, status, resp))
        return len(self.rows)

    def ec2(self, action, params="", resp=""):
        return self.add(action, f"Action={action}&Version=2016-11-15{params}".encode(), resp.encode())

    def sqs(self, op, body, resp=b""):
        return self.add(op, json.dumps(body).encode(), resp, headers=json.dumps({"X-Amz-Target": "AmazonSQS." + op}))

    def rest(self, method, path, status=200):
        return self.add(None, b"data", b"", method=method, path=path, headers="{}", status=status)

    def run(self):
        kept, groups, self.shared = compact_rows(self.rows)
        return [r.seq for r in kept], groups


class CompactTests(unittest.TestCase):
    def test_vpc_created_and_deleted(self):
        j = Journal()
        keep = j.ec2("CreateVpc", "&CidrBlock=10.9.0.0%2F16", "<vpcId>vpc-0keep111</vpcId>")
        c = j.ec2("CreateVpc", "&CidrBlock=10.0.0.0%2F16", "<vpcId>vpc-0abc123456</vpcId>")
        tag = j.ec2("CreateTags", "&ResourceId.1=vpc-0abc123456&Tag.1.Key=Environment", "<return>true</return>")
        other = j.ec2("CreateTags", "&ResourceId.1=vpc-0keep111&Tag.1.Key=Environment", "<return>true</return>")
        d = j.ec2("DeleteVpc", "&VpcId=vpc-0abc123456", "<return>true</return>")
        kept, groups = j.run()
        self.assertEqual(kept, [keep, other])
        self.assertEqual(groups[0]["seqs"], [c, tag, d])
        self.assertIn("vpc-0abc123456", groups[0]["label"])
        self.assertIn("3 calls", groups[0]["label"])

    def test_nested_subnet_and_vpc(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        j.ec2("CreateSubnet", "&VpcId=vpc-0abc123456", "<subnetId>subnet-0def456789</subnetId><vpcId>vpc-0abc123456</vpcId>")
        j.ec2("ModifySubnetAttribute", "&SubnetId=subnet-0def456789&MapPublicIpOnLaunch.Value=true", "")
        j.ec2("DeleteSubnet", "&SubnetId=subnet-0def456789")
        j.ec2("DeleteVpc", "&VpcId=vpc-0abc123456")
        kept, groups = j.run()
        self.assertEqual(kept, [])
        self.assertEqual(len(groups), 2)

    def test_created_and_not_deleted_is_kept(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        j.ec2("CreateTags", "&ResourceId.1=vpc-0abc123456&Tag.1.Key=Environment", "<return>true</return>")
        kept, groups = j.run()
        self.assertEqual(kept, [1, 2])
        self.assertEqual(groups, [])

    def test_delete_pairs_with_the_right_create(self):
        j = Journal()
        a = j.ec2("CreateVpc", "", "<vpcId>vpc-0aaaa1111</vpcId>")
        b = j.ec2("CreateVpc", "", "<vpcId>vpc-0bbbb2222</vpcId>")
        j.ec2("DeleteVpc", "&VpcId=vpc-0aaaa1111")
        kept, _ = j.run()
        self.assertEqual(kept, [b])
        self.assertNotIn(a, kept)

    def test_unrelated_rows_between_are_kept(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        mid = j.sqs("CreateQueue", {"QueueName": "jobs"}, b'{"QueueUrl":"http://x/0000/jobs"}')
        j.ec2("DeleteVpc", "&VpcId=vpc-0abc123456")
        self.assertEqual(j.run()[0], [mid])

    def test_unconfirmed_rows_dropped(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        j.add("CreateVpc", b"Action=CreateVpc", b"", status=0)
        kept, groups = j.run()
        self.assertEqual(kept, [1])
        self.assertIn("unconfirmed", groups[0]["label"])

    def test_sqs_json_queue_by_url(self):
        j = Journal()
        url = "http://127.0.0.1:9297/000000000000/jobs"
        j.sqs("CreateQueue", {"QueueName": "jobs"}, json.dumps({"QueueUrl": url}).encode())
        j.sqs("SendMessage", {"QueueUrl": url, "MessageBody": "hi"}, b"{}")
        j.sqs("DeleteQueue", {"QueueUrl": url})
        keep = j.sqs("CreateQueue", {"QueueName": "other"}, b'{"QueueUrl":"http://127.0.0.1:9297/000000000000/other"}')
        kept, groups = j.run()
        self.assertEqual(kept, [keep])
        self.assertEqual(groups[0]["seqs"], [1, 2, 3])

    def test_tags_are_not_a_resource(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        j.ec2("CreateTags", "&ResourceId.1=vpc-0abc123456&Tag.1.Key=Environment", "<return>true</return>")
        j.ec2("DeleteTags", "&ResourceId.1=vpc-0abc123456&Tag.1.Key=Environment", "<return>true</return>")
        self.assertEqual(j.run()[0], [1, 2, 3])

    def test_s3_object_put_delete(self):
        j = Journal()
        j.rest("PUT", "/bkt")
        j.rest("PUT", "/bkt/a.txt")
        j.rest("PUT", "/bkt/b.txt")
        j.rest("DELETE", "/bkt/a.txt", status=204)
        kept, groups = j.run()
        self.assertEqual(kept, [1, 3])
        self.assertEqual(groups[0]["seqs"], [2, 4])

    def test_s3_bucket_delete_takes_its_config(self):
        j = Journal()
        j.rest("PUT", "/bkt")
        j.rest("PUT", "/bkt?tagging")
        j.rest("PUT", "/other")
        j.rest("DELETE", "/bkt", status=204)
        self.assertEqual(j.run()[0], [3])

    def test_versioned_bucket_untouched(self):
        j = Journal()
        j.rest("PUT", "/ver")
        j.rest("PUT", "/ver?versioning")
        j.rest("PUT", "/ver/a.txt")
        j.rest("DELETE", "/ver/a.txt", status=204)
        self.assertEqual(j.run()[0], [1, 2, 3, 4])

    def test_multi_resource_tags_keep_the_resource(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        j.ec2("CreateVpc", "", "<vpcId>vpc-0keep99999</vpcId>")
        j.ec2("CreateTags", "&ResourceId.1=vpc-0abc123456&ResourceId.2=vpc-0keep99999&Tag.1.Key=Env", "")
        j.ec2("DeleteVpc", "&VpcId=vpc-0abc123456")
        kept, groups = j.run()
        self.assertEqual(kept, [1, 2, 3, 4])
        self.assertEqual(groups, [])
        self.assertIn("vpc-0abc123456", j.shared[0])

    def test_single_resource_tags_still_compacted(self):
        j = Journal()
        j.ec2("CreateVpc", "", "<vpcId>vpc-0abc123456</vpcId>")
        j.ec2("CreateTags", "&ResourceId.1=vpc-0abc123456&Tag.1.Key=Env&Tag.1.Value=dev", "")
        j.ec2("DeleteVpc", "&VpcId=vpc-0abc123456")
        kept, _ = j.run()
        self.assertEqual(kept, [])
        self.assertEqual(j.shared, [])

    def test_s3_copy_source_keeps_both(self):
        copy = json.dumps({"x-amz-copy-source": "/src/a.txt"})
        j = Journal()
        j.rest("PUT", "/src/a.txt")
        j.add(None, b"", b"", method="PUT", path="/dst/b.txt", headers=copy)
        j.rest("DELETE", "/src/a.txt", status=204)
        self.assertEqual(j.run()[0], [1, 2, 3])
        self.assertEqual(len(j.shared), 1)
        j = Journal()  # the deleted object was itself a copy
        j.rest("PUT", "/src/a.txt")
        j.add(None, b"", b"", method="PUT", path="/dst/b.txt", headers=copy)
        j.rest("DELETE", "/dst/b.txt", status=204)
        self.assertEqual(j.run()[0], [1, 2, 3])

    def test_delete_without_earlier_put_is_kept(self):
        j = Journal()
        j.rest("DELETE", "/bkt/a.txt", status=204)
        self.assertEqual(j.run()[0], [1])


if __name__ == "__main__":
    unittest.main()
