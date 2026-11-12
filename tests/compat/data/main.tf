terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

# Points at the proxy (AWS_ENDPOINT_URL=http://127.0.0.1:PORT) or directly at the emulator. Credentials do not matter.
provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

resource "aws_s3_bucket" "data" {
  bucket = "compat-data-bucket"
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      # The emulator rejects Principal "*" (BlockPublicPolicy, same as AWS with the default config).
      Principal = { AWS = "arn:aws:iam::000000000000:root" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.data.arn}/*"
    }]
  })
}

resource "aws_s3_object" "hello" {
  bucket       = aws_s3_bucket.data.id
  key          = "hello.txt"
  content      = "hello"
  content_type = "text/plain"
  # Without this the object can be created before versioning is enabled (version "null") and destroy leaves the bucket non-empty.
  depends_on = [aws_s3_bucket_versioning.data]
}

resource "aws_dynamodb_table" "items" {
  name           = "compat-items"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "id"
  attribute {
    name = "id"
    type = "S"
  }
}

resource "aws_sns_topic" "events" {
  name = "compat-events"
}

resource "aws_sqs_queue" "sub" {
  name = "compat-sub"
}

resource "aws_sns_topic_subscription" "sqs" {
  topic_arn = aws_sns_topic.events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.sub.arn
}

resource "aws_kms_key" "k" {
  description = "compat"
}

resource "aws_kms_alias" "k" {
  name          = "alias/compat"
  target_key_id = aws_kms_key.k.key_id
}

resource "aws_ssm_parameter" "p" {
  name  = "/compat/param"
  type  = "String"
  value = "v1"
}

resource "aws_secretsmanager_secret" "s" {
  name                    = "compat-secret"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "s" {
  secret_id     = aws_secretsmanager_secret.s.id
  secret_string = "shh"
}

resource "aws_ecr_repository" "r" {
  name = "compat/repo"
}

resource "aws_cloudwatch_event_rule" "r" {
  name                = "compat-rule"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "t" {
  rule = aws_cloudwatch_event_rule.r.name
  arn  = aws_sqs_queue.sub.arn
}

resource "aws_route53_zone" "z" {
  name = "compat.example.com"
}

resource "aws_route53_record" "a" {
  zone_id = aws_route53_zone.z.zone_id
  name    = "www.compat.example.com"
  type    = "A"
  ttl     = 60
  records = ["10.1.2.3"]
}
