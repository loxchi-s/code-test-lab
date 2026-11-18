"""Load the repository isolation policy and execute Docker commands from it."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_REQUIRED_EXECUTION_KEYS = {
    "network",
    "filesystem",
    "user",
    "memory",
    "cpus",
    "pids_limit",
    "tmpfs_size",
    "timeout_minutes",
    "drop_capabilities",
    "no_new_privileges",
}


class PolicyError(ValueError):
    """Raised when the isolation policy is invalid or unsafe."""


def load_execution_policy(path: Path) -> dict[str, Any]:
    """Load and validate the execution policy without side effects."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"Unable to load policy: {exc}") from exc

    if (
        not isinstance(document, dict)
        or isinstance(document.get("version"), bool)
        or document.get("version") != 1
    ):
        raise PolicyError("Policy must be a mapping with version: 1")

    execution = document.get("execution")
    if not isinstance(execution, dict):
        raise PolicyError("Policy execution section must be a mapping")

    missing = _REQUIRED_EXECUTION_KEYS - execution.keys()
    if missing:
        raise PolicyError(f"Policy is missing execution settings: {sorted(missing)}")

    if execution["network"] != "disabled":
        raise PolicyError("Only network: disabled is permitted")
    if execution["filesystem"] != "read-only":
        raise PolicyError("Only filesystem: read-only is permitted")
    if not isinstance(execution["user"], str) or not re.fullmatch(
        r"\d+:\d+", execution["user"]
    ):
        raise PolicyError("Execution user must be a numeric UID:GID pair")
    if not isinstance(execution["memory"], str) or not re.fullmatch(
        r"\d+[mMgG]", execution["memory"]
    ):
        raise PolicyError("Execution memory must be a positive Docker memory value")

    for key in ("cpus", "pids_limit", "timeout_minutes"):
        value = execution[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise PolicyError(f"Execution setting {key!r} must be positive")

    if not isinstance(execution["tmpfs_size"], str) or not re.fullmatch(
        r"\d+[mMgG]", execution["tmpfs_size"]
    ):
        raise PolicyError("Execution tmpfs_size must be a positive size")
    if execution["drop_capabilities"] != "all":
        raise PolicyError("All Linux capabilities must be dropped")
    if execution["no_new_privileges"] is not True:
        raise PolicyError("no_new_privileges must be true")

    ci = document.get("ci")
    if not isinstance(ci, dict):
        raise PolicyError("Policy ci section must be a mapping")
    if ci.get("permissions") != {"contents": "read"}:
        raise PolicyError("CI permissions must be limited to contents: read")
    if ci.get("allow_pull_request_target") is not False:
        raise PolicyError("pull_request_target must be disabled")
    if ci.get("expose_repository_secrets_to_test_container") is not False:
        raise PolicyError("Repository secrets must not be exposed to test containers")

    return execution


def build_docker_command(
    image: str, command: list[str], execution: dict[str, Any]
) -> list[str]:
    """Build an argv vector; never interpolate commands through a shell."""
    if not image.strip():
        raise ValueError("Docker image must not be empty")
    if not command or any(not item for item in command):
        raise ValueError("Container command must contain non-empty arguments")

    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,size={execution['tmpfs_size']}",
        "--memory",
        str(execution["memory"]),
        "--cpus",
        str(execution["cpus"]),
        "--pids-limit",
        str(execution["pids_limit"]),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        str(execution["user"]),
        image,
        *command,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("config/test-policy.yml"))
    parser.add_argument("--image", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        execution = load_execution_policy(args.policy)
        docker_argv = build_docker_command(args.image, command, execution)
        completed = subprocess.run(
            docker_argv,
            check=False,
            timeout=float(execution["timeout_minutes"]) * 60,
        )
        return completed.returncode
    except PolicyError as exc:
        print(f"Isolation policy error: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(
            f"Container command exceeded {execution['timeout_minutes']} minutes",
            file=sys.stderr,
        )
        return 124
    except FileNotFoundError as exc:
        print(f"Execution dependency not found: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
