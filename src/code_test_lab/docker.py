"""Construct and execute Docker commands under the validated policy."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - Docker is invoked with argv lists, never through a shell.
import sys
from pathlib import PurePosixPath
from typing import Any

_CONTAINER_NAME_PATTERN = re.compile(r"code-test-lab-[a-f0-9]{32}")


def build_docker_command(
    image: str,
    command: list[str],
    execution: dict[str, Any],
    *,
    container_name: str,
) -> list[str]:
    """Build a Docker argv vector; never interpolate commands through a shell."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", image):
        raise ValueError("Docker image reference contains unsupported characters")
    if not command or any(not item for item in command):
        raise ValueError("Container command must contain non-empty arguments")
    if not _CONTAINER_NAME_PATTERN.fullmatch(container_name):
        raise ValueError("Container name must be a generated code-test-lab identifier")

    tmpfs_target = str(PurePosixPath("/", "tmp"))
    return [
        "docker", "run", "--rm", "--name", container_name,
        "--network", "none", "--read-only",
        "--tmpfs", f"{tmpfs_target}:rw,noexec,nosuid,size={execution['tmpfs_size']}",
        "--memory", str(execution["memory"]),
        "--memory-swap", str(execution["memory"]),
        "--cpus", str(execution["cpus"]),
        "--pids-limit", str(execution["pids_limit"]),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", str(execution["user"]),
        image,
        *command,
    ]


def remove_container(container_name: str) -> bool:
    """Best-effort cleanup for a container whose Docker client timed out."""
    if not _CONTAINER_NAME_PATTERN.fullmatch(container_name):
        raise ValueError("Container name must be a generated code-test-lab identifier")
    docker_executable = shutil.which("docker")
    if docker_executable is None:
        print("Warning: Docker executable not found; timed-out container may remain.", file=sys.stderr)
        return False
    try:
        result = subprocess.run(  # nosec B603 - fixed Docker cleanup argv, no shell.
            [docker_executable, "rm", "--force", container_name],
            check=False, shell=False, timeout=10, capture_output=True, text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Warning: unable to clean up timed-out container: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        print(
            f"Warning: Docker container cleanup returned {result.returncode}: {detail}",
            file=sys.stderr,
        )
        return False
    return True
