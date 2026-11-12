<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/keepstack-logo-white.svg">
    <img src="../assets/keepstack-logo.svg" width="380" alt="keepstack">
  </picture>
</p>

<p align="center">
  <strong>为 AWS Emulator 提供持久化状态。</strong><br>
  本地 AWS 基础设施在重启后依然保留——<code>terraform plan</code> 始终显示 <em>No changes</em>。
</p>

<p align="center">
  <a href="https://github.com/sanlega/keepstack/actions/workflows/ci.yml"><img src="https://github.com/sanlega/keepstack/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="../../LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
</p>

<p align="center">
  <a href="../../README.md">English</a> · <a href="README.es.md">Español</a> · <strong>简体中文</strong>
</p>

<p align="center"><em>如与本译文存在出入，以英文 README 为准。</em></p>

---

## 为什么需要它

[AWS Emulator](https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html) 是 AWS 官方提供的本地 AWS API
模拟器。它速度快、还原度高，但所有数据都只保存在**内存**中：一旦停止它、重启 Docker 或重启电脑，所有 VPC、存储桶和表都会消失。
而 Terraform 状态里仍然记录着这些资源，于是下一次 `terraform plan` 会想把整套基础设施重新创建一遍，而不是只显示你刚做的那一处改动。

**keepstack** 解决了这个问题。它位于模拟器前面，把每一次成功的写操作记录到一个小型 SQLite journal 中，并在模拟器每次启动时重放。
真实的基础设施开发流程——放置几天、回来继续、再加一个服务——现在和在 AWS 上的体验一致。

```console
$ terraform plan            # after a full reboot
No changes. Your infrastructure matches the configuration.

$ terraform plan            # after adding one subnet
Plan: 1 to add, 0 to change, 0 to destroy.
```

## 特性

- **任何情况都不丢状态**——Ctrl+C、`docker kill`、Docker 重启、整机重启均可。如果容器在运行中挂掉，
  keepstack 会自动将其拉起并恢复状态。
- **ID 保持稳定**——服务端随机生成的值（`vpc-…`、KMS 密钥 UUID、Secrets Manager ARN 后缀、Route 53
  托管区域 ID、SQS receipt handle 等）每次重放都会不同。keepstack 会学习这些映射并双向转换，
  因此你的工具看到的始终是原始值。
- **零配置**——一条命令即可。首次运行时会自动构建模拟器镜像（x86_64 或 Apple silicon）。
- **设计上就很安全**——写操作会在到达模拟器*之前*先写入 journal；每个端口只允许一个实例；
  再次执行 `keepstack up --reset` 也绝不会清掉正在运行的实例的状态。
- **无依赖**——仅使用 Python 标准库，支持 macOS、Linux 和 Windows。
- **经过真实 Terraform 测试**——覆盖网络、Serverless 和数据服务等 40 多种资源类型。
  详见[兼容性矩阵](../compatibility.md)。

## 安装

环境要求：[Docker](https://docs.docker.com/get-docker/) 和 Python 3.10+。

```sh
pipx install git+https://github.com/sanlega/keepstack
# or: uv tool install git+https://github.com/sanlega/keepstack
```

## 快速开始

```sh
keepstack up                 # 终端 1 — 恢复状态，并在 http://127.0.0.1:9297 提供 AWS API
```

```sh
eval "$(keepstack env)"      # 终端 2 — 让 AWS CLI、各类 SDK 和 Terraform 指向 keepstack
                             # PowerShell: keepstack env --shell powershell | Invoke-Expression
aws sts get-caller-identity  # "Account": "000000000000" — 说明你连上的是模拟器
```

用自带的示例完整体验一遍：

```sh
cd examples/quickstart
terraform init && terraform apply -auto-approve
# 停止 keepstack（Ctrl+C）或重启电脑，然后重新启动：keepstack up
terraform plan                       # No changes
terraform plan -var extra=true       # Plan: 1 to add — 只有新增的改动
```

状态保存在你运行 `keepstack up` 的目录下的 `.keepstack/` 中，和 `.terraform/` 类似：每个项目一份状态。
使用 `keepstack up --reset` 可以从头开始。

> [!IMPORTANT]
> 请使用 **AWS CLI v2**。AWS CLI v1 对部分服务（例如 SQS）会忽略 `AWS_ENDPOINT_URL`，这些请求会被发往真实的 AWS。
> 在 Terraform 中，请使用 [`examples/quickstart/main.tf`](../../examples/quickstart/main.tf) 里的 `provider` 配置块
> （其中设置了 `s3_use_path_style = true`）。

## 工作原理

```
Terraform / AWS CLI / SDKs ──http──► :9297 keepstack ──https──► :9298 AWS Emulator (Docker)
                                          │
                                          └── .keepstack/journal.db   (SQLite)
```

1. **记录**——每个写调用（`Create*`、`Put*`、S3 `PUT` 等）在转发之前先写入 journal；读请求直接透传。
2. **重放**——启动时，keepstack 会拉起一个干净的模拟器，并按顺序重放 journal；对于领先于模拟器状态的调用
   （例如表仍处于 `CREATING` 时的 `PutItem`）会自动重试。
3. **转换**——它会把每次重放的响应与当初记录的响应做对比，找出哪些生成值发生了变化，
   并在两个方向上改写这些值。

完整设计请阅读 [docs/how-it-works.md](../how-it-works.md)。

## 用法

```text
keepstack up   [--port 9297] [--emulator-port 9298] [--data-dir .keepstack] [--image keepstack-emulator]
               [--reset] [--verbose]
keepstack env  [--port 9297] [--shell sh|powershell]
keepstack --version
```

| 选项 | 默认值 | 说明 |
|---|---|---|
| `--port` | `9297` | 你的工具连接的端口（127.0.0.1 上的明文 HTTP）。 |
| `--emulator-port` | `9298` | 模拟器容器的内部端口。 |
| `--data-dir` | `.keepstack` | journal 的存放位置。 |
| `--image` | `keepstack-emulator` | 模拟器的 Docker 镜像。如果使用默认值且本地不存在，会自动构建。 |
| `--reset` | — | 清除已保存的状态，从头开始。 |
| `--verbose` | — | 记录每一个请求。 |

只要为每个环境指定各自的 `--port`、`--emulator-port` 和 `--data-dir`，就可以同时运行多个相互隔离的环境。

## 局限性

- **仅限本地开发。** keepstack 不校验凭证，并以 `--auth off` 运行模拟器，且只监听 `127.0.0.1`。
  参见 [SECURITY.md](../../SECURITY.md)。
- **它不是 AWS。** 在本地通过并不保证在 AWS 上也能正常工作：模拟器不会评估 IAM 策略，
  各服务的还原度也参差不齐。已知的模拟器缺陷列在[兼容性矩阵](../compatibility.md)中。
- **ID 转换基于启发式规则。** 只有看起来像生成标识符的值才会被学习；
  具体规则和边界情况请参阅[工作原理](../how-it-works.md#limitations)。
- **journal 只增不减。** 如需压缩，请执行 `keepstack up --reset`，然后重新运行 `terraform apply`。

## 参与贡献

欢迎提交 issue 和 pull request，尤其欢迎为[兼容性矩阵](../compatibility.md)补充新条目。
开发环境搭建和测试套件请参阅 [CONTRIBUTING.md](../../CONTRIBUTING.md)。

## 许可证

[Apache-2.0](../../LICENSE)。

keepstack 是一个独立项目，与 Amazon Web Services 没有隶属关系，也未获得其认可或赞助。
AWS 和 AWS Emulator 是 Amazon.com, Inc. 或其关联公司的商标。
