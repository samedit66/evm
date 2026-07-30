from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.model import BuildRequest, CompilerRequirement
from evm.project import create_project
from evm.toolchains import (
    Detection,
    Toolchain,
    compiler_command,
    prepare_build_directory,
    select_toolchain,
)
from evm.versioning import NumericVersion, satisfies


def test_numeric_version_constraints() -> None:
    version = NumericVersion.parse("25.12.9")

    assert satisfies(version, ">=25.12,<26")
    assert not satisfies(version, "=25.12")
    assert NumericVersion.parse("25.12") < version


def test_explicit_compiler_wins_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_project(tmp_path / "hello")
    detections = {
        "ise": Detection(Path("/bin/ec"), NumericVersion.parse("25.12"), None),
        "gobo": Detection(
            Path("/bin/gec"),
            NumericVersion.parse("26.06"),
            None,
        ),
    }
    monkeypatch.setenv("EVM_COMPILER", "gobo")
    monkeypatch.setattr("evm.toolchains.detect_all", lambda: detections)

    selected = select_toolchain(project, "ise")

    assert selected.adapter == "ise"
    assert selected.selection == "explicit"


def test_compatibility_order_controls_automatic_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create_project(tmp_path / "hello")
    project = replace(
        project,
        compilers=(
            CompilerRequirement("gobo", ">=26"),
            CompilerRequirement("ise", ">=25"),
        ),
    )
    detections = {
        "ise": Detection(Path("/bin/ec"), NumericVersion.parse("25.12"), None),
        "gobo": Detection(
            Path("/bin/gec"),
            NumericVersion.parse("26.06"),
            None,
        ),
    }
    monkeypatch.delenv("EVM_COMPILER", raising=False)
    monkeypatch.setattr("evm.toolchains.detect_all", lambda: detections)

    assert select_toolchain(project).adapter == "gobo"


def test_automatic_selection_skips_incompatible_preferred_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = replace(
        create_project(tmp_path / "hello"),
        compilers=(
            CompilerRequirement("gobo", ">=99"),
            CompilerRequirement("ise", ">=25"),
        ),
    )
    detections = {
        "ise": Detection(Path("/bin/ec"), NumericVersion.parse("25.12"), None),
        "gobo": Detection(Path("/bin/gec"), NumericVersion.parse("26.06"), None),
    }
    monkeypatch.delenv("EVM_COMPILER", raising=False)
    monkeypatch.setattr("evm.toolchains.detect_all", lambda: detections)

    selected = select_toolchain(project)

    assert selected.adapter == "ise"


def test_unknown_compiler_id_is_rejected(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")

    with pytest.raises(EvmError, match="known adapters: ise, gobo"):
        select_toolchain(project, "gec")


def test_release_mode_maps_to_each_compiler(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    ise = Toolchain(
        "ise",
        Path("/bin/ec"),
        NumericVersion.parse("25.12"),
        "explicit",
        "--compiler",
    )
    gobo = Toolchain(
        "gobo",
        Path("/bin/gec"),
        NumericVersion.parse("26.06"),
        "explicit",
        "--compiler",
    )

    request = BuildRequest(release=True)
    ise_command = compiler_command(ise, project, request)
    gobo_command = compiler_command(gobo, project, request)

    assert "-finalize" in ise_command
    assert "-c_compile" in ise_command
    assert "--finalize" in gobo_command
    assert "--variable=evm_compiler=gobo" in gobo_command


def test_compiler_command_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    toolchain = Toolchain(
        "gobo",
        Path("/bin/gec"),
        NumericVersion.parse("26.06"),
        "explicit",
        "--compiler",
    )
    request = BuildRequest()

    compiler_command(toolchain, project, request)

    assert not (project.directory / "build").exists()


def test_ise_library_preparation_removes_stale_precompile(tmp_path: Path) -> None:
    project = create_project(tmp_path / "library", library=True)
    toolchain = Toolchain(
        "ise",
        Path("/bin/ec"),
        NumericVersion.parse("25.12"),
        "explicit",
        "--compiler",
    )
    request = BuildRequest()
    stale_file = project.directory / "build" / "ise" / "default" / "dev" / "stale"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale")

    directory = prepare_build_directory(toolchain, project, request)

    assert directory.is_dir()
    assert not stale_file.exists()
