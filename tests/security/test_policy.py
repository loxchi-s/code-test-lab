from pathlib import Path

import pytest
import yaml

from code_test_lab.isolation import (
    PolicyError,
    build_docker_command,
    load_execution_policy,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config/test-policy.yml"
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"


def test_ci_permissions_are_read_only() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))

    assert "permissions:\n  contents: read" in workflow
    assert "pull_request_target:" not in workflow
    assert "secrets." not in workflow
    assert policy["ci"]["permissions"] == {"contents": "read"}
    assert policy["ci"]["allow_pull_request_target"] is False
    assert policy["ci"]["expose_repository_secrets_to_test_container"] is False


def test_policy_generates_the_restricted_docker_argv() -> None:
    execution = load_execution_policy(POLICY_PATH)
    argv = build_docker_command(
        "code-test-lab:test",
        ["python", "-m", "pytest", "-q"],
        execution,
    )

    expected = [
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--memory", "2g",
        "--cpus", "2",
        "--pids-limit", "256",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "10001:10001",
    ]
    for flag in expected:
        assert flag in argv
    assert argv[-4:] == ["python", "-m", "pytest", "-q"]
    assert argv.count("docker") == 1


def test_container_command_is_passed_as_distinct_argv_items() -> None:
    execution = load_execution_policy(POLICY_PATH)
    marker = "test; touch /tmp/should-not-run"
    argv = build_docker_command("code-test-lab:test", ["python", "-c", marker], execution)

    assert argv[-3:] == ["python", "-c", marker]


def test_image_reference_rejects_option_injection() -> None:
    execution = load_execution_policy(POLICY_PATH)

    with pytest.raises(ValueError, match="image reference"):
        build_docker_command("--privileged", ["python", "-m", "pytest"], execution)


def test_workflow_delegates_docker_execution_to_policy_runner() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow.count("python -m code_test_lab.isolation") == 2
    assert "docker run" not in workflow
    assert "timeout-minutes: 45" in workflow


def test_container_timeout_is_derived_from_policy() -> None:
    execution = load_execution_policy(POLICY_PATH)
    assert execution["timeout_minutes"] == 20


def test_policy_rejects_network_access(tmp_path: Path) -> None:
    invalid = tmp_path / "policy.yml"
    invalid.write_text(
        POLICY_PATH.read_text(encoding="utf-8").replace(
            "network: disabled", "network: enabled"
        ),
        encoding="utf-8",
    )

    with pytest.raises(PolicyError, match="network: disabled"):
        load_execution_policy(invalid)


def test_policy_rejects_repository_secret_exposure(tmp_path: Path) -> None:
    invalid = tmp_path / "policy.yml"
    invalid.write_text(
        POLICY_PATH.read_text(encoding="utf-8").replace(
            "expose_repository_secrets_to_test_container: false",
            "expose_repository_secrets_to_test_container: true",
        ),
        encoding="utf-8",
    )

    with pytest.raises(PolicyError, match="secrets"):
        load_execution_policy(invalid)
