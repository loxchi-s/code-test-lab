# code-test-lab

A private, sandbox-first development and testing repository. ChatGPT uses its isolated
Sandbox environment for interactive code inspection, editing, and local test execution.
GitHub Integration manages repository branches, commits, and pull requests. GitHub Actions
runs repeatable CI checks on pushes and pull requests.

No MCP server, MCP client, or MCP transport is part of this architecture.

## Architecture

`ChatGPT Chat -> ChatGPT Sandbox -> GitHub Integration -> GitHub Actions -> ChatGPT Chat`

Responsibilities:

- **ChatGPT Sandbox:** isolated workspace for checkout/import, dependency installation,
  code changes, unit tests, lint, static analysis, and security checks.
- **GitHub Integration:** repository reads/writes, branch management, commits, and PRs.
- **GitHub Actions:** authoritative repository CI, Docker-based isolated test execution,
  logs, and workflow artifacts.
- **Chat:** task intake, review feedback, and evidence-based status reporting.

The Sandbox is a separate execution environment; it is not the GitHub Actions runner.
Changes must be explicitly committed or uploaded to GitHub through the GitHub integration.

## Development environment

- Python 3.12
- Git
- uv (recommended) or pip
- pytest and pytest-asyncio
- Ruff
- mypy
- pip-audit
- Bandit
- Docker is used by GitHub Actions; it is not required inside the ChatGPT Sandbox.

Install dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run checks:

```bash
ruff check src tests
python -m pytest -q
python -m compileall -q src
mypy src
pip-audit
bandit -r src
```

## GitHub Actions

The `.github/workflows/ci.yml` workflow runs on pull requests and pushes to configured
branches. It performs quality checks and executes tests in a restricted Docker container.

The container uses a non-root UID, read-only root filesystem, no network, bounded memory,
CPU and process count, dropped Linux capabilities, and no-new-privileges.

## Architecture and security rules

1. Keep domain and application logic independent of GitHub, Docker, ChatGPT, and UI frameworks.
2. Put external effects behind explicit ports/adapters when they are part of the application.
3. Keep unit tests runnable without network access or a database.
4. Treat repository code under test as untrusted input.
5. Do not pass GitHub credentials or secrets into test containers.
6. Use a dedicated branch and pull request; do not push directly to `main`.
7. Report Designed, Implemented, Tested, and Validated as separate states. Never claim a check
   passed without a corresponding test or CI result.

## Current limitations

- The ChatGPT Sandbox runtime is provisioned independently from this repository. Sandbox
  availability depends on account compute quota and platform capacity.
- This repository does not create or host a persistent remote execution service.
- GitHub Actions is the repeatable CI environment; ChatGPT Sandbox is the interactive
  development environment.
