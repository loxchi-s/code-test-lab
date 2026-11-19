"""Compatibility entry point for policy-driven Docker execution."""

from .cli import main
from .docker import build_docker_command, remove_container
from .policy import PolicyError, load_execution_policy

__all__ = [
    "PolicyError",
    "build_docker_command",
    "load_execution_policy",
    "main",
    "remove_container",
]

# Keep the established command stable: python -m code_test_lab.isolation
if __name__ == "__main__":
    raise SystemExit(main())
