"""Prepare external Python projects inside the restricted dependency container."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_PYPROJECT_BYTES = 1024 * 1024
_COMMIT_SHA_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_DISTRIBUTION_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_SEED_VERSION_PATTERN = re.compile(r"[0-9A-Za-z!][0-9A-Za-z.!+_-]*\Z")


@dataclass(frozen=True)
class TargetProjectMetadata:
    """Validated PEP 621 metadata required to prepare one target project."""

    distribution_name: str
    requirements: tuple[str, ...]
    uses_vcs_versioning: bool


def _canonicalize_distribution_name(name: str) -> str:
    """Normalize a distribution name according to PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    """Extract and normalize the leading distribution name from a PEP 508 requirement."""
    match = _REQUIREMENT_NAME_PATTERN.match(requirement)
    if match is None:
        raise ValueError("target dependencies must be valid PEP 508 requirement strings")
    return _canonicalize_distribution_name(match.group(1))


def _validated_requirements(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"target project.{field} must be a list of requirement strings")

    requirements: list[str] = []
    for raw_requirement in value:
        requirement = raw_requirement.strip()
        if (
            not requirement
            or "\n" in requirement
            or "\r" in requirement
            or requirement.startswith(("-", "#"))
        ):
            raise ValueError(f"target project.{field} contains an unsafe requirement line")
        _requirement_name(requirement)
        requirements.append(requirement)
    return tuple(requirements)


def _read_project_metadata(source: Path) -> dict[str, Any]:
    """Read bounded, non-executable PEP 621 metadata from the target project."""
    pyproject = source / "pyproject.toml"
    if pyproject.is_symlink():
        raise ValueError("target pyproject.toml must not be a symbolic link")
    try:
        with pyproject.open("rb") as stream:
            contents = stream.read(_MAX_PYPROJECT_BYTES + 1)
    except FileNotFoundError as exc:
        raise ValueError("target project must include pyproject.toml") from exc
    if len(contents) > _MAX_PYPROJECT_BYTES:
        raise ValueError("target pyproject.toml exceeds the 1 MiB metadata limit")
    document = tomllib.loads(contents.decode("utf-8"))
    if not isinstance(document, dict):
        raise TypeError("target pyproject.toml must contain a TOML document")
    return document


def read_target_project(source: Path) -> TargetProjectMetadata:
    """Validate static PEP 621 dependencies and identify supported VCS version providers."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError("target source must be a real directory")

    document = _read_project_metadata(source)
    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError("target pyproject.toml must declare a [project] table")

    name = project.get("name")
    if not isinstance(name, str) or not _DISTRIBUTION_NAME_PATTERN.fullmatch(name):
        raise ValueError("target project must declare a valid project.name")

    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or not all(isinstance(item, str) for item in dynamic):
        raise ValueError("target project.dynamic must be a list of strings")
    if {"dependencies", "optional-dependencies"}.intersection(dynamic):
        raise ValueError(
            "dynamic project dependencies are unsupported; use static PEP 621 dependencies"
        )

    dependencies = _validated_requirements(project.get("dependencies", []), "dependencies")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("target project.optional-dependencies must be a table")
    dev_dependencies = _validated_requirements(optional.get("dev"), "optional-dependencies.dev")
    if _canonicalize_distribution_name(name) != "pytest" and not any(
        _requirement_name(item) == "pytest" for item in dev_dependencies
    ):
        raise ValueError("target dev extra must include pytest unless the target distribution is pytest")

    build_system = document.get("build-system", {})
    if not isinstance(build_system, dict):
        raise ValueError("target build-system must be a table")
    build_requirements = _validated_requirements(
        build_system.get("requires", []), "build-system.requires"
    )
    build_requirement_names = {_requirement_name(item) for item in build_requirements}

    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        raise ValueError("target tool configuration must be a table")
    hatch = tool.get("hatch", {})
    hatch_version = hatch.get("version", {}) if isinstance(hatch, dict) else {}
    if (
        isinstance(hatch_version, dict)
        and hatch_version.get("source") == "vcs"
        and "hatch-vcs" not in build_requirement_names
    ):
        raise ValueError("VCS-based Hatch versioning requires hatch-vcs in build-system.requires")
    if "setuptools_scm" in tool and "setuptools-scm" not in build_requirement_names:
        raise ValueError("setuptools-scm configuration requires setuptools-scm in build-system.requires")

    uses_vcs_versioning = "version" in dynamic and bool(
        build_requirement_names.intersection({"setuptools-scm", "hatch-vcs"})
    )
    requirements = tuple(dict.fromkeys((*dependencies, *dev_dependencies)))
    return TargetProjectMetadata(
        distribution_name=name,
        requirements=requirements,
        uses_vcs_versioning=uses_vcs_versioning,
    )


def prepare_target_source(
    source: Path, destination: Path, commit_sha: str
) -> TargetProjectMetadata:
    """Copy source without Git internals and return validated target metadata."""
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("target commit must be a full 40- or 64-character hexadecimal SHA")
    metadata = read_target_project(source)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    return metadata


def scm_version_overrides(
    metadata: TargetProjectMetadata, commit_sha: str, seed_version: str = ""
) -> dict[str, str]:
    """Create a commit-scoped VCS version using the resolver-compatible seed version."""
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("target commit must be a full 40- or 64-character hexadecimal SHA")
    if not metadata.uses_vcs_versioning:
        return {}

    base_version = seed_version.strip() or "0.0.dev0"
    if (
        not _SEED_VERSION_PATTERN.fullmatch(base_version)
        or base_version.count("+") > 1
    ):
        raise ValueError("seed distribution version must be a valid version string")

    commit_suffix = f"sandbox.g{commit_sha[:12].lower()}"
    if "+" in base_version:
        release, local = base_version.split("+", 1)
        if not release or not local:
            raise ValueError("seed distribution version must be a valid version string")
        version = f"{release}+{local}.{commit_suffix}"
    else:
        version = f"{base_version}+{commit_suffix}"

    suffix = re.sub(r"[-_.]+", "_", metadata.distribution_name).upper()
    return {
        f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_{suffix}": version,
        f"VCS_VERSIONING_PRETEND_VERSION_FOR_{suffix}": version,
    }


def main() -> int:
    """Copy target source and write dependency/version metadata for the shell adapter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--requirements-file", type=Path)
    parser.add_argument("--distribution-file", type=Path)
    parser.add_argument("--seed-version", default="")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    try:
        metadata = (
            prepare_target_source(args.source, args.destination, args.commit_sha)
            if args.destination is not None
            else read_target_project(args.source)
        )
        if args.requirements_file is not None:
            args.requirements_file.write_text(
                "".join(f"{requirement}\n" for requirement in metadata.requirements),
                encoding="utf-8",
            )
        if args.distribution_file is not None:
            args.distribution_file.write_text(f"{metadata.distribution_name}\n", encoding="utf-8")
        if args.env_file is not None:
            overrides = scm_version_overrides(metadata, args.commit_sha, args.seed_version)
            args.env_file.write_text(
                "".join(f"{key}={value}\n" for key, value in sorted(overrides.items())),
                encoding="utf-8",
            )
    except (OSError, ValueError, TypeError) as exc:
        print(f"Target preparation metadata error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
