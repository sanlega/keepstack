# Contributing to keepstack

Thanks for helping! keepstack is small on purpose: one dependency-free Python module, a Dockerfile and a test suite
that runs real Terraform against a real emulator. Please keep changes in that spirit.

## Development setup

Requirements: Python 3.10+, Docker and Terraform 1.5+.

```sh
git clone https://github.com/sanlega/keepstack && cd keepstack
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
keepstack --version
```

## Project layout

| Path | Contents |
|---|---|
| `src/keepstack/cli.py` | The whole tool: proxy, journal, replay, ID translation and the `up` / `env` commands |
| `src/keepstack/Dockerfile` | Emulator image, built automatically on first `keepstack up` |
| `examples/quickstart/` | Terraform example used in the README and by the end-to-end tests |
| `tests/test_unit.py` | Fast tests: request classification and ID translation |
| `tests/test_e2e.py` | Real Docker + Terraform: apply, hard reboot, crash recovery, single-instance guard |
| `tests/compat/` | Terraform configurations behind the [compatibility matrix](docs/compatibility.md) |
| `docs/` | Design notes, compatibility matrix, translations and logo |

## Running the tests

```sh
scripts/verify            # macOS / Linux
scripts/verify.ps1        # Windows
```

This runs the unit tests and the end-to-end suite (about one minute). The end-to-end tests are skipped if Docker or
Terraform are missing. Useful variables:

| Variable | Purpose |
|---|---|
| `TERRAFORM` | Path to a specific Terraform binary (default: `terraform` on `PATH`) |
| `KEEPSTACK_E2E_PORT` | Port for the end-to-end run (default `9340`; the emulator uses the next one) |
| `TF_PLUGIN_CACHE_DIR` | Reuse downloaded providers between runs |

Unit tests only: `python -m unittest tests.test_unit -v`.

## Pull requests

- Keep the standard-library-only rule: no runtime dependencies.
- Add a test for every behaviour change. Bugs in replay or ID translation need a unit test that reproduces them.
- New resource types for the compatibility matrix: add them to a group in `tests/compat/`, run the full cycle
  described in [docs/compatibility.md](docs/compatibility.md) and update the table.
- If something fails **without** keepstack too, it is an emulator issue: report it to
  [aws/aws-emulator](https://github.com/aws/aws-emulator) and document it in the compatibility matrix.
- Update [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.
- CI must be green (lint, unit tests on Linux/macOS/Windows, end-to-end on Linux).
