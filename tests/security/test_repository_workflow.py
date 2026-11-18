from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/python-repository-sandbox.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_external_python_workflow_requires_manual_repository_input() -> None:
    workflow = _workflow()
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
    assert dispatch["repository"]["required"] == "true"
    assert dispatch["repository"]["type"] == "string"
    assert dispatch["ref"]["default"] == "main"
    assert workflow["permissions"] == {"contents": "read"}


def test_external_checkout_uses_fixed_workspace_and_does_not_persist_credentials() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["python-sandbox"]["steps"]
    checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    assert len(checkouts) == 2

    harness_checkout, target_checkout = checkouts
    assert harness_checkout["with"]["path"] == "sandbox-harness"
    assert harness_checkout["with"]["persist-credentials"] == "false"
    assert target_checkout["with"]["repository"] == "${{ inputs.repository }}"
    assert target_checkout["with"]["ref"] == "${{ inputs.ref }}"
    assert target_checkout["with"]["path"] == "target-repository"
    assert target_checkout["with"]["persist-credentials"] == "false"
    assert target_checkout["with"]["token"] == "${{ secrets.TARGET_REPO_READ_TOKEN || github.token }}"


def test_target_code_runs_only_in_restricted_docker_container() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["python-sandbox"]
    steps = job["steps"]
    build = next(step for step in steps if step.get("id") == "build-image")
    run_step = next(
        step for step in steps
        if step.get("name") == "Run target pytest suite without network or credentials"
    )
    command = run_step["run"]

    assert "working-directory" == build["working-directory"] or build["working-directory"] == "sandbox-harness"
    assert "docker build" in build["run"]
    for required in (
        "--network none",
        "--read-only",
        "dst=/workspace/target,readonly",
        "--memory 2g --memory-swap 2g --cpus 2 --pids-limit 256",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 10001:10001",
        "--env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "-m pytest -q -p no:cacheprovider /workspace/target",
    ):
        assert required in command
    assert "--privileged" not in command
    assert "/var/run/docker.sock" not in command
    assert "GITHUB_TOKEN" not in command
    assert "secrets." not in command
    assert job["timeout-minutes"] == "35"
