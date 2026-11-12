# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/sanlega/keepstack/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sanlega/keepstack/releases/tag/v0.1.0
