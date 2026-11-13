"""Unit tests for scripts/chaos.py: telling resources Terraform lost in a power cut from real failures."""
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location(
    "chaos", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "chaos.py"))
chaos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chaos)

# Re-apply after a power cut on Linux (#39): the table was created, Terraform died before saving it.
LOST = """aws_dynamodb_table.items: Creating...

Error: creating AWS DynamoDB Table (items): operation error DynamoDB: CreateTable, https response error StatusCode: \
400, RequestID: 0I74K6Z35KIT7JPLR7549KZUQ2IOL6CMF8DK69TNEGVUR8EJGCU9, ResourceInUseException: Table already exists: items

  with aws_dynamodb_table.items,
  on main.tf line 51, in resource "aws_dynamodb_table" "items":
  51: resource "aws_dynamodb_table" "items" {

"""
REAL = """
Error: creating IAM Role (app): operation error IAM: CreateRole, https response error StatusCode: 400, \
MalformedPolicyDocument: invalid principal

  with aws_iam_role.app,
  on main.tf line 70, in resource "aws_iam_role" "app":
"""


class Orphans(unittest.TestCase):
    def test_already_exists_errors_are_resources_terraform_lost(self):
        self.assertEqual(chaos.orphans(LOST), ["aws_dynamodb_table.items"])

    def test_any_other_error_is_a_real_failure(self):
        self.assertIsNone(chaos.orphans(LOST + REAL))
        self.assertIsNone(chaos.orphans("Apply complete!"))

    def test_planned_addresses(self):
        plan = "  # aws_dynamodb_table.items will be created\n  # aws_dynamodb_table_item.seed will be created\n"
        self.assertEqual(chaos.PLANNED.findall(plan), ["aws_dynamodb_table.items", "aws_dynamodb_table_item.seed"])


if __name__ == "__main__":
    unittest.main()
