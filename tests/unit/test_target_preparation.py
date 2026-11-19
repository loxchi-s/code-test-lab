import sys
from collections.abc import Callable
from pathlib import Path

from code_test_lab.target_preparation import (
    main,
    prepare_target_source,
    scm_version_overrides,
)


COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _write_project(
    path: Path,
    *,
    name: str = "pytest",
    dynamic: bool = True,
    requirement: str = "setuptools-scm[toml]>=10.1",
    dev_requirements: tuple[str, ...] = ("pytest-xdist>=3.5",),
    dependencies: tuple[str, ...] = ("iniconfig>=2",),
    build_backend: str = "setuptools.build_meta",
    tool_config: str = '[tool.setuptools_scm]\nversion_file = "src/_version.py"\n',
) -> None:
    version_config = 'dynamic = ["version"]' if dynamic else 'version = "8.0.0"'
    build_requirement = (
        f'"hatchling>=1.25", "{requirement}"'
        if build_backend == "hatchling.build"
        else f'"setuptools>=77", "{requirement}"'
    )
    build_requirements = build_requirement
    dependency_lines = ", ".join(f'"{item}"' for item in dependencies)
    dev_lines = ", ".join(f'"{item}"' for item in dev_requirements)
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        "[build-system]\n"
        f"requires = [{build_requirements}]\n"
        f'build-backend = "{build_backend}"\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        f"{version_config}\n"
        f"dependencies = [{dependency_lines}]\n\n"
        "[project.optional-dependencies]\n"
        f"dev = [{dev_lines}]\n\n"
        f"{tool_config}",
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


def test_prepare_source_excludes_git_and_returns_static_dependency_contract(tmp_path: Path) -> None:
    source = tmp_path / "target"
    destination = tmp_path / "prepared"
    _write_project(source)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("[remote]", encoding="utf-8")
    (source / ".hidden").write_text("preserved", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    metadata = prepare_target_source(source, destination, COMMIT_SHA)

    assert metadata.distribution_name == "pytest"
    assert metadata.requirements == ("iniconfig>=2", "pytest-xdist>=3.5")
    assert metadata.vcs_version_provider == "setuptools-scm"
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
        metadata = prepare_target_source(source, tmp_path / f"prepared-{index}", COMMIT_SHA)
        assert metadata.vcs_version_provider == "setuptools-scm", requirement


def test_hatch_vcs_receives_commit_scoped_version_overrides(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(
        source,
        requirement="hatch-vcs>=0.4",
        build_backend="hatchling.build",
        tool_config='[tool.hatch.version]\nsource = "vcs"\n',
    )
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    assert metadata.vcs_version_provider == "hatch-vcs"
    assert scm_version_overrides(metadata, COMMIT_SHA, "1.2.3") == {
        "SETUPTOOLS_SCM_PRETEND_VERSION": "1.2.3+sandbox.g0123456789ab",
    }


def test_scm_fallback_uses_seed_distribution_version_to_satisfy_dev_constraints(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target"
    _write_project(source, dev_requirements=("pytest-xdist>=3.5",))
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    overrides = scm_version_overrides(metadata, COMMIT_SHA, "9.1.0")

    assert overrides["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST"] == (
        "9.1.0+sandbox.g0123456789ab"
    )
    assert overrides["VCS_VERSIONING_PRETEND_VERSION_FOR_PYTEST"] == (
        "9.1.0+sandbox.g0123456789ab"
    )


def test_scm_fallback_without_seed_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source)
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    assert scm_version_overrides(metadata, COMMIT_SHA) == {
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST": "0.0.dev0+sandbox.g0123456789ab",
        "VCS_VERSIONING_PRETEND_VERSION_FOR_PYTEST": "0.0.dev0+sandbox.g0123456789ab",
    }


def test_existing_seed_local_version_is_preserved(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source)
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    assert scm_version_overrides(metadata, COMMIT_SHA, "9.1.0+vendor.1") == {
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYTEST": (
            "9.1.0+vendor.1.sandbox.g0123456789ab"
        ),
        "VCS_VERSIONING_PRETEND_VERSION_FOR_PYTEST": (
            "9.1.0+vendor.1.sandbox.g0123456789ab"
        ),
    }


def test_static_project_version_does_not_receive_scm_override(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source, dynamic=False)
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    assert metadata.vcs_version_provider is None
    assert scm_version_overrides(metadata, COMMIT_SHA) == {}


def test_dynamic_version_without_supported_vcs_backend_does_not_receive_override(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target"
    _write_project(
        source,
        requirement="hatchling>=1.25",
        build_backend="hatchling.build",
        tool_config="",
    )
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    assert metadata.vcs_version_provider is None
    assert scm_version_overrides(metadata, COMMIT_SHA) == {}


def test_prepare_cli_writes_requirements_and_distribution_metadata(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "target"
    destination = tmp_path / "prepared"
    _write_project(source)
    requirements_file = tmp_path / "requirements.txt"
    distribution_file = tmp_path / "distribution-name"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_preparation",
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--commit-sha",
            COMMIT_SHA,
            "--requirements-file",
            str(requirements_file),
            "--distribution-file",
            str(distribution_file),
        ],
    )

    assert main() == 0
    assert requirements_file.read_text(encoding="utf-8") == (
        "iniconfig>=2\npytest-xdist>=3.5\n"
    )
    assert distribution_file.read_text(encoding="utf-8") == "pytest\n"


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


def test_dynamic_dependency_metadata_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=77"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '[project]\nname = "sample-project"\ndynamic = ["dependencies"]\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n',
        encoding="utf-8",
    )

    _assert_raises(
        ValueError,
        "dynamic project dependencies are unsupported",
        lambda: prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA),
    )


def test_dev_extra_must_include_pytest_for_non_pytest_projects(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source, name="sample-project", dev_requirements=("coverage>=7",))

    _assert_raises(
        ValueError,
        "dev extra must include pytest",
        lambda: prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA),
    )


def test_hatch_vcs_config_requires_hatch_vcs_build_dependency(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(
        source,
        requirement="hatchling>=1.25",
        build_backend="hatchling.build",
        tool_config='[tool.hatch.version]\nsource = "vcs"\n',
    )

    _assert_raises(
        ValueError,
        "requires hatch-vcs in build-system.requires",
        lambda: prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA),
    )


def test_invalid_seed_version_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "target"
    _write_project(source)
    metadata = prepare_target_source(source, tmp_path / "prepared", COMMIT_SHA)

    _assert_raises(
        ValueError,
        "seed distribution version",
        lambda: scm_version_overrides(metadata, COMMIT_SHA, "9.1.0;bad"),
    )
