import re
import shlex
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
import yaml

from code_test_lab.cli import main
from code_test_lab.docker import (
    DockerTarget,
    build_docker_command,
    remove_container,
    resolve_docker_target,
    resolve_image_id,
)
from code_test_lab.policy import PolicyError, load_execution_policy

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config/test-policy.yml"
WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
WORKFLOW_SECURITY_PATH = ROOT / ".github/workflows/workflow-security.yml"
TEST_CONTAINER_NAME = "code-test-lab-" + "a" * 32
IMAGE_ID = "sha256:" + "a" * 64
DOCKER_TARGET = DockerTarget("/usr/bin/docker")


def _docker_info_or_metadata(argv: list[str], metadata: object) -> CompletedProcess[str]:
    if "info" in argv:
        return CompletedProcess(argv, 0, stdout="[]", stderr="")
    import json

    return CompletedProcess(argv, 0, stdout=json.dumps(metadata), stderr="")


def _workflow() -> dict:
    # BaseLoader preserves GitHub's unquoted "on" key as a string.
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _is_single_fail_fast_docker_run(script: str) -> bool:
    """Reject shell suffixes that could mask the Docker command exit status."""
    normalized = re.sub(r"\\\n\s*", " ", script)
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    shell_operators = {";", "&&", "||", "&", "|"}
    return (
        tokens[:2] == ["docker", "run"]
        and tokens.count("docker") == 1
        and "pytest" in normalized
        and not any(token in shell_operators for token in tokens)
    )


def _normalize_read_only_option(option: str) -> str:
    """Canonicalize Docker's explicit true spelling before allowlist validation."""
    return "--read-only" if option == "--read-only=true" else option


def _contains_untrusted_credential(expression: str) -> bool:
    return bool(re.search(
        r"""\$\{\{[^}]*\b(?:secrets\s*(?:\.|\[)|github\s*(?:\.\s*token\b|\[\s*['"]token['"]\s*\]))""",
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
    docker_tokens = shlex.split(quality_run.replace(chr(92) + "\n", " "))
    image_index = next(
        index for index, token in enumerate(docker_tokens)
        if token.startswith("code-test-lab:")
    )
    docker_options = docker_tokens[2:image_index]
    approved_options = {
        "--rm", "--network", "--read-only", "--tmpfs", "--memory", "--memory-swap",
        "--cpus", "--pids-limit", "--log-driver", "--log-opt", "--env",
        "--cap-drop", "--security-opt", "--user", "--entrypoint",
    }
    option_values: dict[str, list[str]] = {}
    index = 0
    while index < len(docker_options):
        option = _normalize_read_only_option(docker_options[index])
        assert option in approved_options, f"unapproved Docker option: {option}"
        if option in {"--rm", "--read-only"}:
            option_values.setdefault(option, []).append("true")
            index += 1
            continue
        assert index + 1 < len(docker_options), f"missing value for {option}"
        option_values.setdefault(option, []).append(docker_options[index + 1])
        index += 2
    for flag_value in expected_policy_flags:
        flag, value = flag_value.split(" ", maxsplit=1)
        assert option_values.get(flag) == [value]
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
        "HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=", "FTP_PROXY=", "NO_PROXY=",
        "http_proxy=", "https_proxy=", "all_proxy=", "ftp_proxy=", "no_proxy=",
        "PYTHONPYCACHEPREFIX=/tmp/pycache", "XDG_CACHE_HOME=/tmp/cache",
    ]
    assert policy["ci"]["permissions"] == {"contents": "read"}
    assert policy["ci"]["allow_pull_request_target"] is False
    assert policy["ci"]["expose_repository_secrets_to_test_container"] is False


def test_read_only_true_option_is_normalized_but_false_is_rejected() -> None:
    assert _normalize_read_only_option("--read-only=true") == "--read-only"
    assert _normalize_read_only_option("--read-only=false") == "--read-only=false"


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
    entrypoint_index = argv.index("--entrypoint")
    assert argv[entrypoint_index + 1] == "python"
    assert argv[entrypoint_index + 2] == IMAGE_ID
    assert argv[-3:] == ["-m", "pytest", "-q"]
    assert argv.count("--read-only") == 1
    assert argv.count("--network") == 1
    assert "max-size=10m" in argv
    assert "max-file=3" in argv
    for proxy in ("HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=", "FTP_PROXY=", "NO_PROXY=", "http_proxy=", "https_proxy=", "all_proxy=", "ftp_proxy=", "no_proxy="):
        assert proxy in argv


def test_container_command_is_passed_as_distinct_argv_items() -> None:
    execution = load_execution_policy(POLICY_PATH)
    marker = "test; touch /tmp/should-not-run"
    argv = build_docker_command(
        IMAGE_ID, ["python", "-c", marker], execution,
        container_name=TEST_CONTAINER_NAME,
    )
    entrypoint_index = argv.index("--entrypoint")
    assert argv[entrypoint_index + 1] == "python"
    assert argv[entrypoint_index + 2] == IMAGE_ID
    assert argv[-2:] == ["-c", marker]


def test_container_command_allows_empty_non_executable_operands() -> None:
    execution = load_execution_policy(POLICY_PATH)
    argv = build_docker_command(
        IMAGE_ID, ["printf", "%s", ""], execution,
        container_name=TEST_CONTAINER_NAME,
    )
    entrypoint_index = argv.index("--entrypoint")
    assert argv[entrypoint_index + 1] == "printf"
    assert argv[entrypoint_index + 2] == IMAGE_ID
    assert argv[-2:] == ["%s", ""]


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


def test_resolve_docker_target_preserves_explicit_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-ci")
    monkeypatch.setenv("DOCKER_HOST", "tcp://ignored.example:2376")
    target = resolve_docker_target()
    assert target == DockerTarget("/usr/bin/docker", ("--context", "remote-ci"))


def test_resolve_docker_target_preserves_explicit_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker.example:2376")
    target = resolve_docker_target()
    assert target == DockerTarget("/usr/bin/docker", ("--host", "tcp://docker.example:2376"))


def test_resolve_docker_target_snapshots_default_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda argv, **kwargs: CompletedProcess(argv, 0, stdout="workstation\n", stderr=""),
    )
    assert resolve_docker_target() == DockerTarget(
        "/usr/bin/docker", ("--context", "workstation"),
    )


