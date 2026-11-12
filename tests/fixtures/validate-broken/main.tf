# Deliberately broken: used by .github/workflows/action.yml to check that the validator fails a pull request.
# DynamoDB rejects attributes that no key or index uses.
resource "aws_dynamodb_table" "broken" {
  name         = "validate-broken"
  billing_mode = "PROVISIONED"
  hash_key     = "id"
  read_capacity  = 1
  write_capacity = 1

  attribute {
    name = "id"
    type = "S"
  }
  attribute {
    name = "unused"
    type = "S"
  }
}
