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
