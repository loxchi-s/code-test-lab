"""Command-line entry point for policy-driven container execution."""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - Docker is invoked with an argv list, never through a shell.
import sys
import uuid
from pathlib import Path

from .docker import build_docker_command, remove_container, resolve_image_id
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
    execution: dict[str, object] | None = None
    try:
        execution = load_execution_policy(args.policy)
        image_id = resolve_image_id(args.image)
        container_name = f"code-test-lab-{uuid.uuid4().hex}"
        docker_argv = build_docker_command(
            image_id, command, execution, container_name=container_name,
        )
        completed = subprocess.run(
            docker_argv, check=False, shell=False,
            timeout=float(execution["timeout_minutes"]) * 60,
        )
        return completed.returncode
    except PolicyError as exc:
        print(f"Isolation policy error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Invalid execution request: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        cleanup_ok = container_name is not None and remove_container(container_name)
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
        cleanup_ok = container_name is not None and remove_container(container_name)
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
