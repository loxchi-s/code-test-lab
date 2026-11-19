from __future__ import annotations

from pathlib import Path

import pytest

from code_test_lab.target_preparation import prepare_target_source


COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _write_project(path: Path, *, name: str = "pytest", dynamic: bool = True) -> None:
    version_config = 'dynamic = ["version"]' if dynamic else 'version = "8.0.0"'
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=77", "setuptools-scm[toml]>=10.1"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        f"{version_config}\n",
        encoding="utf-8",
    )


def test_prepare_source_excludes_git_and_sets_commit_scoped_scm_version(tmp_path: Path) -> None:
    source = tmp_path / "target"
    destination = tmp_path / "prepared"
    _write_project(source)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("[remote]", encoding="utf-8")
    (source / ".hidden").write_text("preserved", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    overrides = prepare_target_source(source, destination, COMMIT_SHA)

    assert overrides == {
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST": "0.0.dev0+g0123456789ab",
        "VCS_VERSIONING_PRETEND_VERSION_FOR_PYTEST": "0.0.dev0+g0123456789ab",
    }
    assert not (destination / ".git").exists()
    assert (destination / ".hidden").read_text(encoding="utf-8") == "preserved"
    assert (destination / "tests" / "test_sample.py").is_file()


def test_static_project_version_does_not_receive_scm_override(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source, dynamic=False)

    assert prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA) == {}


def test_dynamic_version_without_setuptools_scm_does_not_receive_override(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling>=1.25"]\n'
        'build-backend = "hatchling.build"\n'
        '[project]\nname = "sample-project"\ndynamic = ["version"]\n',
        encoding="utf-8",
    )

    assert prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA) == {}


@pytest.mark.parametrize("commit_sha", ["", "abc", "g" * 40, "a" * 41])
def test_invalid_target_commit_is_rejected_before_copy(
    tmp_path: Path, commit_sha: str,
) -> None:
    source = tmp_path / "target"
    _write_project(source)

    with pytest.raises(ValueError, match="full 40- or 64-character"):
        prepare_target_source(source, tmp_path / "prepared", commit_sha)

    assert not (tmp_path / "prepared").exists()


def test_symlinked_pyproject_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target"
    source.mkdir()
    external = tmp_path / "external.toml"
    external.write_text('[project]\nname = "not-trusted"\n', encoding="utf-8")
    (source / "pyproject.toml").symlink_to(external)

    with pytest.raises(ValueError, match="must not be a symbolic link"):
        prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)


def test_oversized_pyproject_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target"
    source.mkdir()
    (source / "pyproject.toml").write_bytes(b"#" * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="1 MiB metadata limit"):
        prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)
