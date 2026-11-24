# Project usage guide

## Requirements

- Python 3.12+
- Git
- Docker for the local policy-driven container runner
- Optional: uv
- Runtime dependency: PyYAML
- Development dependencies: pytest, Ruff, mypy, pip-audit, Bandit, and types-PyYAML

## Install

Using venv:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Or using uv:

```bash
uv sync --extra dev
. .venv/bin/activate
```

Activate the environment in each new shell before running checks.

## Run checks

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

## Run the isolated Docker test runner

Build the image only from a trusted checkout:

```bash
docker build --tag code-test-lab:local .
python -m code_test_lab.isolation \
  --policy config/test-policy.yml \
  --image code-test-lab:local \
  -- python -m pytest -q
```

For untrusted checkouts, use a disposable builder with no proxy credentials or sensitive host mounts. The runner uses the checked-in `config/test-policy.yml` policy and passes arguments directly without a shell. If forced container cleanup cannot be confirmed after a timeout, it returns exit code `125`; inspect the original Docker daemon before retrying. A confirmed timeout cleanup returns `124`.

## Test another public Python repository

Use **Actions → Python Repository Sandbox → Run workflow**.

Prerequisites:

- The workflow must exist on the default branch before GitHub exposes **Run workflow**.
- The Actions branch selector chooses the workflow definition; its `ref` input independently selects the target repository revision.
- Only public target repositories are accepted.
- The current harness image provides Python 3.12 and pytest. Target-specific dependencies are not installed.

Enter the target as `owner/repository`, select a branch, tag, or commit in `ref`, and dispatch the workflow. It checks out the harness and target separately, builds the harness image from the harness directory, mounts the target read-only, and runs pytest in the restricted container. The target Dockerfile is not executed, and the target container receives no network access, GitHub token, or repository secrets.

The manual workflow is separate from normal pull-request CI. Keep the default branch protected and review workflow changes because workflow definitions are part of the repository's trust boundary.

## Execution and security model

- GitHub Actions is the repeatable CI authority; the interactive ChatGPT Sandbox and GitHub-hosted runner are separate environments.
- Treat repository code and pull-request changes as untrusted.
- CI uses restricted containers, read-only mounts, non-root execution, bounded resources, and no network for test workloads.
- Never expose GitHub credentials or secrets to test containers.
- Unit tests must remain runnable without a database or network.
- A green workflow validates only the checks it executes; it does not prove interactive Sandbox connectivity or every production security property.
