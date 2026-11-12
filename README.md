<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/keepstack-logo-white.svg">
    <img src="docs/assets/keepstack-logo.svg" width="380" alt="keepstack">
  </picture>
</p>

<p align="center">
  <strong>Persistent state for AWS Emulator.</strong><br>
  Your local AWS infrastructure survives restarts — <code>terraform plan</code> keeps saying <em>No changes</em>.
</p>

<p align="center">
  <a href="https://github.com/sanlega/keepstack/actions/workflows/ci.yml"><img src="https://github.com/sanlega/keepstack/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
</p>

<p align="center">
  <strong>English</strong> · <a href="docs/i18n/README.es.md">Español</a> · <a href="docs/i18n/README.zh-CN.md">简体中文</a>
</p>

---

## Why

[AWS Emulator](https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html) is AWS's official local
emulator of the AWS APIs. It is fast and faithful, but it keeps everything **in memory**: stop it, restart Docker or
reboot your laptop, and every VPC, bucket and table is gone. Your Terraform state still lists them, so the next
`terraform plan` wants to recreate your whole infrastructure instead of showing the one change you made.

**keepstack** fixes that. It sits in front of the emulator, records every successful write to a small SQLite
journal and replays it whenever the emulator starts. Real infrastructure work — leave it for days, come back,
add one service — now behaves like it does on AWS.

```console
$ terraform plan            # after a full reboot
No changes. Your infrastructure matches the configuration.

$ terraform plan            # after adding one subnet
Plan: 1 to add, 0 to change, 0 to destroy.
```

## Features

- **Survives everything** — Ctrl+C, `docker kill`, Docker restarts, reboots. If the container dies while running,
  keepstack brings it back and restores the state on its own.
- **Stable IDs** — random server-generated values (`vpc-…`, KMS key UUIDs, Secrets Manager ARN suffixes, Route 53
  zone IDs, SQS receipt handles…) differ on every replay. keepstack learns the mapping and translates it both ways, so
  your tools always see the original values.
- **Zero setup** — one command. The emulator image is built automatically on first run (x86_64 or Apple silicon).
- **Safe by design** — writes are journaled *before* they reach the emulator, one instance per port, and a second
  `keepstack up --reset` can never wipe a running instance's state.
- **No dependencies** — pure Python standard library. Works on macOS, Linux and Windows.
- **Tested against real Terraform** — 40+ resource types across networking, serverless and data services.
  See the [compatibility matrix](docs/compatibility.md).

## Install

Requirements: [Docker](https://docs.docker.com/get-docker/) and Python 3.9+.

```sh
pipx install git+https://github.com/sanlega/keepstack
# or: uv tool install git+https://github.com/sanlega/keepstack
```

## Quick start

```sh
keepstack up                 # terminal 1 — restores state, serves the AWS API on http://127.0.0.1:9297
```

```sh
eval "$(keepstack env)"      # terminal 2 — point AWS CLI, SDKs and Terraform at keepstack
                             # PowerShell: keepstack env --shell powershell | Invoke-Expression
aws sts get-caller-identity  # "Account": "000000000000" — you are talking to the emulator
```

Try the full loop with the bundled example:

```sh
cd examples/quickstart
terraform init && terraform apply -auto-approve
# stop keepstack (Ctrl+C) — or reboot — then start it again: keepstack up
terraform plan                       # No changes
terraform plan -var extra=true       # Plan: 1 to add — only the new change
```

State lives in `.keepstack/` in the directory where you run `keepstack up`, just like `.terraform/`: one state per
project. Use `keepstack up --reset` to start from scratch.

> [!IMPORTANT]
> Use **AWS CLI v2**. AWS CLI v1 ignores `AWS_ENDPOINT_URL` for some services (e.g. SQS) and would send those calls
> to real AWS. In Terraform, use the `provider` block from [`examples/quickstart/main.tf`](examples/quickstart/main.tf)
> (it sets `s3_use_path_style = true`).

## How it works

```
Terraform / AWS CLI / SDKs ──http──► :9297 keepstack ──https──► :9298 AWS Emulator (Docker)
                                          │
                                          └── .keepstack/journal.db   (SQLite)
```

1. **Record** — every write call (`Create*`, `Put*`, S3 `PUT`…) is journaled before it is forwarded; reads pass
   straight through.
2. **Replay** — on start, keepstack launches a clean emulator and replays the journal in order, retrying calls that
   run ahead of the emulator (e.g. `PutItem` while the table is still `CREATING`).
3. **Translate** — it diffs each replayed response against the recorded one, learns which generated values changed
   and rewrites them in both directions.

Read the full design in [docs/how-it-works.md](docs/how-it-works.md).

## Usage

```text
keepstack up   [--port 9297] [--emulator-port 9298] [--data-dir .keepstack] [--image keepstack-emulator]
               [--reset] [--verbose]
keepstack env  [--port 9297] [--shell sh|powershell]
keepstack --version
```

| Option | Default | Description |
|---|---|---|
| `--port` | `9297` | Port your tools connect to (plain HTTP on 127.0.0.1). |
| `--emulator-port` | `9298` | Internal port of the emulator container. |
| `--data-dir` | `.keepstack` | Where the journal is stored. |
| `--image` | `keepstack-emulator` | Emulator Docker image. Built automatically if it is the default and missing. |
| `--reset` | — | Clear the saved state and start from scratch. |
| `--verbose` | — | Log every request. |

Run several isolated environments side by side by giving each one its own `--port`, `--emulator-port` and `--data-dir`.

## Limitations

- **Local development only.** keepstack does not check credentials and runs the emulator with `--auth off`. It only
  listens on `127.0.0.1`. See [SECURITY.md](SECURITY.md).
- **It is not AWS.** Passing locally does not guarantee it works on AWS: the emulator does not evaluate IAM policies
  and its fidelity varies by service. Known emulator gaps are listed in the [compatibility matrix](docs/compatibility.md).
- **Heuristic ID translation.** Values are learned only if they look like generated identifiers; see
  [how it works](docs/how-it-works.md#limitations) for the exact rules and edge cases.
- **The journal only grows.** To compact it, run `keepstack up --reset` and `terraform apply` again.

## Contributing

Issues and pull requests are welcome — especially new entries for the [compatibility matrix](docs/compatibility.md).
See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and the test suite.

## License

[Apache-2.0](LICENSE).

keepstack is an independent project. It is not affiliated with, endorsed by or sponsored by Amazon Web Services.
AWS and AWS Emulator are trademarks of Amazon.com, Inc. or its affiliates.
