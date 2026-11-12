# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
