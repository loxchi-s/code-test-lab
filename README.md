# code-test-lab

A private test/execution backend. ChatGPT-compatible MCP tools dispatch fixed, allowlisted jobs
to GitHub Actions; Actions builds and runs the repository test image in a constrained Docker
container. No arbitrary shell command or user-supplied Docker image is accepted.

## Architecture

`MCP Server -> Application Service -> ExecutionRuntime Port -> GitHub Actions Adapter
-> workflow_dispatch -> Docker/OCI -> execution result`

The domain and application layers do not import MCP, GitHub SDKs, Docker, or databases.
The runtime adapter is the only GitHub API boundary.

## MCP tools

- `container_run(target, ref)`: target is `unit`, `lint`, or `security`.
- `container_status(execution_id, ref)`: polls the workflow run.
- `container_cancel(execution_id, ref)`: requests cancellation.

The adapter correlates the GitHub workflow run using its `display_title` (derived from
`run-name`) and branch. The workflow uploads execution metadata as an artifact. Workflow logs
contain test output.

## Configure the MCP server

Requires Python 3.12+ and a GitHub token with Actions: write/read and Contents: read
permissions for this repository. Keep the token in the MCP host's secret manager; never commit it.

```bash
python -m pip install -e .
export GITHUB_REPOSITORY=loxchi-s/code-test-lab
export GITHUB_TOKEN=...
container-mcp
```

The MCP server uses stdio by default. Configure the MCP client to launch this command with
the environment variables above. A hosted ChatGPT integration additionally requires a
reachable, authenticated MCP transport; a local stdio process is not automatically exposed
to ChatGPT.

## GitHub Actions permissions and dispatch

The token used by the MCP host needs:
- Actions: read and write (dispatch, list runs, cancel)
- Contents: read (workflow checkout)

The workflow itself uses only `contents: read`. Do not use `pull_request_target` to execute
untrusted pull request code with privileged secrets.

The `workflow_dispatch` workflow is present on the default branch (`main`), so the GitHub
default-branch prerequisite for dispatch is satisfied. The MCP host still needs a valid token
and network access to the GitHub REST API. End-to-end dispatch and container execution must be
verified from an authenticated MCP host; CI passing on the repository does not prove that path.

## Security boundary

- Fixed workflow and allowlisted target enum; no arbitrary shell command.
- The workflow validates `execution_id` as a 32-character lowercase hexadecimal identifier
  before using it for metadata or artifact creation.
- Workflow input values are passed through environment variables, not interpolated into shell
  source.
- GitHub Actions runner is ephemeral; no persistent daemon is implied.
- Docker container uses no network, read-only root filesystem, non-root UID, dropped
  capabilities, no-new-privileges, PID/memory/CPU limits.
- No host mounts or Docker socket are exposed to the executed container.
- Workflow timeout is 15 minutes; artifact retention is 7 days.

## Known limitations (initial implementation)

- Run lookup uses `display_title` plus branch. High-volume concurrency should use a durable
  execution ledger and stronger correlation (for example, a GitHub App check-run ID).
- Workflow outputs are available in Actions logs; the uploaded artifact currently contains
  execution metadata only, not arbitrary stdout/stderr capture.
- The MCP server must be hosted on a machine/service reachable by its MCP client. GitHub
  Actions is the execution backend, not a permanently running MCP server.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check src tests
pytest -q
```
