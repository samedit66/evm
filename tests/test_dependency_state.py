from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import evm.dependencies as dependencies
from evm.dependencies import clean_unused, dependency_library_locations, dependency_tree_lines
from evm.errors import EvmError
from evm.lockfile import LockedPackage, LockFile, manifest_fingerprint
from evm.model import Dependency
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import LIBRARY_TEMPLATE
from evm.toolchains import Detection
from evm.versioning import NumericVersion


def test_dependency_library_locations_filter_and_normalize_paths(tmp_path: Path) -> None:
    shared = create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    path_package = LockedPackage(
        name="shared",
        version="1",
        source="path+../shared",
        ecf="shared.ecf",
        path="../shared",
    )
    dev_package = LockedPackage(
        name="testing",
        version="1",
        source="iron+repo",
        ecf="testing.ecf",
        development=True,
    )
    metadata_only = LockedPackage(name="metadata", version="1", source="iron+repo")
    lock = LockFile(manifest_fingerprint(project), (path_package, dev_package, metadata_only))

    runtime = dependency_library_locations(project, lock, include_development=False)
    development = dependency_library_locations(project, lock, include_development=True)

    assert runtime == (("shared", "../shared/shared.ecf"),)
    assert development == (
        ("shared", "../shared/shared.ecf"),
        ("testing", f".evm/deps/{dev_package.materialized_name}/testing.ecf"),
    )
    assert shared.ecf_path.is_file()


def test_dependency_tree_renders_sources_missing_nodes_and_cycles(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    project = replace(
        project,
        dependencies=(
            Dependency(name="git", source="path", path="../git"),
            Dependency(name="ise", source="path", path="../ise"),
            Dependency(name="missing", source="path", path="../missing"),
        ),
    )
    git = LockedPackage(
        name="git",
        version="1",
        source="git+repo",
        requested="tag:v1",
        revision="a" * 40,
        dependencies=("gobo",),
        patched=True,
    )
    gobo = LockedPackage(
        name="gobo",
        version="2",
        source="gobo-distribution",
        dependencies=("git", "iron"),
    )
    iron = LockedPackage(name="iron", version="3", source="iron+repo")
    ise = LockedPackage(name="ise", version="4", source="eiffelstudio")
    lock = LockFile(manifest_fingerprint(project), (git, gobo, iron, ise))

    lines = dependency_tree_lines(project, lock)

    rendered = "\n".join(lines)
    assert "git 1 [git tag:v1 @ aaaaaaaa patched]" in rendered
    assert "gobo 2 [Gobo]" in rendered
    assert "iron 3 [IRON]" in rendered
    assert "ise 4 [ISE]" in rendered
    assert "missing [missing from lock]" in rendered


def test_dependency_tree_focus_explains_all_paths(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    project = replace(
        project,
        dependencies=(
            Dependency(name="left", source="path", path="../left"),
            Dependency(name="right", source="path", path="../right"),
        ),
    )
    shared = LockedPackage(name="shared", version="1", source="iron+repo")
    left = LockedPackage(
        name="left",
        version="1",
        source="path+../left",
        dependencies=("shared",),
        path="../left",
    )
    right = replace(left, name="right", source="path+../right", path="../right")
    lock = LockFile(manifest_fingerprint(project), (left, right, shared))

    lines = dependency_tree_lines(project, lock, focus="shared")

    assert lines[1:] == ["left -> shared (1)", "right -> shared (1)"]
    with pytest.raises(EvmError, match="dependency 'missing' is not present"):
        dependency_tree_lines(project, lock, focus="missing")


def test_clean_unused_handles_absent_state_and_removes_archive_files(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package = LockedPackage(
        name="json",
        version="1",
        source="iron+repo",
        checksum="sha256:keep",
        archive="https://example.invalid/json",
    )
    lock = LockFile(manifest_fingerprint(project), (package,))

    assert clean_unused(project, lock) == (0, 0)

    archives = project.state_directory / "sources" / "archives"
    archives.mkdir(parents=True)
    (archives / "keep.tar.bz2").touch()
    (archives / "remove.tar.bz2").touch()
    (archives / "directory").mkdir()

    assert clean_unused(project, lock) == (0, 1)
    assert sorted(path.name for path in archives.iterdir()) == ["directory", "keep.tar.bz2"]


def test_distribution_resolution_and_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "ise-library"
    time = library_root / "time"
    time.mkdir(parents=True)
    (time / "time.ecf").write_text("<system/>")
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    dependency = Dependency(name="time_lib", source="ise", library="time")
    project = replace(project, dependencies=(dependency,))
    detection = Detection(tmp_path / "ec", NumericVersion.parse("25.2.0"), None)
    monkeypatch.setattr(dependencies, "detect", lambda _: detection)
    monkeypatch.setattr(dependencies, "_distribution_root", lambda _: library_root)

    lock = dependencies.resolve_dependencies(project)
    dependencies.install_dependencies(project, lock=lock)

    package = lock.package("time_lib")
    destination = project.state_directory / "deps" / package.materialized_name
    assert package.source == "eiffelstudio"
    assert package.version == "25.2.0"
    assert (destination / "time.ecf").is_file()


def test_distribution_resolution_reports_detection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    dependency = Dependency(name="time", source="ise")
    project = replace(project, dependencies=(dependency,))
    detection = Detection(None, None, "not installed")
    monkeypatch.setattr(dependencies, "detect", lambda _: detection)

    with pytest.raises(EvmError, match="cannot resolve time: not installed"):
        dependencies.resolve_dependencies(project)


def test_gobo_distribution_is_copied_under_library_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "gobo"
    (root / "xml").mkdir(parents=True)
    (root / "xml" / "xml.ecf").touch()
    destination = tmp_path / "destination"
    package = LockedPackage(name="xml", version="1", source="gobo-distribution")
    monkeypatch.setattr(dependencies, "_distribution_root", lambda _: root)

    dependencies._install_distribution(package, destination)

    assert (destination / "library" / "xml" / "xml.ecf").is_file()
