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
    # Restore metadata from the prepared distribution in this process. Child
    # interpreters independently read the same read-only distribution metadata;
    # they must not trust an environment variable that target tests can mutate.
    _install_pytest_version(version)


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


class _XpassGate:
    """Fail the harness run for any unexpected pass of an xfail-marked test."""

    def __init__(self) -> None:
        self.xpasses: list[str] = []

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "call" and report.outcome == "passed" and hasattr(report, "wasxfail"):
            self.xpasses.append(report.nodeid)

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        if self.xpasses:
            print(
                "\\nXPASS correctness gate rejected unexpected passes:\\n"
                + "\\n".join(f" - {nodeid}" for nodeid in self.xpasses),
                file=sys.stderr,
            )
            session.exitstatus = 1


TARGET_VENV_PATH = Path("/sandbox/venv")


def configure_subprocess_pytest_version() -> None:
    """Bootstrap pytest metadata from the read-only prepared venv in child interpreters."""
    site_packages = sorted((TARGET_VENV_PATH / "lib").glob("python*/site-packages"))
    if not site_packages:
        return
    if len(site_packages) != 1 or not site_packages[0].is_dir():
        raise RuntimeError("prepared target venv has ambiguous site-packages directories")

    matches = [
        distribution
        for distribution in metadata.distributions(path=[str(site_packages[0])])
        if (distribution.metadata.get("Name") or "").lower() == "pytest"
    ]
    if not matches:
        return
    if len(matches) != 1:
        raise RuntimeError("prepared target venv has ambiguous pytest distribution metadata")

    try:
        version = Version(matches[0].version)
    except InvalidVersion as exc:
        raise RuntimeError("prepared target pytest distribution has invalid version metadata") from exc

    # Do not import _pytest from sitecustomize: that runs before pytest's normal
    # startup and can change plugin initialization order. Resolve its generated
    # version module only when target pytest imports it. The venv is mounted
    # read-only in the test container, so target code cannot rewrite this source.
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

    # Do not pass the repository root as an explicit collection path.
    # Doing so overrides pytest's configured testpaths and can collect docs,
    # examples, and other non-suite files in projects such as pytest itself.
    # The workflow already sets cwd to the target repository, so pytest can
    # discover tests using the target project's own configuration.
    return int(pytest.main([
        "-q",
        "-r",
        "fEXsx",
        "-o",
        f"cache_dir={pytest_cache}",
        "-o",
        "xfail_strict=true",
    ]))


if __name__ == "sitecustomize":
    configure_subprocess_pytest_version()


if __name__ == "__main__":
    raise SystemExit(main())
