terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
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

data "archive_file" "fn" {
  type        = "zip"
  output_path = "${path.module}/fn.zip"
  source {
    filename = "index.py"
    content  = "def handler(event, context):\n    return {'statusCode': 200, 'body': 'ok'}\n"
  }
}

resource "aws_iam_role" "fn" {
  name = "compat-fn"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_policy" "logs" {
  name = "compat-logs"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents", "sqs:*"], Resource = "*" }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.fn.name
  policy_arn = aws_iam_policy.logs.arn
}

resource "aws_cloudwatch_log_group" "fn" {
  name              = "/aws/lambda/compat-fn"
  retention_in_days = 7
}

resource "aws_lambda_function" "fn" {
  function_name    = "compat-fn"
  role             = aws_iam_role.fn.arn
  runtime          = "python3.12"
  handler          = "index.handler"
  filename         = data.archive_file.fn.output_path
  source_code_hash = data.archive_file.fn.output_base64sha256
  depends_on       = [aws_cloudwatch_log_group.fn]
}

resource "aws_apigatewayv2_api" "http" {
  name          = "compat-http"
  protocol_type = "HTTP"
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

resource "aws_apigatewayv2_integration" "fn" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "hello" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /hello"
  target    = "integrations/${aws_apigatewayv2_integration.fn.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_sqs_queue" "jobs" {
  name = "compat-jobs"
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.fn.arn
  batch_size       = 5
}
