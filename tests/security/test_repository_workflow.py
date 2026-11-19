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
    assert "TARGET_REPOSITORY=" in validate["run"]
    assert "TARGET_REF=" in validate["run"]
    assert '>> "$GITHUB_OUTPUT"' in validate["run"]
    target_checkout = next(
        step for step in job["steps"]
        if step.get("name") == "Checkout target repository into isolated workspace"
    )
    assert target_checkout["with"]["repository"] == "${{ steps.validate-target.outputs.repository }}"
    assert target_checkout["with"]["ref"] == "${{ steps.validate-target.outputs.ref }}"


def test_repository_validation_rejects_private_and_blank_refs(tmp_path: Path) -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    validate = next(
        step for step in steps if step.get("name") == "Validate target repository input"
    )
    base_env = os.environ.copy()
    output_file = tmp_path / "github-output"
    base_env.update({
        "TARGET_REPOSITORY": "owner/public-repo",
        "TARGET_REF": "main",
        "GITHUB_OUTPUT": str(output_file),
    })
    wrapper = r'''curl() { printf '{"visibility":"%s"}' "$TEST_VISIBILITY"; }; jq() { printf '%s\\n' "$TEST_VISIBILITY"; }; eval "$VALIDATE_SCRIPT"'''

    def run_validation(
        visibility: str,
        ref: str = "main",
        repository: str = "owner/public-repo",
    ) -> subprocess.CompletedProcess[str]:
        output_file.write_text("", encoding="utf-8")
        env = base_env | {
            "TEST_VISIBILITY": visibility,
            "TARGET_REPOSITORY": repository,
            "TARGET_REF": ref,
            "VALIDATE_SCRIPT": validate["run"],
        }
        return subprocess.run(
            ["bash", "-e", "-c", wrapper],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
        )

    assert run_validation("public").returncode == 0
    assert "repository=owner/public-repo\nref=main\n" == output_file.read_text(encoding="utf-8")
    assert run_validation("public", " main ", " pytest-dev/pytest ").returncode == 0
    assert "repository=pytest-dev/pytest\nref=main\n" == output_file.read_text(encoding="utf-8")
    assert run_validation("public", "   ").returncode != 0
    assert run_validation("public", "main\nmalicious=value").returncode != 0
    assert run_validation("private").returncode != 0


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
    assert target_checkout["with"]["repository"] == "${{ steps.validate-target.outputs.repository }}"
    assert target_checkout["with"]["ref"] == "${{ steps.validate-target.outputs.ref }}"
    assert target_checkout["with"]["path"] == "target-repository"
    assert target_checkout["with"]["persist-credentials"] == "false"
    assert "token" not in target_checkout["with"]
    assert "TARGET_REPO_READ_TOKEN" not in workflow.__repr__()


