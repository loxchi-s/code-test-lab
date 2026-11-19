from __future__ import annotations

import ast
import os
import shlex
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/python-repository-sandbox.yml"
CI_WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _docker_run_tokens(script: str) -> list[str]:
    """Parse a continued Docker command, including multiline quoted shell arguments."""
    lines = script.splitlines()
    start = next(
        index for index, line in enumerate(lines)
        if line.lstrip().startswith("docker run --rm")
    )
    command_lines: list[str] = []
    for line in lines[start:]:
        command_lines.append(line)
        if line.rstrip().endswith(chr(92)):
            continue
        command = " ".join(
            item.rstrip().removesuffix(chr(92)) for item in command_lines
        )
        try:
            tokens = shlex.split(command)
        except ValueError as error:
            if "No closing quotation" in str(error):
                continue
            raise
        if tokens[-1:] == ["2>&1"]:
            tokens.pop()
        return tokens
    raise AssertionError("Docker command did not terminate with balanced quoting")


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
    assert 'TARGET_REPOSITORY="$(trim "$TARGET_REPOSITORY")"' in validate["run"]
    assert 'TARGET_REF="$(trim "$TARGET_REF")"' in validate["run"]
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
    wrapper = r'''curl() { printf '{"visibility":"%s"}' "$TEST_VISIBILITY"; }; jq() { printf '%s\n' "$TEST_VISIBILITY"; }; eval "$VALIDATE_SCRIPT"'''

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
    assert target_checkout["id"] == "checkout-target"
    assert "token" not in target_checkout["with"]
    assert "TARGET_REPO_READ_TOKEN" not in workflow.__repr__()


