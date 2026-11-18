from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = (ROOT / "config/test-policy.yml").read_text(encoding="utf-8")


def test_ci_permissions_are_read_only() -> None:
    assert "contents: read" in POLICY
    assert "allow_pull_request_target: false" in POLICY


def test_execution_policy_disables_network_and_write_access() -> None:
    assert "network: disabled" in POLICY
    assert "filesystem: read-only" in POLICY


def test_execution_policy_bounds_resources() -> None:
    for setting in (
        'user: "10001:10001"',
        'memory: "2g"',
        "cpus: 2",
        "pids_limit: 256",
        "timeout_minutes: 20",
    ):
        assert setting in POLICY


def test_execution_policy_drops_privileges_and_secrets() -> None:
    assert "drop_capabilities: all" in POLICY
    assert "no_new_privileges: true" in POLICY
    assert "expose_repository_secrets_to_test_container: false" in POLICY
