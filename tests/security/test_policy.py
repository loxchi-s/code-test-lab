from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = (ROOT / "config/test-policy.yml").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_ci_permissions_are_read_only() -> None:
    assert "permissions:\n  contents: read" in WORKFLOW
    assert "pull_request_target:" not in WORKFLOW
    assert "secrets." not in WORKFLOW
    assert "allow_pull_request_target: false" in POLICY
    assert "expose_repository_secrets_to_test_container: false" in POLICY


def test_execution_policy_is_enforced_by_both_docker_runs() -> None:
    expected_flags = (
        "--network none",
        "--read-only",
        "--memory 2g",
        "--cpus 2",
        "--pids-limit 256",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 10001:10001",
    )
    for flag in expected_flags:
        assert WORKFLOW.count(flag) == 2, (
            f"Expected both restricted Docker runs to use {flag!r}"
        )


def test_execution_policy_bounds_container_validation_duration() -> None:
    assert "timeout_minutes: 20" in POLICY
    assert (
        "container-validation:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 20"
    ) in WORKFLOW


def test_policy_declares_expected_isolation_controls() -> None:
    for setting in (
        "network: disabled",
        "filesystem: read-only",
        "drop_capabilities: all",
        "no_new_privileges: true",
    ):
        assert setting in POLICY
