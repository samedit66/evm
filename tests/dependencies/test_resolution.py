from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from evm.dependencies.resolution import install_dependencies, resolve_dependencies
from evm.errors import EvmError
from evm.lockfile import LockedPackage, LockFile, manifest_fingerprint
from evm.manifest import load_manifest
from evm.model import Dependency, Project
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import LIBRARY_TEMPLATE


def test_resolver_rejects_unknown_selected_dependency(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))

    with pytest.raises(EvmError, match="unknown dependency: missing"):
        resolve_dependencies(project, names={"missing"})


def test_resolver_deduplicates_compatible_transitive_dependency(tmp_path: Path) -> None:
    project = _project_with_diamond_dependencies(tmp_path, conflicting=False)

    lock = resolve_dependencies(project)

    assert [package.name for package in lock.packages] == ["left", "right", "shared"]
    assert lock.package("left").dependencies == ("shared",)
    assert lock.package("right").dependencies == ("shared",)


def test_resolver_rejects_conflicting_transitive_versions(tmp_path: Path) -> None:
    project = _project_with_diamond_dependencies(tmp_path, conflicting=True)

    with pytest.raises(EvmError, match="dependency conflict for shared"):
        resolve_dependencies(project)


def test_resolver_reports_dependency_cycle(tmp_path: Path) -> None:
    first = create_project(ProjectCreationRequest(tmp_path / "first", LIBRARY_TEMPLATE))
    second = create_project(ProjectCreationRequest(tmp_path / "second", LIBRARY_TEMPLATE))
    _append_dependency(first, 'second = { path = "../second" }')
    _append_dependency(second, 'first = { path = "../first" }')
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    _append_dependency(project, 'first = { path = "../first" }')

    with pytest.raises(EvmError, match="dependency cycle detected: first -> second -> first"):
        resolve_dependencies(load_manifest(project.manifest_path))


