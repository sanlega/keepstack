terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

# Points at keepstack (`keepstack env`). Credentials do not matter: the emulator runs with --auth off.
provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  # Global endpoint: AWS_ENDPOINT_URL=http://127.0.0.1:9297 (see README)
}

variable "extra" {
  description = "Set to true to add a new resource and see that the plan shows only that change"
  type        = bool
  default     = false
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  tags       = { Name = "demo" }
}

resource "aws_subnet" "a" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
}

# aws_security_group fails on emulator 1.0.322: Terraform revokes the default egress rule
# with IpProtocol=-1 and ports 0-0, and the emulator answers InvalidPermission.NotFound
# (AWS ignores ports with protocol -1). An emulator bug, not a proxy one.

resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.main.id
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs"
}

resource "aws_s3_bucket" "data" {
  bucket = "demo-data-bucket"
}

resource "aws_dynamodb_table" "items" {
  name         = "items"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }
  # The emulator returns max_*_request_units = -1 and Terraform sees a change that does not exist.
  lifecycle { ignore_changes = [on_demand_throughput] }
}

# On replay, PutItem arrives right after CreateTable while the table is still CREATING: the proxy retries.
resource "aws_dynamodb_table_item" "seed" {
  table_name = aws_dynamodb_table.items.name
  hash_key   = aws_dynamodb_table.items.hash_key
  item       = jsonencode({ id = { S = "seed-1" }, note = { S = "data that must survive" } })
}

resource "aws_iam_role" "app" {
  name = "app"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

# Random IDs that are not vpc-style: UUID (KMS), ARN suffix (Secrets Manager), Z... (Route53).
resource "aws_kms_key" "app" {
  description = "demo"
}

resource "aws_kms_alias" "app" {
  name          = "alias/demo"
  target_key_id = aws_kms_key.app.key_id
}

resource "aws_secretsmanager_secret" "db" {
  name = "demo-db-password"
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = "s3cr3t"
}

resource "aws_route53_zone" "internal" {
  name = "demo.internal"
}

resource "aws_subnet" "b" {
  count      = var.extra ? 1 : 0
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.2.0/24"
}

output "vpc_id" { value = aws_vpc.main.id }
output "subnet_id" { value = aws_subnet.a.id }
