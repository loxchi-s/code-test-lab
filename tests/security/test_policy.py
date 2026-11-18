from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from code_test_lab.isolation import (
    PolicyError,
    _remove_container,
    build_docker_command,
    load_execution_policy,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config/test-policy.yml"
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
TEST_CONTAINER_NAME = "code-test-lab-" + "a" * 32


def test_ci_permissions_are_read_only() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))

    assert "permissions:\\n  contents: read" in workflow
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
        container_name=TEST_CONTAINER_NAME,
    )

    expected = [
        "--name", TEST_CONTAINER_NAME,
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--memory", "2g",
        "--memory-swap", "2g",
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
    argv = build_docker_command(
        "code-test-lab:test",
        ["python", "-c", marker],
        execution,
        container_name=TEST_CONTAINER_NAME,
    )

    assert argv[-3:] == ["python", "-c", marker]


def test_image_reference_rejects_option_injection() -> None:
    execution = load_execution_policy(POLICY_PATH)

    with pytest.raises(ValueError, match="image reference"):
        build_docker_command(
            "--privileged",
            ["python", "-m", "pytest"],
            execution,
            container_name=TEST_CONTAINER_NAME,
        )


def test_container_name_rejects_untrusted_values() -> None:
    execution = load_execution_policy(POLICY_PATH)

    with pytest.raises(ValueError, match="generated"):
        build_docker_command(
            "code-test-lab:test",
            ["python", "-m", "pytest"],
            execution,
            container_name="--privileged",
        )


def test_timeout_cleanup_uses_fixed_docker_rm_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("code_test_lab.isolation.subprocess.run", fake_run)

    assert _remove_container(TEST_CONTAINER_NAME) is True
    assert captured["argv"] == ["docker", "rm", "--force", TEST_CONTAINER_NAME]
    assert captured["kwargs"] == {
        "check": False,
        "shell": False,
        "timeout": 10,
        "capture_output": True,
        "text": True,
    }


def test_timeout_cleanup_reports_missing_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_docker(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr("code_test_lab.isolation.subprocess.run", missing_docker)

    assert _remove_container(TEST_CONTAINER_NAME) is False


def test_workflow_delegates_docker_execution_to_policy_runner() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow.count("python -m code_test_lab.isolation") == 2
    assert "docker run" not in workflow
    assert "timeout-minutes: 45" in workflow


def test_container_timeout_is_derived_from_policy() -> None:
    execution = load_execution_policy(POLICY_PATH)
    assert execution["timeout_minutes"] == 20


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("network: disabled", "network: enabled", "network: disabled"),
        ('user: "10001:10001"', 'user: "0:0"', "non-root"),
        ('memory: "2g"', 'memory: "999g"', "memory"),
        ('tmpfs_size: "256m"', 'tmpfs_size: "9999m"', "tmpfs_size"),
        ("cpus: 2", "cpus: 999", "cpus"),
        ("pids_limit: 256", "pids_limit: 99999", "pids_limit"),
        ("timeout_minutes: 20", "timeout_minutes: 21", "timeout_minutes"),
    ],
)
def test_policy_rejects_unsafe_execution_limits(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    invalid = tmp_path / "policy.yml"
    policy_text = POLICY_PATH.read_text(encoding="utf-8")
    assert old in policy_text
    invalid.write_text(policy_text.replace(old, new), encoding="utf-8")

    with pytest.raises(PolicyError, match=message):
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
