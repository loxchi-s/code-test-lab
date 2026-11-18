"""Command-line entry point for policy-driven container execution."""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - Docker is invoked with an argv list, never through a shell.
import sys
import uuid
from pathlib import Path
from typing import Any

from .docker import (
    build_docker_command,
    remove_container,
    resolve_docker_target,
    resolve_image_id,
)
from .policy import PolicyError, load_execution_policy


def main() -> int:
    """Validate policy, run the requested container command, and clean up on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("config/test-policy.yml"))
    parser.add_argument("--image", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    container_name: str | None = None
    container_started = False
    execution: dict[str, Any] | None = None
    docker_target = None
    try:
        execution = load_execution_policy(args.policy)
        docker_target = resolve_docker_target()
        image_id = resolve_image_id(args.image, docker_target)
        container_name = f"code-test-lab-{uuid.uuid4().hex}"
        docker_argv = build_docker_command(
            image_id, command, execution, container_name=container_name,
            docker_target=docker_target,
        )
        container_started = True
        completed = subprocess.run(  # nosec B603 - validated argv; no shell; policy bounds the container.
            docker_argv, check=False, shell=False,
            timeout=float(execution["timeout_minutes"]) * 60,
        )
        if (
            completed.returncode != 0
            and container_name is not None
            and not remove_container(container_name, docker_target)
        ):
            print(
                "Docker client failed and cleanup could not be confirmed; "
                "the container may still be running.",
                file=sys.stderr,
            )
        return completed.returncode
    except PolicyError as exc:
        print(f"Isolation policy error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, TypeError) as exc:
        print(f"Invalid execution request: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        cleanup_ok = container_name is not None and remove_container(container_name, docker_target)
        if not cleanup_ok:
            print(
                "Execution timed out and cleanup could not be confirmed; "
                "the container may still be running.",
                file=sys.stderr,
            )
            return 125
        timeout_minutes = execution["timeout_minutes"] if execution else "configured"
        print(f"Container command exceeded {timeout_minutes} minutes", file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        if not container_started:
            return 130
        cleanup_ok = container_name is not None and remove_container(container_name, docker_target)
        if not cleanup_ok:
            print(
                "Execution interrupted and cleanup could not be confirmed; "
                "the container may still be running.",
                file=sys.stderr,
            )
            return 125
        return 130
    except FileNotFoundError as exc:
        print(f"Execution dependency not found: {exc}", file=sys.stderr)
        return 127
