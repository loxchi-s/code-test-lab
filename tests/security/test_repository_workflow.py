from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/python-repository-sandbox.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _docker_run_tokens(script: str) -> list[str]:
    """Extract only the continued docker run command, excluding surrounding shell text."""
    lines = script.splitlines()
    start = next(
        index for index, line in enumerate(lines)
        if line.lstrip().startswith("docker run --rm")
    )
    command_lines = [lines[start]]
    while command_lines[-1].rstrip().endswith(chr(92)):
        command_lines.append(lines[start + len(command_lines)])
    command = " ".join(line.rstrip().removesuffix(chr(92)) for line in command_lines)
    return shlex.split(command)


def _docker_options(tokens: list[str]) -> tuple[dict[str, list[str]], str, list[str]]:
    assert tokens[:2] == ["docker", "run"]
    image_index = next(
        index for index, token in enumerate(tokens)
        if token.startswith("code-test-lab-python:")
    )
    options = tokens[2:image_index]
    value_options = {
        "--name", "--network", "--mount", "--workdir", "--tmpfs",
        "--memory", "--memory-swap", "--cpus", "--pids-limit",
        "--log-driver", "--log-opt", "--env", "--cap-drop", "--security-opt",
        "--user", "--entrypoint",
    }
    parsed: dict[str, list[str]] = {}
    index = 0
    while index < len(options):
        flag = options[index]
        assert flag in value_options | {"--rm", "--read-only"}, f"unapproved Docker option: {flag}"
        if flag in {"--rm", "--read-only"}:
            parsed.setdefault(flag, []).append("true")
            index += 1
            continue
        assert index + 1 < len(options), f"missing value for {flag}"
        parsed.setdefault(flag, []).append(options[index + 1])
        index += 2
    return parsed, tokens[image_index], tokens[image_index + 1:]


def test_external_python_workflow_is_manual_and_public_only() -> None:
    workflow = _workflow()
    assert set(workflow["on"]) == {"workflow_dispatch"}
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
    assert dispatch["repository"]["required"] == "true"
    assert dispatch["repository"]["type"] == "string"
    assert dispatch["ref"]["default"] == "main"
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["python-sandbox"]
    assert job.get("permissions") in (None, {"contents": "read"})
    validate = next(
        step for step in job["steps"]
        if step.get("name") == "Validate target repository input"
    )
    assert "https://api.github.com/repos/$TARGET_REPOSITORY" in validate["run"]
    assert "jq -r '.visibility // \"unknown\"'" in validate["run"]
    assert '[[ "$visibility" != "public" ]]' in validate["run"]
    assert '[[ -z "${TARGET_REF//[[:space:]]/}" ]]' in validate["run"]


def test_repository_validation_rejects_private_and_blank_refs(
    tmp_path: Path,
) -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    validate = next(
        step for step in steps if step.get("name") == "Validate target repository input"
    )
    base_env = os.environ.copy()
    base_env.update({
        "TARGET_REPOSITORY": "owner/public-repo",
        "TARGET_REF": "main",
    })
    wrapper = (
        'curl() { printf \\'{"visibility":"%s"}\\' "$TEST_VISIBILITY"; }; '
        'jq() { printf \\'%s\\\\n\\' "$TEST_VISIBILITY"; }; '
        'eval "$VALIDATE_SCRIPT"'
    )

    def run_validation(visibility: str, ref: str = "main") -> subprocess.CompletedProcess[str]:
        env = base_env | {
            "TEST_VISIBILITY": visibility,
            "TARGET_REF": ref,
            "VALIDATE_SCRIPT": validate["run"],
        }
        return subprocess.run(
            ["bash", "-e", "-c", wrapper],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
        )

    assert run_validation("public").returncode == 0
    assert run_validation("private").returncode != 0
    assert run_validation("public", "   ").returncode != 0


