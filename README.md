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
- Development dependencies: pytest, Ruff, mypy, pip-audit, Bandit, PyYAML, types-PyYAML
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

## GitHub Actions workflow security

The separate `.github/workflows/workflow-security.yml` workflow performs:

- `actionlint`: validates workflow syntax, expressions, and action inputs.
- `Gitleaks`: scans the full checked-out Git history for accidentally committed secrets.
- `zizmor`: audits GitHub Actions security configuration and gates high-severity findings.

Tool versions are pinned. Downloaded release archives are verified against published SHA-256 checksums.
The job uses read-only repository permissions and does not require repository write access or secrets.

## Policy-driven container execution

`config/test-policy.yml` is the single source of truth for the container isolation settings.
The policy runner validates it and constructs the Docker argument vector; the workflow does not
duplicate Docker security flags.

```bash
python -m code_test_lab.isolation \
  --policy config/test-policy.yml \
  --image code-test-lab:local \
  -- python -m pytest -q
```

The runner passes arguments directly without a shell and enforces the policy's per-command timeout.
The GitHub Actions job timeout is an outer CI safety bound. The test container receives no GitHub
credentials or repository secrets.

## Repository structure

```text
.github/workflows/ci.yml   GitHub Actions quality and Docker validation
config/test-policy.yml     Executable resource and isolation policy
scripts/                   Reusable local check entry points
src/code_test_lab/         Policy validation and isolated Docker runner
tests/security/            Regression tests for policy enforcement
tests/architecture/        Static dependency-boundary and cycle checks
```

## Security contract

1. Treat repository code and pull-request changes as untrusted.
2. The Docker policy is defense-in-depth for test processes, not a security boundary against a malicious PR author: the workflow and policy runner are themselves versioned in the PR and can be modified by that PR.
3. For adversarial-contributor threat models, enforce a trusted workflow from a protected base branch or a separately governed reusable workflow, require appropriate review/approval, and keep secrets unavailable to pull-request jobs.
4. Never expose GitHub credentials or secrets to test containers.
5. Keep network disabled for tests unless a separately reviewed test requires it.
6. Bound CPU, memory, swap, process count, and execution duration.
7. Keep the container non-root, read-only, and without Linux capabilities.
8. Grant CI only the permissions it needs; current workflow uses `contents: read`.
9. Do not execute untrusted pull-request code in a privileged `pull_request_target` workflow.
10. Keep unit tests runnable without a database or network.

## Status semantics

Report Designed, Implemented, Tested, and Validated separately. A green GitHub Actions run
validates only the checks executed by that workflow; it does not prove Sandbox provisioning or
interactive Chat-to-Sandbox connectivity.
