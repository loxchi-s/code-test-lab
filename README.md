# code-test-lab

A private test/execution backend. ChatGPT-compatible MCP tools dispatch fixed, allowlisted
jobs to a trusted GitHub Actions workflow. Actions builds the test runtime from the trusted
default branch and runs the selected source revision as untrusted data inside a restricted
Docker/OCI container.

No arbitrary shell command or user-supplied Docker image is accepted.

## Architecture

`ChatGPT -> MCP Server -> Application Service -> ExecutionRuntime Port
-> GitHub Actions Adapter -> trusted workflow_dispatch -> Docker/OCI -> result artifact
-> MCP container_result`

The domain and application layers do not import MCP, GitHub SDKs, Docker, or databases.
The runtime adapter is the only GitHub API boundary.

## MCP tools

- `container_run(target, ref)`: dispatches `unit`, `lint`, or `security`; `ref` selects
  the source branch/tag to test, not the workflow revision.
- `container_status(execution_id, ref)`: polls the workflow run.
- `container_result(execution_id, ref)`: returns exit code, bounded stdout/stderr, tested
  source ref, and commit SHA after the result artifact is available.
- `container_cancel(execution_id, ref)`: requests cancellation.

The MCP server returns a generated execution ID. Poll status and then retrieve the result;
an artifact can briefly be unavailable after the workflow has completed while GitHub uploads it.

## Trusted workflow and untrusted source boundary

- Dispatch always runs the workflow from `CONTAINER_WORKFLOW_REF` (default: `main`).
- The selected `ref` is passed as `source_ref` and checked out separately as data.
- The Docker image is built from the trusted workflow revision before source checkout.
- Source code is mounted read-only; the container has no network, a read-only root filesystem,
  non-root UID, dropped capabilities, no-new-privileges, and CPU/memory/PID limits.
- The container cannot access the GitHub token or the runner's Docker socket.
- Only fixed test commands are accepted. The workflow does not evaluate user-provided shell.
- Result files are created outside the test container and uploaded as a seven-day artifact.
- The MCP adapter caps each returned log at 16,000 characters. Artifact download is capped at
  8 MiB and each individual log at 1 MiB.

This is a defense-in-depth CI sandbox, not a claim of resistance to Linux kernel or Docker
runtime vulnerabilities. Do not run highly hostile code or secrets in this shared hosted-runner
model without an additional isolation boundary such as a dedicated microVM sandbox.

## Configure the MCP server

Requires Python 3.12+ and a GitHub token with Actions: write/read and Contents: read
permissions for this repository. Keep the token in the MCP host's secret manager; never commit it.

```bash
python -m pip install -e .
export GITHUB_REPOSITORY=loxchi-s/code-test-lab
export GITHUB_TOKEN=...
export CONTAINER_WORKFLOW=container-execute.yml
export CONTAINER_WORKFLOW_REF=main
container-mcp
```

The MCP server uses stdio by default. Configure the MCP client to launch this command with
the environment variables above. A hosted ChatGPT integration additionally requires a
reachable, authenticated MCP transport; a local stdio process is not automatically exposed
to ChatGPT.

## GitHub Actions permissions and dispatch

The token used by the MCP host needs:
- Actions: read and write (dispatch, list runs, cancel, read artifacts)
- Contents: read (workflow and source checkout)

The workflow itself uses only `contents: read`. Do not use `pull_request_target` to execute
untrusted pull request code with privileged secrets.

The `workflow_dispatch` workflow must exist on the default branch. The implementation
dispatches against the trusted `CONTAINER_WORKFLOW_REF` (default `main`), independently of
the source branch under test.

## Current scope and limitations

- The current intake contract tests a Git branch or tag in this repository. Direct upload of
  arbitrary code pasted into a ChatGPT conversation is not implemented yet.
- The fixed targets are unit tests, lint, and security tests. Additional languages and
  dependency profiles need explicit, prebuilt runtime images and allowlisted commands.
- A durable execution ledger and stronger correlation than workflow display title are still
  future work for high concurrency.
- The MCP server must be hosted on a machine/service reachable by its MCP client. GitHub Actions
  is the execution backend, not a permanently running MCP server.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check src tests
pytest -q
```