def test_resolve_image_id_rejects_daemon_without_pid_limit_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            argv, 0, stdout='["WARNING: No pids limit support"]', stderr="",
        )

    monkeypatch.setattr("code_test_lab.docker.subprocess.run", fake_run)
    with pytest.raises(ValueError, match="does not support enforcing PID limits"):
        resolve_image_id("local:test", DOCKER_TARGET)


def test_resolve_image_id_rejects_declared_volumes(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {"Id": IMAGE_ID, "Config": {"Volumes": {"/data": {}}}}
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda argv, **kwargs: _docker_info_or_metadata(argv, metadata),
    )
    with pytest.raises(ValueError, match="volumes"):
        resolve_image_id("local:test", DOCKER_TARGET)


def test_resolve_image_id_returns_immutable_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {"Id": IMAGE_ID, "Config": {"Volumes": None}}
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda argv, **kwargs: _docker_info_or_metadata(argv, metadata),
    )
    assert resolve_image_id("local:test", DOCKER_TARGET) == IMAGE_ID


def test_timeout_cleanup_uses_fixed_docker_rm_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("code_test_lab.docker.subprocess.run", fake_run)
    assert remove_container(TEST_CONTAINER_NAME, DOCKER_TARGET) is True
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
    sbom_step = next(
        step for step in job["steps"]
        if step.get("name") == "Generate SBOM from the image without executing it"
    )
    requirements_step = next(
        step for step in job["steps"]
        if step.get("name") == "Convert SBOM Python packages into pinned requirements data"
    )
    audit_step = next(
        step for step in job["steps"]
        if step.get("name") == "Audit image inventory with trusted pip-audit action"
    )
    assert build_step["id"] == "build-image"
    assert "sha256sum --check" in next(
        step["run"] for step in job["steps"]
        if step.get("name") == "Install trusted Syft image scanner"
    )
    assert "syft \"code-test-lab:${{ github.sha }}\"" in sbom_step["run"]
    assert "cyclonedx-json=" in sbom_step["run"]
    assert "json.load" in requirements_step["run"]
    assert "pkg:pypi/" in requirements_step["run"]
    assert "multiple versions" in requirements_step["run"]
    assert audit_step["uses"] == (
        "pypa/gh-action-pip-audit@1220774d901786e6f652ae159f7b6bc8fea6d266"
    )
    assert audit_step["with"]["disable-pip"] == "true"
    assert audit_step["with"]["no-deps"] == "true"
    assert audit_step["with"]["inputs"] == "${{ runner.temp }}/image-requirements.txt"
    assert not any("--entrypoint pip-audit" in run for run in run_steps)
    assert any("docker build" in run for run in run_steps)
    quality_step = next(
        step for step in job["steps"]
        if step.get("name") == "Run quality checks without network or credentials"
    )
    assert _is_single_fail_fast_docker_run(quality_step["run"])
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
    monkeypatch.setattr("code_test_lab.cli.resolve_docker_target", lambda: DOCKER_TARGET)
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda *args: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("code_test_lab.docker.subprocess.run", fake_run)
    assert main() == 124
    assert calls[0][0][0:3] == ["/usr/bin/docker", "run", "--rm"]
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
    monkeypatch.setattr("code_test_lab.cli.resolve_docker_target", lambda: DOCKER_TARGET)
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda *args: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", timeout_run)
    monkeypatch.setattr("code_test_lab.cli.remove_container", lambda name, target=None: False)
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
    monkeypatch.setattr("code_test_lab.cli.resolve_docker_target", lambda: DOCKER_TARGET)
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda *args: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", interrupt_run)
    monkeypatch.setattr("code_test_lab.cli.remove_container", lambda name, target=None: calls.append(name) or True)
    assert main() == 130
    assert len(calls) == 1


