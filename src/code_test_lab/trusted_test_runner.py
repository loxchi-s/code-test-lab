"""Run target tests with the harness-owned pytest installation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest


def configure_target_import_paths(source: Path, venv: Path) -> Path:
    """Expose target source and dependencies without processing untrusted .pth files."""
    source = source.resolve(strict=True)
    venv = venv.resolve(strict=True)
    if not source.is_dir() or not venv.is_dir():
        raise ValueError("target source and prepared virtual environment must be directories")

    site_packages = sorted((venv / "lib").glob("python*/site-packages"))
    if len(site_packages) != 1 or not site_packages[0].is_dir():
        raise ValueError("prepared virtual environment must contain exactly one site-packages directory")

    # Keep the harness interpreter and pytest dependencies ahead of target-controlled paths.
    sys.path.extend(
        str(path) for path in (source / "src", source) if path.is_dir()
    )
    # Do not call site.addsitedir(): it executes .pth files from the untrusted environment.
    sys.path.append(str(site_packages[0]))
    return site_packages[0]


def main(argv: list[str] | None = None) -> int:
    """Run the target's pytest suite using the trusted harness pytest package."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        configure_target_import_paths(args.source, args.venv)
    except (OSError, ValueError) as exc:
        print(f"Trusted test runner configuration error: {exc}", file=sys.stderr)
        return 2

    return int(pytest.main([
        "-q",
        "-p",
        "no:cacheprovider",
        str(args.source.resolve()),
    ]))


if __name__ == "__main__":
    raise SystemExit(main())
