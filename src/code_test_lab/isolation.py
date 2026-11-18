"""Compatibility entry point for policy-driven Docker execution."""

from .cli import main
from .docker import build_docker_command, remove_container
from .policy import PolicyError, load_execution_policy

# Keep the established command stable: python -m code_test_lab.isolation
if __name__ == "__main__":
    raise SystemExit(main())
