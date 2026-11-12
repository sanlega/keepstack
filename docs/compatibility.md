# Compatibility matrix

Terraform `hashicorp/aws ~> 6.0` against **AWS Emulator 1.0.322** through keepstack 0.1.0.

Every row was tested with the full cycle: `terraform apply` → `terraform plan` (no changes) → kill keepstack **and**
the emulator container → `keepstack up` (no `--reset`) → `terraform plan` (no changes) → `terraform destroy`.

| Resources | Apply | Idempotent plan | Survives restart | Verdict |
|---|:-:|:-:|:-:|---|
| `vpc`, `subnet`, `internet_gateway`, `route_table`, `route`, `route_table_association`, `eip`, `nat_gateway`, `network_acl` | ✅ | ✅ | ✅ | OK |
| `iam_role`, `iam_policy`, `iam_role_policy_attachment` | ✅ | ✅ | ✅ | OK |
| `lambda_function` (inline zip), `lambda_permission`, `cloudwatch_log_group` | ✅ | ✅ | ✅ | OK |
| `apigatewayv2_api`, `_integration`, `_route`, `_stage` | ✅ | ✅ | ✅ | OK |
| `sqs_queue`, `lambda_event_source_mapping` | ✅ | ✅ | ✅ | OK (mapping UUID translated) |
| `s3_bucket`, `s3_bucket_versioning`, `s3_bucket_policy` (non-public principal), `s3_object` | ✅ | ✅ | ✅ | OK |
| `dynamodb_table` (provisioned), `dynamodb_table_item` | ✅ | ✅ | ✅ | OK |
| `sns_topic`, `sns_topic_subscription` (SQS) | ✅ | ✅ | ✅ | OK |
| `kms_key`, `kms_alias` | ✅ | ✅ | ✅ | OK (key UUID translated) |
| `ssm_parameter`, `secretsmanager_secret`, `secretsmanager_secret_version`, `ecr_repository` | ✅ | ✅ | ✅ | OK (secret ARN suffix translated) |
| `cloudwatch_event_rule`, `cloudwatch_event_target` | ✅ | ✅ | ✅ | OK |
| `route53_zone`, `route53_record` | ✅ | ✅ | ✅ | OK (zone ID translated) |
| `security_group` (and `vpc_security_group_*_rule` that depend on it) | ❌ | n/a | n/a | **Emulator bug**, see below. ✅ with `keepstack up --shims` |
| `s3_bucket_policy` with `Principal = "*"` | ❌ | n/a | n/a | Emulator enforces `BlockPublicPolicy` (likely the same as AWS defaults) |
| `dynamodb_table` with `PAY_PER_REQUEST` | ✅ | ⚠️ | n/a | **Emulator bug**, see below. ✅ with `keepstack up --shims` |

Not covered yet: actually invoking Lambda functions or running event source mappings, ECS, RDS, ElastiCache,
CloudFront, Step Functions. Contributions welcome.

## Known emulator issues

These reproduce **directly against AWS Emulator, without keepstack**:

- **`aws_security_group` cannot be created.** Terraform revokes the default egress rule with
  `IpProtocol=-1, FromPort=0, ToPort=0`. AWS ignores ports for protocol `-1`; the emulator requires them to match and
  answers `InvalidPermission.NotFound`. Workaround: `keepstack up --shims`.
- **On-demand DynamoDB tables show a perpetual diff.** The emulator returns `-1` for
  `OnDemandThroughput.MaxRead/WriteRequestUnits`, so every plan shows `max_read_request_units = -1 -> null`.
  Workaround: `keepstack up --shims`, or `lifecycle { ignore_changes = [on_demand_throughput] }`.

Both are covered by `keepstack up --shims` (verified end to end: create, idempotent plan, restart, destroy).
`keepstack check` reads this matrix from [`src/keepstack/compat.json`](../src/keepstack/compat.json): update both together.

## Running a group yourself

The configurations live in [`tests/compat/`](../tests/compat):

```sh
keepstack up --reset                   # terminal 1
eval "$(keepstack env)"                # terminal 2
cd tests/compat/network                # network | network-sg | serverless | data
terraform init && terraform apply -auto-approve
terraform plan -detailed-exitcode      # 0 = no changes
# stop keepstack (Ctrl+C), start it again WITHOUT --reset, then repeat the plan
terraform destroy -auto-approve
```

`network-sg` fails on purpose: it reproduces the security group bug. To compare against the emulator without
keepstack, run the container directly:

```sh
docker run -d -p 127.0.0.1:9299:9299 -v "$HOME/.aws/emulator:/home/emulator/.aws/emulator" \
  keepstack-emulator -p 9299 --auth off
export AWS_ENDPOINT_URL=https://127.0.0.1:9299 AWS_CA_BUNDLE=$HOME/.aws/emulator/9299/ca.pem
```

Found something that breaks? Open an
[emulator incompatibility report](https://github.com/sanlega/keepstack/issues/new?template=incompatibility.yml).