def test_selective_update_preserves_unselected_locked_closure(tmp_path: Path) -> None:
    create_project(ProjectCreationRequest(tmp_path / "first", LIBRARY_TEMPLATE))
    second = create_project(ProjectCreationRequest(tmp_path / "second", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    _append_dependencies(
        project,
        ['first = { path = "../first" }', 'second = { path = "../second" }'],
    )
    parsed = load_manifest(project.manifest_path)
    previous = resolve_dependencies(parsed)
    (second.directory / "src" / "second.e").write_text("class SECOND feature changed: BOOLEAN end")

    updated = resolve_dependencies(parsed, names={"first"}, previous=previous)

    assert updated.package("second") == previous.package("second")


def test_selective_update_rejects_incomplete_locked_closure(tmp_path: Path) -> None:
    create_project(ProjectCreationRequest(tmp_path / "first", LIBRARY_TEMPLATE))
    second = create_project(ProjectCreationRequest(tmp_path / "second", LIBRARY_TEMPLATE))
    _append_dependency(second, 'missing = { path = "../missing" }')
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    _append_dependencies(
        project,
        ['first = { path = "../first" }', 'second = { path = "../second" }'],
    )
    parsed = load_manifest(project.manifest_path)
    first_package = resolve_dependencies(
        replace(parsed, dependencies=(parsed.dependencies[0],))
    ).package("first")
    second_package = LockedPackage(
        name="second",
        version="0.1.0",
        source="path+../second",
        dependencies=("missing",),
        path="../second",
    )
    previous = LockFile(manifest_fingerprint(parsed), (first_package, second_package))

    with pytest.raises(EvmError, match="missing transitive dependency missing"):
        resolve_dependencies(parsed, names={"first"}, previous=previous)


def test_path_dependency_must_exist(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    dependency = Dependency(name="missing", source="path", path="../missing")

    with pytest.raises(EvmError, match="path dependency missing does not exist"):
        resolve_dependencies(replace(project, dependencies=(dependency,)))


def test_path_dependency_rejects_ambiguous_legacy_ecf(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "one.ecf").touch()
    (legacy / "two.ecf").touch()
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    dependency = Dependency(name="legacy", source="path", path="../legacy")

    with pytest.raises(EvmError, match="legacy dependency must specify ecf"):
        resolve_dependencies(replace(project, dependencies=(dependency,)))


def test_dependency_ecf_cannot_escape_package_root(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    outside = tmp_path / "outside.ecf"
    outside.touch()
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    dependency = Dependency(
        name="legacy",
        source="path",
        path="../legacy",
        ecf="../outside.ecf",
    )

    with pytest.raises(EvmError, match=r"dependency\.ecf escapes the dependency root"):
        resolve_dependencies(replace(project, dependencies=(dependency,)))


def test_install_rejects_lock_with_missing_transitive_package(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package = LockedPackage(
        name="json",
        version="1.0.0",
        source="iron+repo",
        dependencies=("base",),
    )
    lock = LockFile(manifest_fingerprint(project), (package,))

    with pytest.raises(EvmError, match="missing transitive dependency base"):
        install_dependencies(project, lock=lock)

    assert not project.state_directory.exists()


def test_install_rejects_missing_path_without_writing_state(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package = LockedPackage(
        name="shared",
        version="1.0.0",
        source="path+../shared",
        path="../shared",
    )
    lock = LockFile(manifest_fingerprint(project), (package,))

    with pytest.raises(EvmError, match="path dependency shared does not exist"):
        install_dependencies(project, lock=lock)

    assert not (project.state_directory / "state.toml").exists()


def test_offline_install_reports_missing_git_identity(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package = LockedPackage(
        name="json",
        version="1.0.0",
        source="git+https://example.invalid/json.git",
        revision="a" * 40,
        tree="b" * 40,
        ecf="json.ecf",
    )
    lock = LockFile(manifest_fingerprint(project), (package,))

    with pytest.raises(EvmError, match=r"json .*git\+.*aaaa.*missing locally"):
        install_dependencies(project, lock=lock, offline=True)


def test_offline_iron_install_materializes_from_verified_archive(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package, archive = _cached_iron_package(project, "json")
    lock = LockFile(manifest_fingerprint(project), (package,))

    install_dependencies(project, lock=lock, offline=True)

    destination = project.state_directory / "deps" / package.materialized_name
    assert (destination / "json.ecf").is_file()
    assert (destination / ".evm-package").is_file()
    assert archive.is_file()


def test_offline_iron_install_rejects_corrupted_archive(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package, archive = _cached_iron_package(project, "json")
    archive.write_bytes(b"corrupted")
    lock = LockFile(manifest_fingerprint(project), (package,))

    with pytest.raises(EvmError, match=r"json .*missing locally"):
        install_dependencies(project, lock=lock, offline=True)


def test_install_repairs_modified_materialized_package_offline(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package, _ = _cached_iron_package(project, "json")
    lock = LockFile(manifest_fingerprint(project), (package,))
    install_dependencies(project, lock=lock, offline=True)
    destination = project.state_directory / "deps" / package.materialized_name
    (destination / "json.ecf").write_text("damaged")

    install_dependencies(project, lock=lock, offline=True)

    assert (destination / "json.ecf").read_text() == "<system/>"


def test_install_rejects_unsupported_locked_source_and_cleans_staging(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    package = LockedPackage(name="json", version="1.0.0", source="unknown")
    lock = LockFile(manifest_fingerprint(project), (package,))

    with pytest.raises(EvmError, match="unsupported locked source for json"):
        install_dependencies(project, lock=lock)

    assert list((project.state_directory / "tmp").iterdir()) == []
    assert not (project.state_directory / "state.toml").exists()


def test_install_writes_each_materialized_package_once_to_state(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    first, _ = _cached_iron_package(project, "first")
    second, _ = _cached_iron_package(project, "second")
    lock = LockFile(manifest_fingerprint(project), (first, second))

    install_dependencies(project, lock=lock, offline=True)

    state = (project.state_directory / "state.toml").read_text()
    assert state.count('name = "first"') == 1
    assert state.count('name = "second"') == 1


def test_git_tree_mismatch_is_rejected_before_materialization(tmp_path: Path) -> None:
    repository = create_project(
        ProjectCreationRequest(tmp_path / "library", LIBRARY_TEMPLATE)
    ).directory
    _initialize_repository(repository)
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    dependency = Dependency(
        name="library",
        source="git",
        git=str(repository),
        requested_kind="branch",
        requested_value="main",
    )
    parsed = replace(project, dependencies=(dependency,))
    lock = resolve_dependencies(parsed)
    package = replace(lock.package("library"), tree="0" * 40)
    bad_lock = replace(lock, packages=(package,))

    with pytest.raises(EvmError, match="Git tree verification failed for library"):
        install_dependencies(parsed, lock=bad_lock, offline=True)

    assert not (project.state_directory / "deps" / package.materialized_name).exists()


def _project_with_diamond_dependencies(tmp_path: Path, *, conflicting: bool) -> Project:
    shared = create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))
    alternative = create_project(ProjectCreationRequest(tmp_path / "alternative", LIBRARY_TEMPLATE))
    alternative_manifest = alternative.manifest_path
    alternative_manifest.write_text(
        alternative_manifest.read_text()
        .replace('name = "alternative"', 'name = "shared"')
        .replace('version = "0.1.0"', 'version = "2.0.0"')
    )
    left = create_project(ProjectCreationRequest(tmp_path / "left", LIBRARY_TEMPLATE))
    right = create_project(ProjectCreationRequest(tmp_path / "right", LIBRARY_TEMPLATE))
    _append_dependency(left, 'shared = { path = "../shared" }')
    right_source = "../alternative" if conflicting else "../shared"
    _append_dependency(right, f'shared = {{ path = "{right_source}" }}')
    project = create_project(ProjectCreationRequest(tmp_path / "app"))
    _append_dependencies(
        project,
        ['left = { path = "../left" }', 'right = { path = "../right" }'],
    )
    assert shared.directory.is_dir()
    return load_manifest(project.manifest_path)


def _append_dependency(project: Project, dependency: str) -> None:
    _append_dependencies(project, [dependency])


def _append_dependencies(project: Project, dependency_lines: list[str]) -> None:
    manifest = project.manifest_path
    manifest.write_text(manifest.read_text() + "\n[dependencies]\n" + "\n".join(dependency_lines))


def _cached_iron_package(project: Project, name: str) -> tuple[LockedPackage, Path]:
    archive_bytes = _iron_archive_bytes(name)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    archive = project.state_directory / "sources" / "archives" / f"{digest}.tar.bz2"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(archive_bytes)
    package = LockedPackage(
        name=name,
        version="1.0.0",
        source="iron+https://example.invalid/iron/1.0",
        checksum=f"sha256:{digest}",
        ecf=f"{name}.ecf",
        archive=f"https://example.invalid/{name}.tar.bz2",
    )
    return package, archive


def _iron_archive_bytes(name: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2") as archive:
        content = b"<system/>"
        info = tarfile.TarInfo(f"{name}.ecf")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _initialize_repository(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "evm-tests@example.invalid")
    _git(path, "config", "user.name", "EVM Tests")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
