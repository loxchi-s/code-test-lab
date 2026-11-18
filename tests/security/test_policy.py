import re
import shlex
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
WORKFLOW_SECURITY_PATH = ROOT / ".github/workflows/workflow-security.yml"
TEST_CONTAINER_NAME = "code-test-lab-" + "a" * 32
IMAGE_ID = "sha256:" + "a" * 64


def _workflow() -> dict:
    # BaseLoader preserves GitHub's unquoted "on" key as a string.
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _contains_untrusted_credential(expression: str) -> bool:
    return bool(re.search(
        r"\$\{\{[^}]*\b(?:secrets\s*(?:\.|\[)|github\.token\b)",
        expression,
        flags=re.IGNORECASE,
    ))


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
    assert not _contains_untrusted_credential(yaml.dump(workflow.get("env", {})))
    assert not _contains_untrusted_credential(yaml.dump(container_job.get("env", {})))
    for step in container_job["steps"]:
        assert not _contains_untrusted_credential(yaml.dump(step))

    quality_run = next(
        step["run"] for step in container_job["steps"]
        if step.get("name") == "Run quality checks without network or credentials"
    )
    execution = policy["execution"]
    expected_policy_flags = (
        "--network none",
        f"--tmpfs /tmp:rw,noexec,nosuid,size={execution['tmpfs_size']}",
        f"--memory {execution['memory']}",
        f"--memory-swap {execution['memory']}",
        f"--cpus {execution['cpus']}",
        f"--pids-limit {execution['pids_limit']}",
        f"--user {execution['user']}",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
    )
    docker_tokens = shlex.split(quality_run)
    image_index = next(
        index for index, token in enumerate(docker_tokens)
        if token.startswith("code-test-lab:")
    )
    docker_options = docker_tokens[2:image_index]
    for flag_value in expected_policy_flags:
        flag, value = flag_value.split(" ", maxsplit=1)
        assert docker_options.count(flag) == 1
        assert docker_options[docker_options.index(flag) + 1] == value
    readonly_options = [
        token for token in docker_options
        if token == "--read-only" or token.startswith("--read-only=")
    ]
    assert readonly_options in (["--read-only"], ["--read-only=true"])
    assert docker_options.count("--log-driver") == 1
    assert docker_options[docker_options.index("--log-driver") + 1] == "json-file"
    log_options = [
        docker_options[index + 1]
        for index, token in enumerate(docker_options)
        if token == "--log-opt"
    ]
    assert log_options == ["max-size=10m", "max-file=3"]
    forwarded_env = [
        docker_options[index + 1]
        for index, token in enumerate(docker_options)
        if token == "--env"
    ]
    assert forwarded_env == [
        "HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=",
        "http_proxy=", "https_proxy=", "all_proxy=",
        "PYTHONPYCACHEPREFIX=/tmp/pycache", "XDG_CACHE_HOME=/tmp/cache",
    ]
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
    build_step = next(step for step in job["steps"] if step.get("name") == "Build validation image")
    audit_step = next(
        step for step in job["steps"]
        if step.get("name") == "Audit installed dependencies in a restricted network container"
    )
    assert build_step["id"] == "build-image"
    assert "steps.build-image.outcome == 'success'" in audit_step["if"]
    assert "!cancelled()" in audit_step["if"]
    assert any("docker build" in run for run in run_steps)
    assert any("docker run" in run and "pytest" in run for run in run_steps)
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
        ("cpus: 2", "cpus: 0.001", "0.01"),
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    one_minute_policy = tmp_path / "one-minute-policy.yml"
    one_minute_policy.write_text(
        POLICY_PATH.read_text(encoding="utf-8").replace(
            "timeout_minutes: 20", "timeout_minutes: 1",
        ),
        encoding="utf-8",
    )

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[1] == "run":
            raise TimeoutExpired(argv, timeout=60)
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(one_minute_policy), "--image", IMAGE_ID,
        "--", "python", "-m", "pytest",
    ])
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", fake_run)
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda _: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("code_test_lab.docker.subprocess.run", fake_run)
    assert main() == 124
    assert calls[0][0][0:3] == ["docker", "run", "--rm"]
    assert calls[0][1]["timeout"] == 60
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


def test_workflow_credential_guard_detects_bracket_and_dot_contexts() -> None:
    assert _contains_untrusted_credential("${{ secrets['TOKEN'] }}")
    assert _contains_untrusted_credential("${{ secrets.TOKEN }}")
    assert _contains_untrusted_credential("${{ github.token }}")
    assert not _contains_untrusted_credential("${{ github.sha }}")


