# Agent guide

keepstack is a dependency-free Python CLI (`src/keepstack/cli.py`) that runs AWS Emulator in Docker behind a proxy.
The proxy journals successful writes to SQLite and replays them on start, so Terraform state survives restarts.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout and [docs/how-it-works.md](docs/how-it-works.md) for the design.

## Rules
- Standard library only. No runtime dependencies.
- User-facing text, code and comments in English. Translations live in `docs/i18n/`; the English README is the source
  of truth — update the translations when it changes.
- Every behaviour change needs a test; replay/translation bugs need a unit test that reproduces them.

## Verify
```sh
scripts/verify            # Linux/macOS
scripts/verify.ps1        # Windows (PowerShell)
```
Runs the unit tests and the real Docker + Terraform end-to-end suite (~1 min, ports 9340/9341,
`KEEPSTACK_E2E_PORT` to change them). Point `TERRAFORM` at a specific binary if it is not on `PATH`.