def test_external_checkout_uses_fixed_workspace_and_no_private_token() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    checkouts = [
        step for step in steps
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkouts) == 2

    harness_checkout, target_checkout = checkouts
    assert harness_checkout["with"]["path"] == "sandbox-harness"
    assert harness_checkout["with"]["persist-credentials"] == "false"
    assert target_checkout["with"]["repository"] == "${{ inputs.repository }}"
    assert target_checkout["with"]["ref"] == "${{ inputs.ref }}"
    assert target_checkout["with"]["path"] == "target-repository"
    assert target_checkout["with"]["persist-credentials"] == "false"
    assert "token" not in target_checkout["with"]
    assert "TARGET_REPO_READ_TOKEN" not in workflow.__repr__()


def test_target_code_runs_only_in_restricted_docker_container() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["python-sandbox"]
    steps = job["steps"]
    build = next(step for step in steps if step.get("id") == "build-image")
    capability_step = next(
        step for step in steps
        if step.get("name") == "Verify Docker daemon PID limit support"
    )
    run_step = next(
        step for step in steps
        if step.get("name") == "Run target pytest suite without network or credentials"
    )

    assert capability_step["timeout-minutes"] == "2"
    assert "docker info --format '{{json .Warnings}}'" in capability_step["run"]
    assert 'test("pids limit"; "i")' in capability_step["run"]
    assert build["working-directory"] == "sandbox-harness"
    assert shlex.split(build["run"]) == [
        "docker", "build", "--pull", "--tag",
        "code-test-lab-python:${GITHUB_SHA}", ".",
    ]

    assert run_step["timeout-minutes"] == "20"
    assert "trap restore_workflow_commands EXIT" in run_step["run"]
    assert "local exit_code=$?" in run_step["run"]
    assert 'exit "$exit_code"' in run_step["run"]
    assert "stop-commands" in run_step["run"]
    assert "printf '\\n::%s::\\n' \"$marker\"" in run_step["run"]
    assert run_step["run"].rstrip().endswith("2>&1")

    tokens = _docker_run_tokens(run_step["run"])
    options, image, command = _docker_options(tokens)
    assert image == "code-test-lab-python:${GITHUB_SHA}"
    assert options["--rm"] == ["true"]
    assert options["--read-only"] == ["true"]
    assert options["--network"] == ["none"]
    assert options["--mount"] == [
        "type=bind,src=${GITHUB_WORKSPACE}/target-repository,dst=/workspace/target,readonly"
    ]
    assert options["--workdir"] == ["/workspace/target"]
    assert options["--tmpfs"] == ["/tmp:rw,noexec,nosuid,size=256m"]
    assert options["--memory"] == ["2g"]
    assert options["--memory-swap"] == ["2g"]
    assert options["--cpus"] == ["2"]
    assert options["--pids-limit"] == ["256"]
    assert options["--log-driver"] == ["json-file"]
    assert options["--log-opt"] == ["max-size=10m", "max-file=3"]
    assert options["--cap-drop"] == ["ALL"]
    assert options["--security-opt"] == ["no-new-privileges"]
    assert options["--user"] == ["10001:10001"]
    assert options["--entrypoint"] == ["python"]
    forwarded_env = options["--env"]
    assert forwarded_env == [
        "HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=", "FTP_PROXY=", "NO_PROXY=",
        "http_proxy=", "https_proxy=", "all_proxy=", "ftp_proxy=", "no_proxy=",
        "PYTHONDONTWRITEBYTECODE=1", "PYTHONPYCACHEPREFIX=/tmp/pycache",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "XDG_CACHE_HOME=/tmp/cache",
    ]

    assert command[:2] == ["-I", "-c"]
    launcher = command[2]
    assert launcher.index("import pytest") < launcher.index('sys.path.insert(0, "/workspace/target")')
    assert "raise SystemExit(pytest.main(" in launcher
    assert '"/workspace/target"' in launcher
    assert "--privileged" not in tokens
    assert not any("docker.sock" in token for token in tokens)
    assert "GITHUB_TOKEN" not in run_step["run"]
    assert "secrets." not in run_step["run"]
    assert job["timeout-minutes"] == "35"

def test_readme_documents_manual_dispatch_and_private_target_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "default branch" in readme
    assert "branch selector" in readme
    assert "Only public target repositories are accepted" in readme
    assert "20-minute timeout" in readme
