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
CI also runs a restricted-container smoke test against a generated fixture to verify the
base harness image, read-only target mounts, and non-root execution. The manual external-repository
workflow prepares a separate target virtual environment before executing the selected repository.

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
A standard-library parser filters the SBOM to PyPI package components; the separately installed,
checksum-pinned Grype scanner evaluates that Python dependency inventory against its vulnerability
database with a low-severity failure threshold. A second Grype scan evaluates the complete image
SBOM, including operating-system packages, and blocks High or Critical findings. Medium and Low
image findings remain visible in the scanner report. Neither scan installs packages or executes
code from the PR-built image. Scanner independence does not replace trusted workflow
governance: the PR still contains its own workflow definitions.
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
4. It builds the trusted harness image from the `sandbox-harness` directory only. It creates a size-bounded 2 GiB host tmpfs mount and copies the target checkout into that workspace from a restricted container; the original checkout is mounted read-only.
5. It prepares the target environment inside a separate network-enabled preparation container with no GitHub credentials, no secrets, no Docker socket, a read-only root filesystem, non-root UID, dropped capabilities, no-new-privileges, and bounded CPU, memory, PIDs, and a 15-minute dependency-preparation timeout and a 2 GiB tmpfs workspace limit. The preparation adapter copies source without Git internals and parses bounded PEP 621 metadata without executing target code. It writes static project dependencies and the `dev` extra to a requirements file, installs those dependencies as wheels, and records the version of the target distribution if dependency resolution installed it transitively. For dynamic versions, it appends the target commit SHA to that resolver-compatible seed version; when no seed exists, it uses a deterministic `0.0.dev0` fallback. The adapter uses distribution-scoped pretend-version variables for setuptools-scm and the unscoped `SETUPTOOLS_SCM_PRETEND_VERSION` variable for hatch-vcs, which invokes setuptools-scm without a distribution name. Distribution names in build requirements are normalized according to PEP 503. The dependency bootstrap runs `pip install --requirement /tmp/target-requirements.txt` before querying the resolver-installed seed version. If that package was installed transitively, the workflow removes that wheel and installs the target source with `pip install --no-deps --editable .` using the seed version plus a commit-scoped local label. This preserves the version constraints that pip already resolved for dev dependencies while ensuring imports come from the target checkout. This avoids fetching unbounded Git history and avoids pip rejecting a low synthetic version against self-referential dev-extra constraints. Other VCS-based version providers and dynamic PEP 621 dependencies are not currently supported. The target must declare static `project.dependencies`, a static `project.optional-dependencies.dev` extra containing pytest (unless the target distribution itself is pytest), and wheel-compatible dependencies. Build backends and package installation scripts are untrusted code and execute only inside this restricted preparation container.
6. After preparation exits, a separate trusted metadata-only container reads the original `pyproject.toml` with the harness adapter to identify the target distribution; it has no network and executes no target code. The test container mounts the prepared workspace read-only for dependencies and mounts the original target checkout separately at `/input/target` as read-only, so build-backend changes to the disposable preparation copy cannot alter the test files. The trusted test runner is copied into a dedicated `/opt/trusted-runner` virtual environment in the image, containing the harness-resolved pytest version and its dependencies. It runs with its own immutable interpreter, imports trusted pytest before adding the prepared virtual environment's site-packages to `sys.path` for ordinary targets, adds the target's root and `src` directories directly, and never processes untrusted `.pth` files. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` prevents automatic loading of target-installed pytest plugins. The test runner never executes `/sandbox/venv/bin/python` for these targets. When the target distribution itself is pytest, the same trusted interpreter is retained but the runner imports pytest from the target source after placing target source and dependency paths ahead of the dedicated runner site-packages; that result is not an independent validation of pytest's own test runner. The container keeps `--network none`, no credentials, no Docker socket, non-root UID, dropped capabilities, no-new-privileges, bounded CPU/memory/PIDs, and a 20-minute timeout. Test output is prevented from being interpreted as GitHub Actions workflow commands. Target code and dependencies remain untrusted and execute within the restricted container; a passing test run means only that the target's own suite exited successfully under this harness, not that its tests are an independent proof of correctness.

Dependency preparation is the only network-enabled execution stage; the test stage has no network. The workflow never runs target installation commands on the GitHub Actions host and never passes GitHub credentials or repository secrets into either container. Repositories without a compatible `pyproject.toml` dev extra, or dependencies available only as source distributions, are outside this workflow's current contract.

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
