# Security policy

## Scope and threat model

keepstack is a **local development tool**. By design:

- It serves plain HTTP on `127.0.0.1` only and does **not** check credentials: any process on your machine can call it.
- It runs AWS Emulator with `--auth off`, published only on `127.0.0.1`.
- The journal (`.keepstack/journal.db`) stores every write request and response in clear text, including any secret
  values you send (e.g. Secrets Manager secrets, SSM parameters). Treat it like a Terraform state file and do not
  commit it.

Do not expose keepstack to a network, and never point it at real AWS credentials or data.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/sanlega/keepstack/security/advisories/new)
rather than in a public issue. Include steps to reproduce and the keepstack version (`keepstack --version`).

You can expect an acknowledgement within a week. Only the latest release receives security fixes.

Vulnerabilities in AWS Emulator itself should be reported to AWS as described in its
[security documentation](https://docs.aws.amazon.com/emulator/latest/userguide/security.html).
