from __future__ import annotations

import sys
from pathlib import Path

import pytest

from code_test_lab.trusted_test_runner import configure_target_import_paths


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


def test_target_paths_reject_ambiguous_virtualenv_site_packages(tmp_path: Path) -> None:
    from code_test_lab.trusted_test_runner import configure_target_import_paths

    source = tmp_path / "target"
    source.mkdir()
    venv = tmp_path / "venv"
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (venv / "lib" / "python3.13" / "site-packages").mkdir(parents=True)

    with pytest.raises(ValueError, match="exactly one site-packages"):
        configure_target_import_paths(source, venv)