def test_workflow_credential_guard_detects_bracket_and_dot_contexts() -> None:
    assert _contains_untrusted_credential("${{ secrets['TOKEN'] }}")
    assert _contains_untrusted_credential("${{ secrets.TOKEN }}")
    assert _contains_untrusted_credential("${{ github.token }}")
    assert _contains_untrusted_credential("${{ github['token'] }}")
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
    scan_step = next(
        step for step in gitleaks_steps
        if step.get("name") == "Scan all commits using trusted base-branch rules"
    )
    scan = scan_step["run"]
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
    assert command_tokens[2] == "."
    assert 'cd "$trusted_dir"' in scan
    assert scan_step["env"]["TRUSTED_BASE_SHA"] == (
        "${{ github.event.pull_request.base.sha || github.sha }}"
    )
    assert scan_step["env"]["TRUSTED_HEAD_SHA"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    assert command_tokens[command_tokens.index("--config") + 1] == "$trusted_config"
    assert (
        command_tokens[command_tokens.index("--gitleaks-ignore-path") + 1]
        == "$trusted_ignore"
    )
    assert "--ignore-gitleaks-allow" in command_tokens
    assert "--redact" in command_tokens
    assert "--no-banner" in command_tokens
    assert (
        '--log-opts=--all --full-history --diff-filter=uxdbT --diff-merges=first-parent $trusted_head'
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
    def interrupt_inspection(*args: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(POLICY_PATH), "--image", "local:test",
    ])
    monkeypatch.setattr("code_test_lab.cli.resolve_docker_target", lambda: DOCKER_TARGET)
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", interrupt_inspection)
    monkeypatch.setattr(
        "code_test_lab.cli.remove_container",
        lambda name, target=None: pytest.fail("cleanup must not run before Docker launch"),
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
    assert "TRUSTED_HEAD_SHA" in scan_step["env"]
    assert "git worktree add" in scan_step["run"]
    assert "gitleaks git ." in scan_step["run"]
    assert '--config "$trusted_config"' in scan_step["run"]
    assert '--gitleaks-ignore-path "$trusted_ignore"' in scan_step["run"]
    assert "--ignore-gitleaks-allow" in scan_step["run"]
    assert "--diff-filter=uxdbT" in scan_step["run"]
    actionlint_job = workflow["jobs"]["actionlint"]
    lint_step = next(step for step in actionlint_job["steps"] if step.get("name", "").startswith("Lint workflows"))
    assert "actionlint -config-file \"$RUNNER_TEMP/actionlint-empty.yml\" -color" in lint_step["run"]

    zizmor_job = workflow["jobs"]["zizmor"]
    audit = next(
        step["run"] for step in zizmor_job["steps"]
        if step.get("name") == "Audit workflow security"
    )
    assert "--no-config" in audit
    assert "--no-ignores" in audit
    assert "--collect=all" in audit
    assert "--strict-collection" in audit
    assert "${{ runner.temp }}/zizmor-venv/bin/zizmor" in audit


def test_readme_warns_about_untrusted_docker_build_context() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "only for a trusted checkout" in readme
    assert "disposable builder with no proxy credentials" in readme
    assert "exit code `125`" in readme
    assert "original Docker daemon" in readme


def test_policy_rejects_duplicate_keys_inside_yaml_merge_operands(tmp_path: Path) -> None:
    invalid = tmp_path / "duplicate-merge.yml"
    text = POLICY_PATH.read_text(encoding="utf-8").replace(
        '  memory: "2g"',
        '  <<: {memory: "512m", memory: "2g"}',
        1,
    )
    invalid.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError, match="duplicate key"):
        load_execution_policy(invalid)


def test_policy_rejects_signaling_nan_cpu_value(tmp_path: Path) -> None:
    invalid = tmp_path / "signaling-nan.yml"
    text = POLICY_PATH.read_text(encoding="utf-8").replace("cpus: 2", "cpus: sNaN")
    invalid.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError, match="cpus"):
        load_execution_policy(invalid)



