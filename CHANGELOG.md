# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Docs: CloudFormation and CDK stacks persist across restarts; generated physical names do not (documented limitation,
  [#19](https://github.com/sanlega/keepstack/issues/19)).

### Added
- `keepstack validate [PATH]`: applies a Terraform directory on keepstack (init, plan, apply, plan again) and reports
  config errors, emulator gaps (HTTP 501 or known bugs, warnings unless `--strict`) and non-idempotent resources, with
  file and line. Uses a temporary override file and a local backend inside the data dir, so the user's files and
  backend are untouched and the next run starts from the previous infrastructure. Lists replacements and destroys
  (`--fail-on-destroy`), and writes `--json`, `--report` and `--markdown` reports. Works with OpenTofu (`--terraform`).
- `keepstack validate --run CMD` (repeatable): smoke tests after the apply, with the AWS variables and every Terraform
  output as `TF_OUTPUT_<name>`; a failing command fails the validation.
- Inside GitHub Actions, `keepstack validate` prints every finding and destructive change as an inline annotation on
  the `.tf` line.

- GitHub Action validator mode: `validate: <dir>` runs `keepstack validate`, writes the report to the job summary and
  to a single pull request comment that is updated on every run (`comment`), installs Terraform when missing, and
  exposes `result`, `report` and `baseline-restored` outputs. Also `run`, `strict` and `fail-on-destroy` inputs.
- `baseline: true`: pushes to the default branch save the validated environment (journal and Terraform state); pull
  requests restore it and are validated on top of it, so the report shows the incremental plan.

### Changed
- `keepstack up --reset` also deletes the Terraform state kept by `keepstack validate`.

### Fixed
- Secrets Manager ARNs whose random suffix came out in a single case (`demo-db-password-SGAEFE`) were not translated
  after a restart, so Terraform saw the secret as deleted. With emulator 1.0.337 it happened in 2 of 3 CI runs
  (`SGAEFE`, `ctksym`).

## [0.2.0] - 2026-11-12

### Added
- `keepstack ui`: read-only web dashboard at `http://127.0.0.1:9297/__keepstack/` with status, calls per service,
  existing resources, recent calls (request/response), ID translations and snapshots. Localhost only.
- GitHub Action (`uses: sanlega/keepstack@v0.2.0`): persistent local AWS in any workflow, with optional state cache.
- `keepstack env --shell dotenv` for `.env` files and `$GITHUB_ENV`.
- Release automation: tagging `v*` builds the package, publishes it to PyPI (trusted publishing) and creates the
  GitHub release from this changelog.
- `keepstack check`: pre-flight compatibility report for a Terraform project (scans `.tf` files or a
  `terraform show -json` plan), with workarounds and the emulator's known gaps per service. Exits 1 if anything will fail.
- `keepstack up -d`, `down`, `status` and `logs`: run in the background and inspect the state (calls per service,
  last restore).
- `keepstack doctor`: checks Docker, the port, the AWS CLI version and real-credential leaks.
- `keepstack snapshot save/list/restore/delete` (with `--include` for files such as `terraform.tfstate`),
  `keepstack export` and `keepstack import`.
- `keepstack compact`: drops created-then-deleted resources from the journal, verified in a throwaway emulator.
- `keepstack up --shims`: opt-in workarounds for `aws_security_group` and on-demand DynamoDB tables.

### Changed
- `--reset` also forgets per-state settings (such as remembered shims).

### Fixed
- SQLite connections are now closed after use; they kept `journal.db` locked on Windows.

## [0.1.0] - 2026-11-12

### Added
- `keepstack up`: runs AWS Emulator in Docker behind a persistence proxy and restores the saved state on start.
- `keepstack env`: prints the environment variables for the AWS CLI, SDKs and Terraform (`sh` and PowerShell).
- Write-ahead SQLite journal: writes are recorded before they reach the emulator.
- Replay with retries for calls that run ahead of the emulator (e.g. `PutItem` on a `CREATING` table).
- Generic translation of server-generated values (EC2 IDs, UUIDs, ARN suffixes, Route 53 zone IDs, receipt handles,
  base64 blobs), with collision-safe aliases.
- Automatic recovery when the emulator container dies.
- Single instance per port; `--reset` never wipes a running instance's journal.
- Automatic build of the emulator image (x86_64 and arm64).
- Compatibility matrix covering 40+ Terraform resource types.

[Unreleased]: https://github.com/sanlega/keepstack/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sanlega/keepstack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sanlega/keepstack/releases/tag/v0.1.0
