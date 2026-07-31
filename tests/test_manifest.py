from __future__ import annotations

from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.manifest import load_manifest, parse_manifest
from evm.project import (
    create_project,
    effective_sources,
    target_chain,
    validate_configuration,
)


def test_manifest_rejects_unknown_requires_key(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text() + '\n[requires]\ndebugger = "yes"\n')

    with pytest.raises(EvmError, match="allowed keys"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("section", "declaration"),
    [
        ("dependencies", 'base = { source = "ise" }'),
        ("dependencies", 'eiffel_base = { source = "ise", library = "base" }'),
        ("dependencies", 'free_elks = { source = "gobo", library = "free_elks" }'),
        ("dependencies", 'runtime = { source = "gobo", library = "free_elks" }'),
        ("dev-dependencies", 'runtime = { source = "ise", library = "base" }'),
    ],
)
def test_manifest_rejects_implicit_runtime_dependencies(
    tmp_path: Path,
    section: str,
    declaration: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[{section}]\n{declaration}\n"
    )

    with pytest.raises(EvmError, match=r"runtime library .* EVM provides automatically"):
        load_manifest(project.manifest_path)


@pytest.mark.parametrize(
    "declaration",
    [
        'base = { git = "https://example.invalid/base.git", tag = "v1" }',
        'base = { source = "ise", library = "time" }',
        'free_elks = { source = "gobo", library = "xml" }',
    ],
)
def test_manifest_rejects_reserved_runtime_group_names(
    tmp_path: Path,
    declaration: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[dependencies]\n{declaration}\n"
    )

    with pytest.raises(EvmError, match=r"dependency name .* is reserved"):
        load_manifest(project.manifest_path)


@pytest.mark.parametrize(
    "declaration",
    [
        'foundation = { git = "https://example.invalid/base.git", tag = "v1" }',
        'time_adapter = { source = "ise", library = "time" }',
        'xml_adapter = { source = "gobo", library = "xml" }',
    ],
)
def test_manifest_allows_non_runtime_dependency_aliases(
    tmp_path: Path,
    declaration: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[dependencies]\n{declaration}\n"
    )

    loaded = load_manifest(project.manifest_path)

    assert len(loaded.dependencies) == 1


@pytest.mark.parametrize("constraint", ["^25.12", "~25.12", "25.12", ">=25.x"])
def test_manifest_rejects_invalid_version_constraints(tmp_path: Path, constraint: str) -> None:
    project = create_project(tmp_path / constraint.replace(".", "_").replace(">", "x"))
    path = project.manifest_path
    path.write_text(path.read_text() + f'\n[compatibility]\ncompilers = ["ise {constraint}"]\n')

    with pytest.raises(EvmError, match="invalid version constraint"):
        load_manifest(path)


def test_target_inheritance_is_normalized(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text()
        + '\n[targets.server]\nextends = "default"\n'
        + 'root = "SERVER.start"\nsources = ["server"]\n'
    )
    (project.directory / "server").mkdir()

    loaded = load_manifest(path)

    assert [item.name for item in target_chain(loaded, "server")] == [
        "default",
        "server",
    ]
    assert effective_sources(loaded, "server") == ("src", "server")
    assert validate_configuration(loaded) == []


def test_target_cycle_is_rejected(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text() + '\n[targets.a]\nextends = "b"\n' + '\n[targets.b]\nextends = "a"\n'
    )

    with pytest.raises(EvmError, match="cycle"):
        load_manifest(path)


def test_condition_values_are_closed_where_portability_requires_it(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text() + '\n[[conditions]]\nwhen = { compiler = "gec" }\n' + 'sources = ["src"]\n'
    )

    with pytest.raises(EvmError, match="allowed: gobo, ise"):
        load_manifest(path)


def test_parse_manifest_reports_invalid_toml_with_source_path(tmp_path: Path) -> None:
    manifest = tmp_path / "Eiffel.toml"

    with pytest.raises(EvmError, match=r"cannot parse .*Eiffel\.toml"):
        parse_manifest("[project", manifest)


def test_managed_ecf_must_stay_inside_project(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text().replace('ecf = "hello.ecf"', 'ecf = "../outside.ecf"'))

    with pytest.raises(EvmError, match="must stay inside"):
        load_manifest(path)


def test_ecf_include_must_stay_inside_project(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text() + '\n[ecf]\ninclude = ["../outside.xml"]\n')

    with pytest.raises(EvmError, match=r"ecf\.include\[0\] must stay inside"):
        load_manifest(path)


def test_release_is_a_valid_explicit_target_name(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text() + '\n[targets.release]\nextends = "default"\n')

    loaded = load_manifest(path)

    assert loaded.target("release").extends == "default"
