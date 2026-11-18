from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
import yaml

from code_test_lab.cli import main
from code_test_lab.docker import build_docker_command, remove_container, resolve_image_id
from code_test_lab.policy import PolicyError, load_execution_policy

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config/test-policy.yml"
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
TEST_CONTAINER_NAME = "code-test-lab-" + "a" * 32
IMAGE_ID = "sha256:" + "a" * 64


def _workflow() -> dict:
    # BaseLoader preserves GitHub's unquoted "on" key as a string.
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_ci_permissions_and_untrusted_contexts_are_restricted() -> None:
    workflow = _workflow()
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        permissions = job.get("permissions")
        assert permissions is None or permissions == {"contents": "read"}

    events = workflow["on"]
    if isinstance(events, str):
        event_names = {events}
    elif isinstance(events, list):
        event_names = set(events)
    else:
        event_names = set(events)
    assert "pull_request_target" not in event_names

    container_job = workflow["jobs"]["quality"]
    for step in container_job["steps"]:
        run = step.get("run", "")
        assert not __import__("re").search(
            r"\$\{\{[^}]*\b(?:secrets(?:\s*\[)|secrets\.|github\.token)\b",
            run,
            flags=__import__("re").IGNORECASE,
        )
    assert policy["ci"]["permissions"] == {"contents": "read"}
    assert policy["ci"]["allow_pull_request_target"] is False
    assert policy["ci"]["expose_repository_secrets_to_test_container"] is False


def test_policy_generates_exact_restricted_docker_argv() -> None:
    execution = load_execution_policy(POLICY_PATH)
    argv = build_docker_command(
        IMAGE_ID, ["python", "-m", "pytest", "-q"], execution,
        container_name=TEST_CONTAINER_NAME,
    )
    expected_pairs = {
        "--name": TEST_CONTAINER_NAME,
        "--network": "none",
        "--tmpfs": "/tmp:rw,noexec,nosuid,size=256m",
        "--memory": "2g",
        "--memory-swap": "2g",
        "--cpus": "2",
        "--pids-limit": "256",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--user": "10001:10001",
    }
    for flag, value in expected_pairs.items():
        assert argv.count(flag) == 1
        assert argv[argv.index(flag) + 1] == value
    assert argv[-4:] == ["python", "-m", "pytest", "-q"]
    assert argv.count("--read-only") == 1
    assert argv.count("--network") == 1
    assert "max-size=10m" in argv
    assert "max-file=3" in argv
    for proxy in ("HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=", "http_proxy=", "https_proxy=", "all_proxy="):
        assert proxy in argv


def test_container_command_is_passed_as_distinct_argv_items() -> None:
    execution = load_execution_policy(POLICY_PATH)
    marker = "test; touch /tmp/should-not-run"
    argv = build_docker_command(
        IMAGE_ID, ["python", "-c", marker], execution,
        container_name=TEST_CONTAINER_NAME,
    )
    assert argv[-3:] == ["python", "-c", marker]


def test_container_command_allows_empty_non_executable_operands() -> None:
    execution = load_execution_policy(POLICY_PATH)
    argv = build_docker_command(
        IMAGE_ID, ["printf", "%s", ""], execution,
        container_name=TEST_CONTAINER_NAME,
    )
    assert argv[-3:] == ["printf", "%s", ""]


def test_image_reference_rejects_option_injection() -> None:
    execution = load_execution_policy(POLICY_PATH)
    with pytest.raises(ValueError, match="immutable"):
        build_docker_command(
            "--privileged", ["python", "-m", "pytest"], execution,
            container_name=TEST_CONTAINER_NAME,
        )


def test_container_name_rejects_untrusted_values() -> None:
    execution = load_execution_policy(POLICY_PATH)
    with pytest.raises(ValueError, match="generated"):
        build_docker_command(
            IMAGE_ID, ["python", "-m", "pytest"], execution,
            container_name="--privileged",
        )


def test_resolve_image_id_rejects_declared_volumes(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    metadata = {"Id": IMAGE_ID, "Config": {"Volumes": {"/data": {}}}}
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda argv, **kwargs: CompletedProcess(
            argv, 0, stdout=json.dumps(metadata), stderr="",
        ),
    )
    with pytest.raises(ValueError, match="volumes"):
        resolve_image_id("local:test")


