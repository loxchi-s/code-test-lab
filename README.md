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
- Runtime dependency: PyYAML
- Development dependencies: pytest, Ruff, mypy, pip-audit, Bandit, types-PyYAML
- Docker is required only by the local policy-driven container runner and GitHub-hosted CI.

## Setup

Using venv:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Using uv:

```bash
uv sync --extra dev
. .venv/bin/activate
```

Activate the environment in each new shell before running the checks.

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
CI uses Syft to inspect the built image and emit a CycloneDX SBOM without running the image.
The separately installed, checksum-pinned Grype scanner evaluates that SBOM against its
vulnerability database with a low-severity failure threshold. It does not install packages or
execute code from the PR-built image. This improves scanner independence but does not replace
trusted workflow governance: the PR still contains its own workflow definitions.
The job uses read-only repository permissions and does not require repository write access or secrets.

## Policy-driven container execution

`config/test-policy.yml` is the source of truth for the local policy runner.
The GitHub Actions workflow uses an explicit, fixed Docker argument vector so it does not install
or execute pull-request-controlled Python code on the host before isolation. CI limits mirror the
checked-in policy and are asserted by regression tests; update both together when changing limits.

Building the local image executes the checkout's Dockerfile through the Docker builder. Do this only for a trusted checkout. Docker may populate proxy build arguments from the developer's Docker client configuration, so an untrusted Dockerfile must never run on a host with proxy credentials. For untrusted checkouts, use a disposable builder with no proxy credentials and no sensitive host mounts.

Build the local image first (Docker must be available):

```bash
docker build --tag code-test-lab:local .
python -m code_test_lab.isolation \
  --policy config/test-policy.yml \
  --image code-test-lab:local \
  -- python -m pytest -q
```

The runner passes arguments directly without a shell and applies the policy's per-command deadline to the Docker client. When that deadline expires, the client is stopped and the runner attempts bounded container cleanup against the same Docker daemon. If forced removal cannot be confirmed, the runner returns exit code `125`; the workload may still be running, so inspect the original Docker daemon before retrying. A confirmed cleanup returns `124` for the timeout. Ordinary unit tests and lint checks do not require Docker. The GitHub Actions job timeout is an outer CI safety bound. The test container receives no GitHub credentials or repository secrets.

## Test another GitHub repository (Python)

Use the manual **Python Repository Sandbox** workflow.

**Prerequisites:** The workflow file must already exist on the repository's default branch before GitHub exposes **Run workflow**. In the Actions page, the branch selector chooses the branch containing the workflow definition; the workflow's `ref` input independently selects the revision of the target repository. Only public target repositories are accepted. Private targets are rejected because the harness repository's Actions log readers may not have permission to view the target's code or test output.

1. Open **Actions → Python Repository Sandbox → Run workflow**.
2. Enter a public target as `owner/repository`; choose a branch, tag, or commit in `ref` (default: `main`).
3. The workflow verifies that the target is publicly accessible before checkout. It checks out this harness into `sandbox-harness` and the selected target into `target-repository`; checkout credentials are not persisted.
4. It builds the trusted harness image from the `sandbox-harness` directory only, mounts only `target-repository` read-only, and runs pytest inside the restricted container. The launcher imports pytest from the trusted image before adding the target directory to Python's import path.
5. Target output is prevented from being interpreted as GitHub Actions workflow commands, and the test step has a 20-minute timeout.

The target container has no network, no GitHub token or repository secrets, no Docker socket, a read-only root filesystem, non-root UID, dropped capabilities, no-new-privileges, and bounded CPU, memory, PIDs, and execution time. The workflow does not execute the target repository's Dockerfile or install its dependencies. Therefore, the first version is intended for Python repositories whose test dependencies are already available in the harness image (currently Python 3.12 and pytest). Repositories requiring additional packages will fail with an import error until a separately reviewed dependency-preparation stage is added. Do not solve that by running `pip install` on the host or by exposing credentials/network to the test container.

The workflow is manually dispatched and is separate from the normal PR CI. It is a first Python adapter; additional language runtimes can be added as separate, pinned sandbox images without changing the repository-checkout contract. As with the existing CI, protect the default branch and govern workflow changes: a workflow modified on its own branch is not an independent trust boundary.

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
2. The Docker policy is defense-in-depth for test processes, not a security boundary against a
   malicious PR author: the workflow, Dockerfile, and policy runner are themselves versioned in the
   PR and can be modified by that PR.
3. For adversarial-contributor threat models, enforce a trusted workflow from a protected base
   branch or a separately governed reusable workflow, require appropriate review/approval, and
   keep secrets unavailable to pull-request jobs.
4. Never expose GitHub credentials or secrets to test containers.
5. Keep network disabled for tests unless a separately reviewed test requires it.
6. Bound CPU, memory, swap, process count, log storage, and execution duration.
7. Keep the container non-root, read-only, and without Linux capabilities.
8. Grant CI only the permissions it needs; current workflow uses `contents: read`.
9. Do not execute untrusted pull-request code in a privileged `pull_request_target` workflow.
10. Keep unit tests runnable without a database or network.

## Status semantics

Report Designed, Implemented, Tested, and Validated separately. A green GitHub Actions run
validates only the checks executed by that workflow; it does not prove Sandbox provisioning or
interactive Chat-to-Sandbox connectivity.