def test_dependency_preparation_isolated_and_test_execution_is_offline() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    workspace = next(
        step for step in steps if step.get("name") == "Create size-bounded host tmpfs workspace"
    )
    prepare = next(
        step for step in steps
        if step.get("name") == "Prepare target dependencies in restricted network container"
    )
    metadata = next(
        step for step in steps
        if step.get("name") == "Resolve target distribution with trusted metadata parser"
    )
    test = next(
        step for step in steps
        if step.get("name") == "Run target pytest suite without network or credentials"
    )
    cleanup = next(
        step for step in steps
        if step.get("name") == "Remove prepared target containers and unmount tmpfs workspace"
    )

    assert prepare["timeout-minutes"] == "15"
    assert "trap restore_workflow_commands EXIT" in prepare["run"]
    assert "--requirements-file /tmp/target-requirements.txt" in prepare["run"]
    assert "--distribution-file /tmp/target-distribution" in prepare["run"]
    assert "--requirement /tmp/target-requirements.txt" in prepare["run"]
    assert "TARGET_SEED_VERSION" in prepare["run"]
    assert '--seed-version "$TARGET_SEED_VERSION"' in prepare["run"]
    assert "--no-deps --editable ." in prepare["run"]
    assert "python -m venv /sandbox/venv" in prepare["run"]
    assert "TARGET_COMMIT" in prepare["env"]
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
    assert "code_test_lab.target_preparation --source /input/target" in prepare_command[1]
    assert "--destination /sandbox/source" in prepare_command[1]
    assert "--requirements-file /tmp/target-requirements.txt" in prepare_command[1]
    assert "--distribution-file /tmp/target-distribution" in prepare_command[1]
    assert '--commit-sha "$TARGET_COMMIT"' in prepare_command[1]
    assert "TARGET_SEED_VERSION" in prepare_command[1]
    assert "pip uninstall --yes" in prepare_command[1]
    assert "--no-deps --editable ." in prepare_command[1]
    assert "SETUPTOOLS_SCM_PRETEND_VERSION=*|" in prepare_command[1]
    assert "VCS_VERSIONING_PRETEND_VERSION_FOR_" in prepare_command[1]
    assert "TARGET_COMMIT=${TARGET_COMMIT}" in prepare_options["--env"]
    assert "mktemp -d" in workspace["run"]
    assert "GITHUB_OUTPUT" in workspace["run"]
    assert "sudo mount -t tmpfs" in workspace["run"]
    assert "size=2g,uid=10001,gid=10001,mode=0755" in workspace["run"]
    assert "python -m pip install" in prepare_command[1]

    assert metadata["id"] == "target-metadata"
    assert metadata["if"] == "${{ steps.prepare-dependencies.outcome == 'success' }}"
    assert metadata["timeout-minutes"] == "2"
    assert "TARGET_COMMIT" in metadata["env"]
    assert "GITHUB_OUTPUT" in metadata["run"]
    assert "distribution=\"$(cat \"$metadata_file\")\"" in metadata["run"]
    assert 'distribution="${distribution,,}"' in metadata["run"]
    assert "pip install" not in metadata["run"]
    metadata_tokens = _docker_run_tokens(metadata["run"])
    metadata_options, metadata_image, metadata_command = _docker_options(metadata_tokens)
    assert metadata_image == "code-test-lab-python:${GITHUB_SHA}"
    assert metadata_options["--network"] == ["none"]
    assert metadata_options["--read-only"] == ["true"]
    assert metadata_options["--mount"] == [
        "type=bind,src=${GITHUB_WORKSPACE}/target-repository,dst=/input/target,readonly"
    ]
    assert metadata_options["--tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m"]
    assert metadata_options["--memory"] == ["512m"]
    assert metadata_options["--memory-swap"] == ["512m"]
    assert metadata_options["--cpus"] == ["1"]
    assert metadata_options["--pids-limit"] == ["64"]
    assert metadata_options["--cap-drop"] == ["ALL"]
    assert metadata_options["--security-opt"] == ["no-new-privileges"]
    assert metadata_options["--user"] == ["10001:10001"]
    assert metadata_options["--env"] == ["TARGET_COMMIT=${TARGET_COMMIT}"]
    assert metadata_options["--entrypoint"] == ["/bin/sh"]
    assert metadata_command[:1] == ["-ec"]
    assert "python -I -m code_test_lab.target_preparation --source /input/target" in metadata_command[1]
    assert "--distribution-file /tmp/target-distribution" in metadata_command[1]
    assert "cat /tmp/target-distribution" in metadata_command[1]

    assert test["if"] == "${{ steps.target-metadata.outcome == 'success' }}"
    assert test["timeout-minutes"] == "20"
    assert "trap restore_workflow_commands EXIT" in test["run"]
    assert "stop-commands" in test["run"]
    assert "TARGET_WORKSPACE" in test["env"]
    assert "TARGET_SOURCE" in test["env"]
    assert "TARGET_DISTRIBUTION" in test["env"]
    test_tokens = _docker_run_tokens(test["run"])
    test_options, test_image, test_command = _docker_options(test_tokens)
    assert test_image == "code-test-lab-python:${GITHUB_SHA}"
    assert test_options["--network"] == ["none"]
    assert test_options["--read-only"] == ["true"]
    assert test_options["--mount"] == [
        "type=bind,src=${TARGET_WORKSPACE},dst=/sandbox,readonly",
        "type=bind,src=${TARGET_SOURCE},dst=/input/target,readonly",
    ]
    assert test_options["--workdir"] == ["/input/target"]
    assert test_options["--mount"][1].endswith(",readonly")
    assert test_options["--tmpfs"] == ["/tmp:rw,noexec,nosuid,size=256m"]
    assert test_options["--memory"] == ["2g"]
    assert test_options["--memory-swap"] == ["2g"]
    assert test_options["--cpus"] == ["2"]
    assert test_options["--pids-limit"] == ["256"]
    assert test_options["--cap-drop"] == ["ALL"]
    assert test_options["--security-opt"] == ["no-new-privileges"]
    assert test_options["--user"] == ["10001:10001"]
    assert "TARGET_DISTRIBUTION=${TARGET_DISTRIBUTION}" in test_options["--env"]
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in test_options["--env"]
    assert test_options["--entrypoint"] == ["/bin/sh"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/sandbox/venv/bin" not in dockerfile
    assert test_command[:1] == ["-ec"]
    test_script = test_command[1]
    assert 'if [ "$TARGET_DISTRIBUTION" = "pytest" ]; then' in test_script
    assert test_script.count("exec /opt/trusted-runner/bin/python -I -B /opt/trusted-runner/trusted_test_runner.py") == 2
    assert "--target-pytest" in test_script
    assert "/sandbox/venv/bin/python" not in test_script
    assert "/opt/trusted-runner" in dockerfile
    runner = (ROOT / "src/code_test_lab/trusted_test_runner.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--target-pytest", action="store_true")' in runner
    main_body = runner.split("def main", maxsplit=1)[1]
    target_branch = main_body.split("if args.target_pytest:", maxsplit=1)[1].split(
        "else:", maxsplit=1
    )[0]
    trusted_branch = main_body.split("else:", maxsplit=1)[1]
    assert target_branch.index("configure_target_import_paths") < target_branch.index("import pytest")
    assert trusted_branch.index("import pytest") < trusted_branch.index("configure_target_import_paths")
    assert "sys.path[trusted_index:trusted_index] = target_paths" in runner
    assert "sys.path.append(target_paths[-1])" in runner
    runner_tree = ast.parse(runner)
    assert not any(
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Attribute) and node.func.attr == "addsitedir"
            or isinstance(node.func, ast.Name) and node.func.id == "addsitedir"
        )
        for node in ast.walk(runner_tree)
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "site"
        and any(alias.name == "addsitedir" for alias in node.names)
        for node in ast.walk(runner_tree)
    )
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in test["run"]
    assert "GITHUB_TOKEN" not in test["run"]
    assert "secrets." not in test["run"]
    assert "docker.sock" not in test["run"]

    assert cleanup["if"] == "${{ always() }}"
    assert 'docker rm -f "$PREP_CONTAINER" "$TEST_CONTAINER"' in cleanup["run"]
    assert 'sudo umount "$TARGET_WORKSPACE"' in cleanup["run"]
    assert 'mountpoint -q "$TARGET_WORKSPACE"' in cleanup["run"]
    assert 'rmdir "$TARGET_WORKSPACE"' in cleanup["run"]


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
    assert "trusted metadata-only container" in readme
    assert "trusted test runner" in readme
    assert "--requirement /tmp/target-requirements.txt" in readme
    assert "--no-deps --editable ." in readme
    assert "resolver-compatible seed version" in readme
    assert "--network none" in readme


