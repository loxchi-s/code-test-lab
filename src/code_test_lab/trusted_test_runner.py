""""Run target tests with the harness-owned Python interpreter."""

from __future__ import annotations

import argparse
import importlib.abc
import importlib.machinery
import os
import sys
import sysconfig
import tempfile
import types
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from packaging.version import InvalidVersion, Version


def configure_target_import_paths(
    source: Path, venv: Path, *, target_pytest: bool = False
) -> Path:
    """Expose target source and dependencies without processing untrusted .pth files."""
    source = source.resolve(strict=True)
    venv = venv.resolve(strict=True)
    if not source.is_dir() or not venv.is_dir():
        raise ValueError("target source and prepared virtual environment must be directories")

    site_packages = sorted((venv / "lib").glob("python*/site-packages"))
    if len(site_packages) != 1 or not site_packages[0].is_dir():
        raise ValueError("prepared virtual environment must contain exactly one site-packages directory")

    target_paths = [
        str(path) for path in (source / "src", source) if path.is_dir()
    ]
    # The pytest source tree uses a src layout. Adding its repository root to
    # sys.path makes documentation examples importable as top-level modules;
    # nested pytester runs can then collide with generated modules of the same
    # name. The src directory is sufficient for pytest and _pytest imports.
    if target_pytest and (source / "src").is_dir():
        target_paths = [str(source / "src")]
    target_paths.append(str(site_packages[0]))

    # PYTHONPATH is inherited by ordinary Python child processes. Python does not
    # process .pth files from PYTHONPATH entries, unlike site.addsitedir().
    os.environ["PYTHONPATH"] = os.pathsep.join(target_paths)

    if target_pytest:
        trusted_site = Path(sysconfig.get_paths()["purelib"]).resolve()
        trusted_index = next(
            (
                index
                for index, entry in enumerate(sys.path)
                if Path(entry).resolve() == trusted_site
            ),
            None,
        )
        if trusted_index is None:
            raise ValueError("trusted interpreter site-packages directory is not on sys.path")
        sys.path[trusted_index:trusted_index] = target_paths
    else:
        # Import trusted pytest before adding target-controlled paths.
        sys.path.extend(target_paths[:-1])
        sys.path.append(target_paths[-1])

    return site_packages[0]


def _install_pytest_version(version: Version) -> None:
    """Install validated pytest version metadata into the current interpreter."""
    import _pytest

    version_tuple = tuple(version.release)
    version_module = types.ModuleType("_pytest._version")
    dynamic_version_module = cast(Any, version_module)
    dynamic_version_module.version = str(version)
    dynamic_version_module.version_tuple = version_tuple
    sys.modules["_pytest._version"] = version_module
    _pytest._version = version_module
    _pytest.__version__ = str(version)
    _pytest.version_tuple = version_tuple


def configure_target_pytest_version(site_packages: Path) -> None:
    """Apply the prepared distribution version when pytest's generated file is absent."""
    matches = [
        distribution
        for distribution in metadata.distributions(path=[str(site_packages)])
        if (distribution.metadata.get("Name") or "").lower() == "pytest"
    ]
    if len(matches) != 1:
        raise ValueError(
            "prepared pytest environment must contain exactly one pytest distribution metadata record"
        )

    try:
        version = Version(matches[0].version)
    except InvalidVersion as exc:
        raise ValueError("prepared pytest distribution has invalid version metadata") from exc

    # The source checkout omits setuptools-scm's generated _pytest/_version.py.
    # Restore validated distribution metadata in this process and export only
    # that validated version for the trusted child-process bootstrap.
    _install_pytest_version(version)
    os.environ["TARGET_PYTEST_VERSION"] = str(version)


class _PytestVersionLoader(importlib.abc.Loader):
    """Provide generated pytest version metadata without importing _pytest early."""

    def __init__(self, version: Version) -> None:
        self.version = version

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        dynamic_module = cast(Any, module)
        dynamic_module.version = str(self.version)
        dynamic_module.version_tuple = tuple(self.version.release)


class _PytestVersionFinder(importlib.abc.MetaPathFinder):
    """Supply the setuptools-scm generated module when target pytest imports it."""

    def __init__(self, version: Version) -> None:
        self.loader = _PytestVersionLoader(version)

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "_pytest._version":
            return None
        return importlib.machinery.ModuleSpec(fullname, self.loader)


def configure_subprocess_pytest_version() -> None:
    """Bootstrap target pytest metadata lazily in child interpreters via sitecustomize."""
    version_text = os.environ.get("TARGET_PYTEST_VERSION")
    if not version_text:
        return
    try:
        version = Version(version_text)
    except InvalidVersion as exc:
        raise RuntimeError("trusted pytest version environment value is invalid") from exc

    # Do not import _pytest from sitecustomize: that runs before pytest's normal
    # startup and can change plugin initialization order. Resolve its generated
    # version module only when target pytest imports it.
    sys.meta_path[:] = [
        finder for finder in sys.meta_path if not isinstance(finder, _PytestVersionFinder)
    ]
    sys.meta_path.insert(0, _PytestVersionFinder(version))


def main(argv: list[str] | None = None) -> int:
    """Run target tests with the immutable harness interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--target-pytest", action="store_true")
    args = parser.parse_args(argv)

    # Resolve writable test state beneath the container's bounded temporary mount.
    temporary_root = Path(tempfile.gettempdir())
    hypothesis_storage = temporary_root / "hypothesis"
    pytest_cache = temporary_root / "pytest-cache"
    os.environ.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", str(hypothesis_storage))
    os.environ["PYTEST_ADDOPTS"] = f"-o cache_dir={pytest_cache}"

    try:
        if args.target_pytest:
            site_packages = configure_target_import_paths(
                args.source, args.venv, target_pytest=True
            )
            configure_target_pytest_version(site_packages)
            import pytest
        else:
            # Load the trusted pytest before exposing any target-controlled paths.
            import pytest

            configure_target_import_paths(args.source, args.venv)
    except (ImportError, OSError, ValueError, metadata.PackageNotFoundError) as exc:
        print(f"Trusted test runner configuration error: {exc}", file=sys.stderr)
        return 2

    return int(pytest.main([
        "-q",
        "-o",
        f"cache_dir={pytest_cache}",
        str(args.source.resolve()),
    ]))


if __name__ == "sitecustomize":
    configure_subprocess_pytest_version()


if __name__ == "__main__":
    raise SystemExit(main())
