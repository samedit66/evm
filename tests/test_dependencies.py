from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from lxml import etree

import evm.dependencies as dependencies
from evm.cli import main
from evm.dependencies import install_dependencies, resolve_dependencies
from evm.ecf import generate_ecf
from evm.errors import EvmError
from evm.lockfile import LockedPackage, LockFile, load_lock
from evm.manifest import load_manifest
from evm.model import Dependency, Project
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import LIBRARY_TEMPLATE


def test_resolver_rejects_programmatic_implicit_runtime_dependency(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    dependency = Dependency(name="eiffel_base", source="ise", library="base")
    project_with_runtime_dependency = replace(project, dependencies=(dependency,))

    with pytest.raises(EvmError, match="runtime library 'base'"):
        resolve_dependencies(project_with_runtime_dependency)

    assert not (project.directory / ".evm" / "deps").exists()


def test_resolver_accepts_implicit_runtime_discovered_from_iron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    json_dependency = Dependency(name="json", source="iron", version="25.02")
    project = replace(project, dependencies=(json_dependency,))
    base_dependency = Dependency(
        name="base",
        source="iron",
        version="25.02",
        ecf="base.ecf",
    )
    packages = {
        "json": LockedPackage(name="json", version="25.02", source="iron+repository"),
        "base": LockedPackage(name="base", version="25.02", source="iron+repository"),
    }

    def resolve_iron_fixture(
        _project: Project,
        dependency: Dependency,
        *,
        offline: bool,
        previous: LockFile | None,
    ) -> tuple[LockedPackage, Project | None, tuple[Dependency, ...]]:
        del offline, previous
        discovered = (base_dependency,) if dependency.name == "json" else ()
        return packages[dependency.name], None, discovered

    monkeypatch.setattr(dependencies, "_resolve_iron", resolve_iron_fixture)

    lock = resolve_dependencies(project)

    assert {package.name for package in lock.packages} == {"base", "json"}
    assert lock.package("json").dependencies == ("base",)


@pytest.mark.parametrize(
    "arguments",
    [
        ["add", "base", "--source", "ise"],
        ["add", "eiffel_base", "--source", "ise", "--library", "base"],
    ],
)
def test_add_runtime_dependency_preserves_project_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    files = (project.manifest_path, project.directory / "Eiffel.lock", project.ecf_path)
    original_contents = {path: path.read_bytes() for path in files}

    result = CliRunner().invoke(main, arguments)

    assert result.exit_code != 0
    assert "runtime library 'base'" in result.output
    assert {path: path.read_bytes() for path in files} == original_contents
    assert not (project.directory / ".evm" / "deps").exists()


def test_path_dependency_is_live_and_rendered_as_relative_ecf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()

    added = runner.invoke(main, ["add", "shared", "--path", "../shared"])

    assert added.exit_code == 0, added.output
    lock = load_lock(project.directory / "Eiffel.lock")
    package = lock.package("shared")
    assert package.path == "../shared"
    assert package.ecf == "shared.ecf"
    assert not (project.directory / ".evm" / "deps").exists()
    locations = etree.parse(str(project.ecf_path)).xpath(
        "//*[local-name()='library'][@name='shared']/@location"
    )
    assert locations == ["../shared/shared.ecf"]

    (dependency.directory / "src" / "shared.e").write_text(
        "class SHARED feature value: INTEGER = 1 end"
    )
    checked = runner.invoke(main, ["check", "--configuration-only"])
    release = runner.invoke(main, ["check", "--configuration-only", "--release"])

    assert checked.exit_code == 0, checked.output
    assert release.exit_code != 0
    assert "external path dependency" in release.output


def test_git_tag_is_locked_materialized_and_reinstalled_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _git_library(tmp_path / "tagged", version="1.2.3", tag="v1.2.3")
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()

    added = runner.invoke(
        main,
        ["add", "tagged", "--git", str(repository), "--tag", "v1.2.3"],
    )

    assert added.exit_code == 0, added.output
    lock = load_lock(project.directory / "Eiffel.lock")
    package = lock.package("tagged")
    assert package.requested == "tag:v1.2.3"
    assert package.revision is not None and len(package.revision) == 40
    assert package.tree is not None and len(package.tree) == 40
    destination = project.directory / ".evm" / "deps" / package.materialized_name
    assert (destination / "tagged.ecf").is_file()

    shutil.rmtree(destination)
    installed = runner.invoke(main, ["install", "--locked", "--offline"])

    assert installed.exit_code == 0, installed.output
    assert (destination / "tagged.ecf").is_file()


def test_git_branch_changes_only_after_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _git_library(tmp_path / "moving", version="1.0.0")
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()
    added = runner.invoke(
        main,
        ["add", "moving", "--git", str(repository), "--branch", "main"],
    )
    assert added.exit_code == 0, added.output
    before = load_lock(project.directory / "Eiffel.lock").package("moving").revision
    (repository / "src" / "moving.e").write_text("class MOVING feature value: INTEGER = 2 end")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "change value")

    installed = runner.invoke(main, ["install", "--locked"])
    unchanged = load_lock(project.directory / "Eiffel.lock").package("moving").revision
    updated = runner.invoke(main, ["update", "moving"])
    after = load_lock(project.directory / "Eiffel.lock").package("moving").revision

    assert installed.exit_code == 0, installed.output
    assert unchanged == before
    assert updated.exit_code == 0, updated.output
    assert after != before