def test_ci_smokes_trusted_runner_against_poisoned_environment() -> None:
    ci_workflow = yaml.load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = ci_workflow["jobs"]["quality"]["steps"]
    smoke = next(
        step for step in steps
        if step.get("name") == "Smoke test trusted runner against poisoned prepared environment"
    )
    assert smoke["timeout-minutes"] == "3"
    assert "untrusted pytest imported" in smoke["run"]
    assert "exit 97" in smoke["run"]
    assert "/opt/trusted-runner/trusted_test_runner.py" in smoke["run"]
    assert "/opt/trusted-runner/bin/python" in smoke["run"]
    assert "--network none --read-only" in smoke["run"]
    assert "dst=/sandbox,readonly" in smoke["run"]
    assert "dst=/input/target,readonly" in smoke["run"]


def test_ci_smokes_bounded_host_tmpfs_persistence_and_readonly_mount() -> None:
    ci_workflow = yaml.load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = ci_workflow["jobs"]["quality"]["steps"]
    smoke = next(
        step for step in steps
        if step.get("name") == "Smoke test bounded host tmpfs workspace lifecycle"
    )
    cleanup = next(
        step for step in steps
        if step.get("name") == "Remove tmpfs smoke containers and unmount workspace"
    )

    assert smoke["timeout-minutes"] == "3"
    assert "sudo mount -t tmpfs" in smoke["run"]
    assert "size=64m,uid=10001,gid=10001,mode=0755" in smoke["run"]
    assert 'type=bind,src=$workspace,dst=/workspace"' in smoke["run"]
    assert 'type=bind,src=$workspace,dst=/workspace,readonly"' in smoke["run"]
    assert 'probe.write_text("blocked")' in smoke["run"]
    assert cleanup["if"] == "${{ always() }}"
    assert "WORKSPACE" in cleanup["env"]
    assert 'sudo umount "$WORKSPACE"' in cleanup["run"]
    assert 'mountpoint -q "$WORKSPACE"' in cleanup["run"]
