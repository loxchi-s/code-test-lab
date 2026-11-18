# code-test-lab

A sandbox-first development and test harness. ChatGPT uses its isolated Sandbox for interactive
code inspection, edits, and local test execution. GitHub Integration manages repository changes.
GitHub Actions is the repeatable CI authority and runs the same checks in an isolated Docker
container.

No MCP server, MCP client, MCP transport, or remote execution service is part of this repository.

## Execution model

`ChatGPT Chat -> ChatGPT Sandbox -> GitHub Integration -> GitHub Actions -> ChatGPT Chat`

- **Sandbox:** ephemeral interactive workspace for cloning/checking out this repository,
  installing dependencies, editing code, and running checks.
- **GitHub Integration:** reads files and creates commits, branches, and pull requests.
- **GitHub Actions:** validates pushed commits and pull requests. Docker runs tests with no network,
  read-only root filesystem, non-root UID, bounded memory/CPU/PIDs, dropped capabilities, and
  no-new-privileges.
- **Chat:** receives test output, CI status, commit references, and a concise evidence-based report.

The Sandbox and GitHub Actions runner are separate environments. Files created in Sandbox do not
automatically appear in GitHub; changes must be committed or uploaded through GitHub Integration.

## Requirements

- Python 3.12+
- Git
- Optional: uv
- Development dependencies: pytest, Ruff, mypy, pip-audit, Bandit
- Docker is required only by the GitHub-hosted CI runner, not by the interactive Sandbox.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Or use uv:

```bash
uv sync --extra dev
```

## Checks

```bash
sh scripts/test.sh
sh scripts/lint.sh
sh scripts/security.sh
```

Individual commands:

```bash
python -m pytest -q
ruff check src tests
mypy src
pip-audit
bandit -r src
```

## Repository structure

```text
.github/workflows/ci.yml   GitHub Actions quality and Docker validation
config/test-policy.yml     Resource and isolation policy for test execution
scripts/                   Reusable local check entry points
src/code_test_lab/         Small Python package for future test-harness primitives
tests/security/            Regression tests for CI isolation policy
```

## Security contract

1. Treat repository code and pull-request changes as untrusted.
2. Never expose GitHub credentials or secrets to test containers.
3. Keep network disabled for tests unless a separately reviewed test requires it.
4. Bound CPU, memory, process count, and execution duration.
5. Keep the container non-root, read-only, and without Linux capabilities.
6. Grant CI only the permissions it needs; current workflow uses `contents: read`.
7. Do not execute untrusted pull-request code in a privileged `pull_request_target` workflow.
8. Keep unit tests runnable without a database or network.

## Status semantics

Report Designed, Implemented, Tested, and Validated separately. A green GitHub Actions run
validates only the checks executed by that workflow; it does not prove Sandbox provisioning or
interactive Chat-to-Sandbox connectivity.