def test_cleanup_treats_already_removed_container_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda argv, **kwargs: CompletedProcess(
            argv, 1, stdout="", stderr=f"Error response from daemon: No such container: {TEST_CONTAINER_NAME}",
        ),
    )
    assert remove_container(TEST_CONTAINER_NAME, DOCKER_TARGET) is True


def test_main_cleans_up_after_docker_client_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    cleanup_calls: list[str] = []

    def failed_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(argv, 125, stdout="", stderr="Docker attach stream failed")

    monkeypatch.setattr("sys.argv", [
        "code-test-lab", "--policy", str(POLICY_PATH), "--image", "local:test",
        "--", "python", "-m", "pytest",
    ])
    monkeypatch.setattr("code_test_lab.cli.resolve_image_id", lambda _: IMAGE_ID)
    monkeypatch.setattr("code_test_lab.cli.subprocess.run", failed_run)
    monkeypatch.setattr(
        "code_test_lab.cli.remove_container",
        lambda name, target=None: cleanup_calls.append(name) or True,
    )
    assert main() == 125
    assert len(cleanup_calls) == 1
    assert capsys.readouterr().err == ""



def test_docker_command_overrides_image_entrypoint_and_preserves_empty_operands() -> None:
    command = build_docker_command(
        IMAGE_ID, ["printf", "%s", ""], load_execution_policy(POLICY_PATH),
        container_name=TEST_CONTAINER_NAME,
    )
    assert command[command.index("--entrypoint") + 1] == "printf"
    assert command[-3:] == [IMAGE_ID, "%s", ""]


@pytest.mark.parametrize("metadata", [None, [], {"Config": None}, {"Config": []}])
def test_resolve_image_id_rejects_invalid_metadata_shape(
    monkeypatch: pytest.MonkeyPatch, metadata: object,
) -> None:
    monkeypatch.setattr("code_test_lab.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "code_test_lab.docker.subprocess.run",
        lambda *args, **kwargs: _docker_info_or_metadata(args[0], metadata),
    )
    with pytest.raises((ValueError, TypeError), match="invalid object|missing Config"):
        resolve_image_id("local:test", DOCKER_TARGET)



def test_policy_rejects_repeated_yaml_merge_keys(tmp_path: Path) -> None:
    invalid = tmp_path / "duplicate-merge-key.yml"
    text = POLICY_PATH.read_text(encoding="utf-8").replace(
        "execution:\n",
        "execution:\n  <<: {memory: \"512m\"}\n  <<: {tmpfs_size: \"128m\"}\n",
        1,
    )
    invalid.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError, match="duplicate merge key"):
        load_execution_policy(invalid)


def test_quality_command_guard_rejects_exit_status_masking() -> None:
    assert _is_single_fail_fast_docker_run("docker run image pytest -q")
    assert not _is_single_fail_fast_docker_run("docker run image pytest -q || true")


def test_policy_rejects_excessive_yaml_alias_expansion(tmp_path: Path) -> None:
    invalid = tmp_path / "alias-expansion.yml"
    policy = POLICY_PATH.read_text(encoding="utf-8")
    aliases = "base: &base {a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7, h: 8, i: 9, j: 10}\n"
    for index in range(12):
        source = "base" if index == 0 else f"layer{index - 1}"
        aliases += f"layer{index}: &layer{index} {{<<: [*{source}, *{source}]}}\n"
    aliases += "execution: {<<: *layer11}\n"
    invalid.write_text(aliases + policy, encoding="utf-8")
    with pytest.raises(PolicyError, match="expansion"):
        load_execution_policy(invalid)


def test_policy_rejects_oversized_yaml_file(tmp_path: Path) -> None:
    invalid = tmp_path / "oversized.yml"
    invalid.write_text(" " * (65 * 1024), encoding="utf-8")
    with pytest.raises(PolicyError, match="exceeds"):
        load_execution_policy(invalid)
