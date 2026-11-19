"""Run target tests with the harness-owned Python interpreter."""

from __future__ import annotations

import argparse
import sys
import sysconfig
from pathlib import Path


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
    target_paths.append(str(site_packages[0]))

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


def main(argv: list[str] | None = None) -> int:
    """Run target tests with the immutable harness interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--target-pytest", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.target_pytest:
            configure_target_import_paths(args.source, args.venv, target_pytest=True)
            import pytest
        else:
            # Load the trusted pytest before exposing any target-controlled paths.
            import pytest

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