def test_resolve_image_id_returns_immutable_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    metadata = {"Id": IMAGE_ID, "Config": {"Volumes": None}}
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda argv, **kwargs: CompletedProcess(
            argv, 0, stdout=json.dumps(metadata), stderr="",
        ),
    )
    assert resolve_image_id("local:test") == IMAGE_ID


def test_timeout_cleanup_uses_fixed_docker_rm_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("code_test_lab.docker.subprocess.run", fake_run)
    assert remove_container(TEST_CONTAINER_NAME) is True
    assert captured["argv"] == ["/usr/bin/docker", "rm", "--force", TEST_CONTAINER_NAME]
    assert captured["kwargs"] == {
        "check": False, "shell": False, "timeout": 10,
        "capture_output": True, "text": True,
    }


def test_timeout_cleanup_reports_missing_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: None)
    assert remove_container(TEST_CONTAINER_NAME) is False


def test_workflow_runs_quality_inside_the_isolated_container() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["quality"]
    assert int(job["timeout-minutes"]) == 45
    run_steps = [step.get("run", "") for step in job["steps"]]
    assert any("docker build" in run for run in run_steps)
    assert any("docker run" in run for run in run_steps)
    assert not any("pip install -e" in run for run in run_steps)


def test_container_timeout_is_derived_from_policy() -> None:
    assert load_execution_policy(POLICY_PATH)["timeout_minutes"] == 20


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("network: disabled", "network: enabled", "network: disabled"),
        ('user: "10001:10001"', 'user: "0:0"', "non-root"),
        ('memory: "2g"', 'memory: "999g"', "memory"),
        ('memory: "2g"', 'memory: "1m"', "memory"),
        ('tmpfs_size: "256m"', 'tmpfs_size: "9999m"', "tmpfs_size"),
        ("cpus: 2", "cpus: 999", "cpus"),
        ("cpus: 2", "cpus: 0.1234567891", "NanoCPUs"),
        ("pids_limit: 256", "pids_limit: 99999", "pids_limit"),
        ("timeout_minutes: 20", "timeout_minutes: 21", "timeout_minutes"),
        ('user: "10001:10001"', 'user: "١٠٠٠١:10001"', "ASCII"),
    ],
)
def test_policy_rejects_unsafe_execution_limits(
    tmp_path: Path, old: str, new: str, message: str,
) -> None:
    invalid = tmp_path / "policy.yml"
    policy_text = POLICY_PATH.read_text(encoding="utf-8")
    assert old in policy_text
    invalid.write_text(policy_text.replace(old, new), encoding="utf-8")
    with pytest.raises(PolicyError, match=message):
        load_execution_policy(invalid)


def test_policy_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    invalid = tmp_path / "duplicate.yml"
    text = POLICY_PATH.read_text(encoding="utf-8").replace(
        '  memory: "2g"', '  memory: "512m"\n  memory: "2g"',
    )
    invalid.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError, match="duplicate key"):
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


def test_main_cleans_up_container_after_timeout_and_uses_policy_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[1] == "run":
            raise TimeoutExpired(argv, timeout=1200)
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(POLICY_PATH), "--image", IMAGE_ID,
        "--", "python", "-m", "pytest",
    ])
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", fake_run)
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda _: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("code_test_lab.docker.subprocess.run", fake_run)
    assert main() == 124
    assert calls[0][0][0:3] == ["docker", "run", "--rm"]
    assert calls[0][1]["timeout"] == 1200
    container_name = calls[0][0][calls[0][0].index("--name") + 1]
    assert calls[1][0] == ["/usr/bin/docker", "rm", "--force", container_name]


def test_main_reports_failed_timeout_cleanup(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def timeout_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        raise TimeoutExpired(argv, timeout=1200)

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(POLICY_PATH), "--image", "local:test",
        "--", "python", "-m", "pytest",
    ])
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda _: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", timeout_run)
    monkeypatch.setattr("code_test_lab.cli.remove_container", lambda _: False)
    assert main() == 125
    assert "cleanup could not be confirmed" in capsys.readouterr().err


def test_main_cleans_up_after_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def interrupt_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(POLICY_PATH), "--image", "local:test",
        "--", "python", "-m", "pytest",
    ])
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda _: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", interrupt_run)
    monkeypatch.setattr("code_test_lab.cli.remove_container", lambda name: calls.append(name) or True)
    assert main() == 130
    assert len(calls) == 1
