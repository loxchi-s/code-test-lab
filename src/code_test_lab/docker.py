"""Construct and execute Docker commands under the validated policy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - Docker is invoked with argv lists, never through a shell.
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

_CONTAINER_NAME_PATTERN = re.compile(r"code-test-lab-[a-f0-9]{32}")
_IMAGE_ID_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class DockerTarget:
    """Immutable Docker CLI target shared by inspect, launch, and cleanup."""

    executable: str
    global_args: tuple[str, ...] = ()


def resolve_docker_target() -> DockerTarget:
    """Pin the effective Docker context or host for one complete execution."""
    docker_executable = shutil.which("docker")
    if docker_executable is None:
        raise FileNotFoundError("docker executable not found")

    context = os.environ.get("DOCKER_CONTEXT")
    host = os.environ.get("DOCKER_HOST")
    if context:
        return DockerTarget(docker_executable, ("--context", context))
    if host:
        return DockerTarget(docker_executable, ("--host", host))

    try:
        result = subprocess.run(  # nosec B603 - fixed Docker subcommand, argv only, and shell=False.
            [docker_executable, "context", "show"],
            check=False, shell=False, timeout=10, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Docker context resolution exceeded 10 seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise ValueError(f"Docker context resolution failed: {detail or result.returncode}")
    context = result.stdout.strip()
    if not context or len(context) > 128 or any(char.isspace() for char in context):
        raise ValueError("Docker context resolution returned an invalid context name")
    return DockerTarget(docker_executable, ("--context", context))


def _require_pid_limit_support(target: DockerTarget) -> None:
    """Fail closed when the selected daemon reports that PID limits are unsupported."""
    try:
        result = subprocess.run(  # nosec B603 - Docker executable and subcommand are fixed; target is argv-only.
            [
                target.executable, *target.global_args,
                "info", "--format", "{{json .Warnings}}",
            ],
            check=False, shell=False, timeout=10, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Docker daemon capability inspection exceeded 10 seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise ValueError(
            f"Docker daemon capability inspection failed: {detail or result.returncode}"
        )
    try:
        warnings = json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise ValueError("Docker daemon returned invalid capability metadata") from exc
    if warnings is None:
        warnings = []
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("Docker daemon returned an invalid warnings list")
    if any("pids limit" in warning.casefold() for warning in warnings):
        raise ValueError("Selected Docker daemon does not support enforcing PID limits")


def resolve_image_id(image: str, docker_target: DockerTarget | None = None) -> str:
    """Resolve a local image to an immutable ID and reject image-declared volumes."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", image):
        raise ValueError("Docker image reference contains unsupported characters")
    target = docker_target or resolve_docker_target()
    _require_pid_limit_support(target)
    try:
        result = subprocess.run(  # nosec B603 - executable and inspect argv are fixed; image is validated.
            [
                target.executable, *target.global_args,
                "image", "inspect", "--format", "{{json .}}", image,
            ],
            check=False, shell=False, timeout=10, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Docker image inspection exceeded 10 seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise ValueError(f"Docker image inspection failed: {detail or result.returncode}")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Docker image inspection returned invalid metadata") from exc
    if not isinstance(metadata, dict):
        raise TypeError("Docker image inspection returned an invalid object")
    config = metadata.get("Config")
    if not isinstance(config, dict):
        raise TypeError("Docker image metadata is missing Config")
    volumes = config.get("Volumes")
    if volumes:
        raise ValueError("Docker images declaring volumes are not permitted")
    image_id = metadata.get("Id")
    if not isinstance(image_id, str) or not _IMAGE_ID_PATTERN.fullmatch(image_id):
        raise ValueError("Docker image inspection did not return an immutable image ID")
    return image_id


def build_docker_command(
    image: str,
    command: list[str],
    execution: dict[str, Any],
    *,
    container_name: str,
    docker_target: DockerTarget | None = None,
) -> list[str]:
    """Build a Docker argv vector; never interpolate commands through a shell."""
    if not _IMAGE_ID_PATTERN.fullmatch(image):
        raise ValueError("Docker image must be an immutable sha256 image ID")
    if not command or not command[0]:
        raise ValueError("Container command must contain a non-empty executable")
    if not _CONTAINER_NAME_PATTERN.fullmatch(container_name):
        raise ValueError("Container name must be a generated code-test-lab identifier")

    target = docker_target or DockerTarget("docker")
    tmpfs_target = str(PurePosixPath("/", "tmp"))
    return [
        target.executable, *target.global_args,
        "run", "--rm", "--name", container_name,
        "--network", "none", "--read-only",
        "--tmpfs", f"{tmpfs_target}:rw,noexec,nosuid,size={execution['tmpfs_size']}",
        "--memory", str(execution["memory"]),
        "--memory-swap", str(execution["memory"]),
        "--cpus", str(execution["cpus"]),
        "--pids-limit", str(execution["pids_limit"]),
        "--log-driver", "json-file",
        "--log-opt", "max-size=10m",
        "--log-opt", "max-file=3",
        "--env", "HTTP_PROXY=",
        "--env", "HTTPS_PROXY=",
        "--env", "ALL_PROXY=",
        "--env", "FTP_PROXY=",
        "--env", "NO_PROXY=",
        "--env", "http_proxy=",
        "--env", "https_proxy=",
        "--env", "all_proxy=",
        "--env", "ftp_proxy=",
        "--env", "no_proxy=",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", str(execution["user"]),
        "--entrypoint", command[0],
        image,
        *command[1:],
    ]


def remove_container(
    container_name: str, docker_target: DockerTarget | None = None,
) -> bool:
    """Best-effort bounded cleanup for a container whose Docker client stopped."""
    if not _CONTAINER_NAME_PATTERN.fullmatch(container_name):
        raise ValueError("Container name must be a generated code-test-lab identifier")
    try:
        target = docker_target or resolve_docker_target()
        result = subprocess.run(  # nosec B603 - fixed cleanup argv and validated generated container name.
            [
                target.executable, *target.global_args,
                "rm", "--force", container_name,
            ],
            check=False, shell=False, timeout=10, capture_output=True, text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"Cleanup failed: unable to remove container {container_name}: {exc}",
            file=sys.stderr,
        )
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if "No such container" in detail or "No such object" in detail:
            return True
        print(
            f"Cleanup failed for {container_name} (exit {result.returncode}): "
            f"{detail or 'Docker returned a non-zero status'}; container may still be running.",
            file=sys.stderr,
        )
        return False
    return True