def test_dependency_preparation_isolated_and_test_execution_is_offline() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    workspace = next(
        step for step in steps if step.get("name") == "Create isolated target workspace"
    )
    prepare = next(
        step for step in steps
        if step.get("name") == "Prepare target dependencies in restricted network container"
    )
    test = next(
        step for step in steps
        if step.get("name") == "Run target pytest suite without network or credentials"
    )
    cleanup = next(
        step for step in steps
        if step.get("name") == "Remove prepared target workspace"
    )

    assert prepare["timeout-minutes"] == "15"
    assert "trap restore_workflow_commands EXIT" in prepare["run"]
    assert "pip install --no-cache-dir --only-binary=:all:" in prepare["run"]
    assert '--editable ".[dev]"' in prepare["run"]
    assert "python -m venv /sandbox/venv" in prepare["run"]
    assert "GITHUB_TOKEN" not in prepare["run"]
    assert "secrets." not in prepare["run"]
    assert "docker.sock" not in prepare["run"]

    prepare_tokens = _docker_run_tokens(prepare["run"])
    prepare_options, prepare_image, prepare_command = _docker_options(prepare_tokens)
    assert prepare_image == "code-test-lab-python:${GITHUB_SHA}"
    assert prepare_options["--network"] == ["bridge"]
    assert prepare_options["--read-only"] == ["true"]
    assert prepare_options["--mount"] == [
        "type=bind,src=${GITHUB_WORKSPACE}/target-repository,dst=/input/target,readonly",
        "type=bind,src=${TARGET_WORKSPACE},dst=/sandbox",
    ]
    assert prepare_options["--workdir"] == ["/sandbox"]
    assert prepare_options["--tmpfs"] == ["/tmp:rw,nosuid,size=1g"]
    assert prepare_options["--memory"] == ["4g"]
    assert prepare_options["--memory-swap"] == ["4g"]
    assert prepare_options["--cpus"] == ["2"]
    assert prepare_options["--pids-limit"] == ["256"]
    assert prepare_options["--cap-drop"] == ["ALL"]
    assert prepare_options["--security-opt"] == ["no-new-privileges"]
    assert prepare_options["--user"] == ["10001:10001"]
    assert prepare_options["--entrypoint"] == ["/bin/sh"]
    assert prepare_command[:1] == ["-ec"]
    assert "cp -a /input/target/. /sandbox/source/" in prepare_command[1]
    assert 'sudo chown 10001:10001 "$workspace"' in workspace["run"]
    assert "chmod 0777" not in workspace["run"]
    assert "is_relative_to(source)" in prepare_command[1]
    assert 'python -m pip install' in prepare_command[1]

    assert test["timeout-minutes"] == "20"
    assert "trap restore_workflow_commands EXIT" in test["run"]
    assert "stop-commands" in test["run"]
    assert "TARGET_WORKSPACE" in test["env"]
    test_tokens = _docker_run_tokens(test["run"])
    test_options, test_image, test_command = _docker_options(test_tokens)
    assert test_image == "code-test-lab-python:${GITHUB_SHA}"
    assert test_options["--network"] == ["none"]
    assert test_options["--read-only"] == ["true"]
    assert test_options["--mount"] == [
        "type=bind,src=${TARGET_WORKSPACE},dst=/sandbox,readonly"
    ]
    assert test_options["--workdir"] == ["/sandbox/source"]
    assert test_options["--tmpfs"] == ["/tmp:rw,noexec,nosuid,size=256m"]
    assert test_options["--memory"] == ["2g"]
    assert test_options["--memory-swap"] == ["2g"]
    assert test_options["--cpus"] == ["2"]
    assert test_options["--pids-limit"] == ["256"]
    assert test_options["--cap-drop"] == ["ALL"]
    assert test_options["--security-opt"] == ["no-new-privileges"]
    assert test_options["--user"] == ["10001:10001"]
    assert test_options["--entrypoint"] == ["/sandbox/venv/bin/python"]
    assert test_command[:4] == ["-I", "-B", "-m", "pytest"]
    assert test_command[-1] == "/sandbox/source"
    assert "GITHUB_TOKEN" not in test["run"]
    assert "secrets." not in test["run"]
    assert "docker.sock" not in test["run"]

    assert cleanup["if"] == "${{ always() }}"
    assert 'sudo rm -rf -- "$TARGET_WORKSPACE"' in cleanup["run"]


def test_target_code_is_never_run_on_host_and_cleanup_is_bounded() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    prepare = next(
        step for step in steps
        if step.get("name") == "Prepare target dependencies in restricted network container"
    )
    test = next(
        step for step in steps
        if step.get("name") == "Run target pytest suite without network or credentials"
    )
    assert "pip install" not in prepare.get("run", "").split("docker run", 1)[0]
    assert "python -m pytest" not in test.get("run", "").split("docker run", 1)[0]
    assert workflow["jobs"]["python-sandbox"]["timeout-minutes"] == "45"


def test_readme_documents_manual_dispatch_and_dependency_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "default branch" in readme
    assert "branch selector" in readme
    assert "Only public target repositories are accepted" in readme
    assert "15-minute dependency-preparation timeout" in readme
    assert "network-enabled preparation container" in readme
    assert 'pip install -e ".[dev]"' in readme
    assert "--network none" in readme
