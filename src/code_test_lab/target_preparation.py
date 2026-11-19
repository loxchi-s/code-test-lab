"""Prepare an external Python source tree and derive safe SCM version overrides."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

_MAX_PYPROJECT_BYTES = 1024 * 1024
_COMMIT_SHA_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_DISTRIBUTION_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")


def _read_project_metadata(source: Path) -> dict[str, Any]:
    """Read bounded, non-executable PEP 621 metadata from the target project."""
    pyproject = source / "pyproject.toml"
    if pyproject.is_symlink():
        raise ValueError("target pyproject.toml must not be a symbolic link")
    try:
        with pyproject.open("rb") as stream:
            contents = stream.read(_MAX_PYPROJECT_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(contents) > _MAX_PYPROJECT_BYTES:
        raise ValueError("target pyproject.toml exceeds the 1 MiB metadata limit")
    document = tomllib.loads(contents.decode("utf-8"))
    if not isinstance(document, dict):
        raise TypeError("target pyproject.toml must contain a TOML document")
    return document


def _scm_version_overrides(document: dict[str, Any], commit_sha: str) -> dict[str, str]:
    """Create package-scoped deterministic versions for setuptools-scm projects."""
    project = document.get("project")
    build_system = document.get("build-system")
    if not isinstance(project, dict) or not isinstance(build_system, dict):
        return {}

    dynamic = project.get("dynamic", [])
    requirements = build_system.get("requires", [])
    if not isinstance(dynamic, list) or "version" not in dynamic:
        return {}
    if not isinstance(requirements, list) or not all(
        isinstance(requirement, str) for requirement in requirements
    ):
        raise ValueError("target build-system.requires must be a list of strings")

    scm_requirement = re.compile(
        r"^\s*setuptools[-_]scm(?=\s*(?:\[|[<>=!~;\s]|$))",
        re.IGNORECASE,
    )
    requirement_names = {
        re.sub(r"[-_.]+", "-", match.group(1)).lower()
        for requirement in requirements
        if (match := re.match(
            r"^\\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)",
            requirement,
        ))
    }
    if not requirement_names.intersection({"setuptools-scm", "hatch-vcs"}):
        return {}

    name = project.get("name")
    if not isinstance(name, str) or not _DISTRIBUTION_NAME_PATTERN.fullmatch(name):
        raise ValueError("dynamic setuptools-scm projects must declare a valid project.name")

    suffix = re.sub(r"[-_.]+", "_", name).upper()
    version = f"0.0.dev0+g{commit_sha[:12].lower()}"
    return {
        f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_{suffix}": version,
        f"VCS_VERSIONING_PRETEND_VERSION_FOR_{suffix}": version,
    }


def prepare_target_source(source: Path, destination: Path, commit_sha: str) -> dict[str, str]:
    """Copy source without Git internals and return deterministic SCM environment values."""
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("target commit must be a full 40- or 64-character hexadecimal SHA")
    if source.is_symlink() or not source.is_dir():
        raise ValueError("target source must be a real directory")

    document = _read_project_metadata(source)
    overrides = _scm_version_overrides(document, commit_sha)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    return overrides


def main() -> int:
    """Copy target source and write validated environment assignments for the shell."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()

    try:
        overrides = prepare_target_source(args.source, args.destination, args.commit_sha)
        args.env_file.write_text(
            "".join(f"{name}={value}\n" for name, value in sorted(overrides.items())),
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        print(f"Target preparation metadata error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
