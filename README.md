<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/keepstack-logo-white.svg">
    <img src="docs/assets/keepstack-logo.svg" width="360" alt="keepstack">
  </picture>
</p>

<h3 align="center">Your local AWS survives restarts.</h3>

<p align="center">
  Persistent state for <a href="https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html">AWS Emulator</a>.
  Reboot, come back days later, and <code>terraform plan</code> still says <em>No changes</em>.
</p>

<p align="center">
  <a href="https://github.com/sanlega/keepstack/actions/workflows/ci.yml"><img src="https://github.com/sanlega/keepstack/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen.svg" alt="Zero dependencies">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/how-it-works.md">How it works</a> ·
  <a href="docs/compatibility.md">Compatibility</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
  <br>
  <sub><strong>English</strong> · <a href="docs/i18n/README.es.md">Español</a> · <a href="docs/i18n/README.zh-CN.md">简体中文</a></sub>
</p>

<br>

## The problem

AWS Emulator is AWS's official local emulator: fast and faithful, but **everything lives in memory**.

<table>
<tr>
<th width="50%">😩 Without keepstack</th>
<th width="50%">😌 With keepstack</th>
</tr>
<tr>
<td>

```console
$ terraform apply      # 30 resources
$ # ...reboot...
$ terraform plan
Plan: 30 to add, 0 to change, 0 to destroy.
```

Everything is gone. Terraform wants to rebuild the
whole stack, and you can't see the one change you made.

</td>
<td>

```console
$ terraform apply      # 30 resources
$ # ...reboot...
$ terraform plan
No changes. Your infrastructure matches
the configuration.
```

Same VPCs, same IDs, same data. Add a subnet and
`plan` shows exactly **1 to add**.

</td>
</tr>
</table>

## Quick start

> **Requirements:** [Docker](https://docs.docker.com/get-docker/) and Python 3.10+.

**1. Install**

```sh
pipx install git+https://github.com/sanlega/keepstack
```

**2. Start it:** the emulator image is built on first run, then your saved state is restored

```sh
keepstack up
```

**3. Point your tools at it:** in another terminal

```sh
eval "$(keepstack env)"        # PowerShell: keepstack env --shell powershell | iex
aws sts get-caller-identity    # "Account": "000000000000" → you're on the emulator
```

That's it. Use Terraform, the AWS CLI or any SDK as usual; stop keepstack whenever you like.
Try the full loop with [`examples/quickstart`](examples/quickstart/main.tf).

> [!IMPORTANT]
> Use **AWS CLI v2**: v1 ignores `AWS_ENDPOINT_URL` for some services (e.g. SQS) and would call real AWS.
> In Terraform, copy the `provider` block from the [quickstart example](examples/quickstart/main.tf).

## Features

| | |
|---|---|
| 🔁 **Survives everything** | Ctrl+C, `docker kill`, Docker restarts, reboots. If the container dies, keepstack restores it on its own. |
| 🪪 **Stable IDs** | `vpc-…`, KMS UUIDs, Secrets Manager ARNs, Route 53 zone IDs… keep their original values across restarts. |
| ⚡ **Zero setup** | One command, no config. The emulator image builds itself (x86_64 and Apple silicon). |
| 🛡️ **Safe by design** | Write-ahead journal, one instance per port: a stray `--reset` can't wipe a running state. |
| 📦 **Zero dependencies** | Pure Python standard library. macOS, Linux and Windows. |
| ✅ **Tested for real** | 40+ Terraform resource types, end-to-end against a real emulator. [See the matrix →](docs/compatibility.md) |

## How it works

```mermaid
flowchart LR
    T["Terraform<br/>AWS CLI · SDKs"] -- "http :9297" --> K["keepstack"]
    K -- "https :9298" --> E["AWS Emulator<br/>(Docker)"]
    K <--> J[("journal.db<br/>SQLite")]
```

1. **Record:** every write is saved to the journal *before* it reaches the emulator. Reads pass straight through.
2. **Replay:** on start, a clean emulator receives the journal in order (with retries for resources still settling).
3. **Translate:** new random IDs are mapped back to the originals, so your tools never notice the restart.

State lives in `.keepstack/` next to where you run it, one state per project, like `.terraform/`.
The full design is in [docs/how-it-works.md](docs/how-it-works.md).

## Reference

<details>
<summary><strong>Commands and options</strong></summary>

<br>

```text
keepstack up    [--port 9297] [--emulator-port 9298] [--data-dir .keepstack]
                [--image keepstack-emulator] [--reset] [--verbose]
keepstack env   [--port 9297] [--shell sh|powershell]
keepstack --version
```

| Option | Default | Description |
|---|---|---|
| `--port` | `9297` | Port your tools connect to (plain HTTP on 127.0.0.1) |
| `--emulator-port` | `9298` | Internal port of the emulator container |
| `--data-dir` | `.keepstack` | Where the journal is stored |
| `--image` | `keepstack-emulator` | Emulator image; built automatically if it's the default and missing |
| `--reset` | | Clear the saved state and start from scratch |
| `--verbose` | | Log every request |

Run several isolated environments side by side with different `--port`, `--emulator-port` and `--data-dir`.

</details>

<details>
<summary><strong>Limitations</strong></summary>

<br>

- **Local development only:** no credential checks, emulator runs with `--auth off`, bound to `127.0.0.1`.
  See [SECURITY.md](SECURITY.md).
- **It is not AWS:** the emulator doesn't evaluate IAM and its fidelity varies by service.
  Known gaps are in the [compatibility matrix](docs/compatibility.md).
- **Heuristic ID translation:** only values that look generated are learned.
  Details in [how it works](docs/how-it-works.md#limitations).
- **The journal only grows:** compact it with `keepstack up --reset` and a fresh `terraform apply`.

</details>

## Contributing

Issues and PRs are welcome, especially new entries for the [compatibility matrix](docs/compatibility.md).
Start with [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE)

<sub>keepstack is an independent project, not affiliated with, endorsed by or sponsored by Amazon Web Services.
AWS and AWS Emulator are trademarks of Amazon.com, Inc. or its affiliates.</sub>
