from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ci_uses_read_only_repository_permissions() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "permissions:" in workflow
    assert "contents: read" in workflow
    assert "pull_request_target" not in workflow


def test_container_ci_enforces_isolation_controls() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for control in (
        "--network none",
        "--read-only",
        "--memory 2g",
        "--cpus 2",
        "--pids-limit 256",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 10001:10001",
    ):
        assert control in workflow


def test_policy_does_not_allow_secrets_in_test_container() -> None:
    policy = (ROOT / "config/test-policy.yml").read_text(encoding="utf-8")
    assert "expose_repository_secrets_to_test_container: false" in policy
    assert "network: disabled" in policy
