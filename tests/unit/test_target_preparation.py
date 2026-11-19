from collections.abc import Callable
from pathlib import Path

from code_test_lab.target_preparation import prepare_target_source

COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _write_project(
    path: Path,
    *,
    name: str = "pytest",
    dynamic: bool = True,
    requirement: str = "setuptools-scm[toml]>=10.1",
) -> None:
    version_config = 'dynamic = ["version"]' if dynamic else 'version = "8.0.0"'
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        "[build-system]\n"
        f'requires = ["setuptools>=77", "{requirement}"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        f"{version_config}\n",
        encoding="utf-8",
    )


def _assert_raises(
    expected_exception: type[Exception],
    expected_message: str,
    operation: Callable[[], object],
) -> None:
    try:
        operation()
    except expected_exception as error:
        assert expected_message in str(error)
    else:
        raise AssertionError(f"Expected {expected_exception.__name__} to be raised")


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


def test_setuptools_scm_requirement_names_are_pep503_normalized(tmp_path: Path) -> None:
    requirements = (
        "setuptools-scm[toml]>=10.1",
        "setuptools_scm[toml]>=10.1",
        "setuptools.scm[toml]>=10.1",
        "setuptools__scm[toml]>=10.1",
    )
    for index, requirement in enumerate(requirements):
        source = tmp_path / f"target-{index}"
        _write_project(source, requirement=requirement)
        overrides = prepare_target_source(source, tmp_path / f"prepared-{index}", COMMIT_SHA)
        assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST" in overrides, requirement
        assert "VCS_VERSIONING_PRETEND_VERSION_FOR_PYTEST" in overrides, requirement


def test_hatch_vcs_receives_commit_scoped_version_overrides(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source, requirement="hatch-vcs>=0.4")

    overrides = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    assert overrides == {
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST": "0.0.dev0+g0123456789ab",
        "VCS_VERSIONING_PRETEND_VERSION_FOR_PYTEST": "0.0.dev0+g0123456789ab",
    }


def test_static_project_version_does_not_receive_scm_override(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source, dynamic=False)

    assert prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA) == {}


def test_dynamic_version_without_supported_vcs_backend_does_not_receive_override(
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


def test_invalid_target_commit_is_rejected_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source)
    for commit_sha in ("", "abc", "g" * 40, "a" * 41):
        destination = tmp_path / f"prepared-{len(commit_sha)}-{commit_sha[:1]}"
        _assert_raises(
            ValueError,
            "full 40- or 64-character",
            lambda sha=commit_sha, dest=destination: prepare_target_source(
                source, dest, sha
            ),
        )
        assert not destination.exists()


def test_symlinked_pyproject_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target"
    source.mkdir()
    external = tmp_path / "external.toml"
    external.write_text('[project]\nname = "not-trusted"\n', encoding="utf-8")
    (source / "pyproject.toml").symlink_to(external)

    _assert_raises(
        ValueError,
        "must not be a symbolic link",
        lambda: prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA),
    )


def test_oversized_pyproject_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target"
    source.mkdir()
    (source / "pyproject.toml").write_bytes(b"#" * (1024 * 1024 + 1))

    _assert_raises(
        ValueError,
        "1 MiB metadata limit",
        lambda: prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA),
    )