def test_git_monorepository_subdir_and_legacy_ecf_are_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "mono"
    package = repository / "packages" / "parser"
    create_project(ProjectCreationRequest(package, LIBRARY_TEMPLATE))
    _initialize_repository(repository)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "legacy.ecf").write_text(
        '<system xmlns="http://www.eiffel.com/developers/xml/configuration-1-23-0" '
        'name="legacy" uuid="00000000-0000-4000-8000-000000000001">'
        '<target name="default"><root all_classes="true"/></target></system>'
    )
    _initialize_repository(legacy)
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()

    mono = runner.invoke(
        main,
        [
            "add",
            "parser",
            "--git",
            str(repository),
            "--branch",
            "main",
            "--subdir",
            "packages/parser",
        ],
    )
    legacy_result = runner.invoke(
        main,
        [
            "add",
            "legacy",
            "--git",
            str(legacy),
            "--branch",
            "main",
            "--ecf",
            "legacy.ecf",
        ],
    )

    assert mono.exit_code == 0, mono.output
    assert legacy_result.exit_code == 0, legacy_result.output
    lock = load_lock(project.directory / "Eiffel.lock")
    assert lock.package("parser").subdir == "packages/parser"
    assert lock.package("legacy").manifest is None
    assert lock.package("legacy").version == "0.0.0"


def test_remove_preserves_manifest_lock_and_ecf_consistency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()
    assert runner.invoke(main, ["add", "shared", "--path", "../shared"]).exit_code == 0

    removed = runner.invoke(main, ["remove", "shared"])

    assert removed.exit_code == 0, removed.output
    assert load_manifest(project.manifest_path).dependencies == ()
    assert load_lock(project.directory / "Eiffel.lock").packages == ()
    assert not etree.parse(str(project.ecf_path)).xpath(
        "//*[local-name()='library'][@name='shared']"
    )


def test_clean_unused_removes_unreachable_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _git_library(tmp_path / "unused", version="1.0.0")
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()
    added = runner.invoke(
        main,
        ["add", "unused", "--git", str(repository), "--branch", "main"],
    )
    assert added.exit_code == 0, added.output
    package = load_lock(project.directory / "Eiffel.lock").package("unused")
    destination = project.directory / ".evm" / "deps" / package.materialized_name
    assert destination.is_dir()
    assert runner.invoke(main, ["remove", "unused"]).exit_code == 0

    cleaned = runner.invoke(main, ["clean", "--unused"])

    assert cleaned.exit_code == 0, cleaned.output
    assert not destination.exists()
    assert not any((project.directory / ".evm" / "sources" / "git").iterdir())


def test_manifest_lock_mismatch_is_rejected_before_install(tmp_path: Path) -> None:
    create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text() + '\n[dependencies]\nshared = { path = "../shared" }\n'
    )

    with pytest.raises(EvmError, match="inconsistent"):
        install_dependencies(load_manifest(manifest))


def test_local_patch_replaces_git_content_without_network(tmp_path: Path) -> None:
    create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        + '\n[dependencies]\nshared = { git = "https://example.invalid/shared.git", tag = "v1" }\n'
        + '\n[patch]\nshared = { path = "../shared" }\n'
    )
    project = load_manifest(manifest)

    lock = resolve_dependencies(project, offline=True)

    package = lock.package("shared")
    assert package.source == "git+https://example.invalid/shared.git"
    assert package.patched is True
    assert package.path == "../shared"


def test_development_dependency_is_only_added_to_development_target(
    tmp_path: Path,
) -> None:
    create_project(ProjectCreationRequest(tmp_path / "testing", LIBRARY_TEMPLATE))
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        + '\n[targets.test]\nextends = "default"\nroot = "APPLICATION.make"\nsources = ["tests"]\n'
        + '\n[dev-dependencies]\ntesting = { path = "../testing" }\n'
    )
    project = load_manifest(manifest)
    lock = resolve_dependencies(project)

    document = etree.fromstring(generate_ecf(project, lock))
    default_libraries = document.xpath(
        "./*[local-name()='target'][@name='default']/*[local-name()='library']/@name"
    )
    test_libraries = document.xpath(
        "./*[local-name()='target'][@name='test']/*[local-name()='library']/@name"
    )

    assert "testing" not in default_libraries
    assert "testing" in test_libraries


def _git_library(path: Path, *, version: str, tag: str | None = None) -> Path:
    create_project(ProjectCreationRequest(path, LIBRARY_TEMPLATE))
    manifest = path / "Eiffel.toml"
    manifest.write_text(manifest.read_text().replace('version = "0.1.0"', f'version = "{version}"'))
    _initialize_repository(path)
    if tag is not None:
        _git(path, "tag", tag)
    return path


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
