from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from code_test_lab.trusted_test_runner import (
    configure_subprocess_pytest_version,
    configure_target_import_paths,
    configure_target_pytest_version,
)


def test_target_paths_are_added_without_executing_untrusted_pth_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "target"
    source_src = source / "src"
    source_src.mkdir(parents=True)
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    marker = tmp_path / "pth-executed"
    (site_packages / "untrusted.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )

    original_path = list(sys.path)
    monkeypatch.setattr(sys, "path", original_path.copy())
    trusted_pytest = pytest.__file__

    result = configure_target_import_paths(source, venv)

    assert result == site_packages
    assert sys.path[: len(original_path)] == original_path
    assert sys.path[-3:] == [str(source_src), str(source), str(site_packages)]
    assert pytest.__file__ == trusted_pytest
    assert not marker.exists()


def test_target_pytest_paths_precede_harness_site_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sysconfig

    source = tmp_path / "target"
    (source / "src").mkdir(parents=True)
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    original_path = list(sys.path)
    monkeypatch.setattr(sys, "path", original_path.copy())
    trusted_site = Path(sysconfig.get_paths()["purelib"]).resolve()
    trusted_index = next(
        index for index, entry in enumerate(original_path)
        if Path(entry).resolve() == trusted_site
    )

    configure_target_import_paths(source, venv, target_pytest=True)

    assert sys.path[trusted_index:trusted_index + 3] == [
        str(source / "src"), str(source), str(site_packages)
    ]
    assert sys.path.index(str(site_packages)) < sys.path.index(str(trusted_site))


def test_target_paths_reject_ambiguous_virtualenv_site_packages(tmp_path: Path) -> None:
    source = tmp_path / "target"
    source.mkdir()
    venv = tmp_path / "venv"
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (venv / "lib" / "python3.13" / "site-packages").mkdir(parents=True)

    with pytest.raises(ValueError, match="exactly one site-packages"):
        configure_target_import_paths(source, venv)



def test_prepared_import_paths_are_inherited_by_child_processes_without_pth_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "target"
    source_src = source / "src"
    source_src.mkdir(parents=True)
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "prepared_dependency.py").write_text(
        "VALUE = 'prepared'\n", encoding="utf-8"
    )
    marker = tmp_path / "child-pth-executed"
    (site_packages / "untrusted.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", "/untrusted/inherited/path")
    monkeypatch.setattr(sys, "path", list(sys.path))

    configure_target_import_paths(source, venv)

    expected_paths = os.pathsep.join(
        (str(source_src), str(source), str(site_packages))
    )
    assert os.environ["PYTHONPATH"] == expected_paths
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import prepared_dependency; print(prepared_dependency.VALUE)",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.strip() == "prepared"
    assert not marker.exists()


def test_target_pytest_uses_validated_prepared_distribution_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = tmp_path / "site-packages"
    dist_info = site_packages / "pytest-9.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest\nVersion: 9.0.0\n",
        encoding="utf-8",
    )
    target_pytest = types.ModuleType("_pytest")
    monkeypatch.setitem(sys.modules, "_pytest", target_pytest)

    configure_target_pytest_version(site_packages)

    assert target_pytest.__version__ == "9.0.0"
    assert target_pytest.version_tuple == (9, 0, 0)
    assert sys.modules["_pytest._version"].version == "9.0.0"
    assert sys.modules["_pytest._version"].version_tuple == (9, 0, 0)
    assert target_pytest._version is sys.modules["_pytest._version"]
    assert os.environ["TARGET_PYTEST_VERSION"] == "9.0.0"


def test_subprocess_bootstrap_installs_validated_pytest_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_pytest = types.ModuleType("_pytest")
    monkeypatch.setitem(sys.modules, "_pytest", target_pytest)
    monkeypatch.setenv("TARGET_PYTEST_VERSION", "9.2.0+sandbox.g123456789abc")

    configure_subprocess_pytest_version()

    assert target_pytest.__version__ == "9.2.0+sandbox.g123456789abc"
    assert target_pytest.version_tuple == (9, 2, 0)
    assert sys.modules["_pytest._version"].version == "9.2.0+sandbox.g123456789abc"
    assert sys.modules["_pytest._version"].version_tuple == (9, 2, 0)


def test_subprocess_bootstrap_rejects_invalid_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TARGET_PYTEST_VERSION", "not-a-version")

    with pytest.raises(RuntimeError, match="environment value is invalid"):
        configure_subprocess_pytest_version()


def test_target_pytest_rejects_missing_or_invalid_distribution_version(
    tmp_path: Path
) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    with pytest.raises(ValueError, match="exactly one pytest distribution"):
        configure_target_pytest_version(site_packages)

    dist_info = site_packages / "pytest-unknown.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest\nVersion: unknown\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid version metadata"):
        configure_target_pytest_version(site_packages)