def test_workflow_security_scans_are_independent_and_cover_merge_history() -> None:
    workflow = yaml.load(
        WORKFLOW_SECURITY_PATH.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    jobs = workflow["jobs"]
    assert {"actionlint", "gitleaks", "zizmor"} <= set(jobs)
    assert all(not job.get("needs") and not job.get("if") for job in jobs.values())

    gitleaks_steps = jobs["gitleaks"]["steps"]
    checkout = next(
        step for step in gitleaks_steps
        if step.get("uses", "").startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == "0"
    scan = next(
        step["run"] for step in gitleaks_steps
        if step.get("name") == "Scan all commits using trusted base-branch rules"
    )
    scan_lines = scan.splitlines()
    command_start = next(
        index for index, line in enumerate(scan_lines)
        if line.lstrip().startswith("gitleaks git ")
    )
    command_lines = [scan_lines[command_start]]
    while command_lines[-1].rstrip().endswith(chr(92)):
        command_lines.append(scan_lines[command_start + len(command_lines)])
    command = " ".join(
        line.rstrip().removesuffix(chr(92)) for line in command_lines
    )
    command_tokens = shlex.split(command)
    assert command_tokens[:2] == ["gitleaks", "git"]
    assert command_tokens[2] == "$GITHUB_WORKSPACE"
    assert command_tokens[command_tokens.index("--config") + 1] == "$trusted_config"
    assert (
        command_tokens[command_tokens.index("--gitleaks-ignore-path") + 1]
        == "$trusted_ignore"
    )
    assert "--redact" in command_tokens
    assert "--no-banner" in command_tokens
    assert (
        '--log-opts=--all --full-history --diff-filter=uxdb --diff-merges=first-parent'
        in command_tokens
    )

    zizmor_steps = jobs["zizmor"]["steps"]
    assert any("python -I -m venv" in step.get("run", "") for step in zizmor_steps)
    assert any(
        "/zizmor-venv/bin/python -I -m pip" in step.get("run", "")
        for step in zizmor_steps
    )


def test_policy_accepts_exact_decimal_cpu_nanocpus(tmp_path: Path) -> None:
    valid = tmp_path / "decimal-cpus.yml"
    valid.write_text(
        POLICY_PATH.read_text(encoding="utf-8").replace("cpus: 2", "cpus: 1.003"),
        encoding="utf-8",
    )
    assert load_execution_policy(valid)["cpus"] == 1.003


def test_policy_preserves_yaml_merge_precedence(tmp_path: Path) -> None:
    merged = tmp_path / "merged-policy.yml"
    text = POLICY_PATH.read_text(encoding="utf-8").replace(
        "execution:\n",
        'defaults: &defaults\n  memory: "512m"\nexecution:\n  <<: *defaults\n',
        1,
    )
    merged.write_text(text, encoding="utf-8")
    assert load_execution_policy(merged)["memory"] == "2g"


def test_main_interrupt_before_container_launch_returns_130_without_cleanup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt_inspection(_: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(POLICY_PATH), "--image", "local:test",
    ])
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", interrupt_inspection)
    monkeypatch.setattr(
        "code_test_lab.cli.remove_container",
        lambda _: pytest.fail("cleanup must not run before Docker launch"),
    )
    assert main() == 130
    assert capsys.readouterr().err == ""


def test_workflow_security_uses_trusted_gitleaks_config_and_no_zizmor_config() -> None:
    workflow = yaml.load(
        WORKFLOW_SECURITY_PATH.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    gitleaks_job = workflow["jobs"]["gitleaks"]
    scan_step = next(
        step for step in gitleaks_job["steps"]
        if step.get("name") == "Scan all commits using trusted base-branch rules"
    )
    assert "TRUSTED_BASE_SHA" in scan_step["env"]
    assert "git worktree add" in scan_step["run"]
    assert 'gitleaks git "$GITHUB_WORKSPACE"' in scan_step["run"]
    assert '--config "$trusted_config"' in scan_step["run"]
    assert '--gitleaks-ignore-path "$trusted_ignore"' in scan_step["run"]

    zizmor_job = workflow["jobs"]["zizmor"]
    audit = next(
        step["run"] for step in zizmor_job["steps"]
        if step.get("name") == "Audit workflow security"
    )
    assert "--no-config" in audit
    assert "${{ runner.temp }}/zizmor-venv/bin/zizmor" in audit


def test_readme_warns_about_untrusted_docker_build_context() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "only for a trusted checkout" in readme
    assert "disposable builder with no proxy credentials" in readme
