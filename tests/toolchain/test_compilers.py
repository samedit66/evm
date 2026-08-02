"""Contract tests for compiler-specific build translation."""

from __future__ import annotations

from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.model import BuildRequest
from evm.project.creation import ProjectCreationRequest, create_project
from evm.toolchain.compilers import (
    automatic_compiler_adapter_names,
    compiler_adapter,
    compiler_adapter_names,
)
from evm.toolchain.selection import Toolchain
from evm.versioning import NumericVersion


def test_registry_preserves_automatic_selection_priority() -> None:
    assert compiler_adapter_names() == ("ise", "gobo", "serpent", "liberty")
    assert automatic_compiler_adapter_names() == ("ise", "gobo")


def test_registry_rejects_unknown_adapter() -> None:
    with pytest.raises(EvmError, match="known adapters: ise, gobo, serpent, liberty"):
        compiler_adapter("unknown")


def test_each_adapter_creates_a_command_for_its_executable(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    request = BuildRequest()

    for name, executable in (("ise", "/tools/ec"), ("gobo", "/tools/gec")):
        toolchain = Toolchain(
            name,
            Path(executable),
            NumericVersion.parse("26.01"),
            "explicit",
            "test",
        )

        command = compiler_adapter(name).compiler_command(toolchain, project, request)

        assert command[0] == executable


def test_gobo_adapter_reports_unsupported_scoop(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello", scoop=True))

    error = compiler_adapter("gobo").compatibility_error(project)

    assert error == "required capability concurrency=scoop is unsupported"


def test_serpent_adapter_builds_worker_request(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    toolchain = Toolchain(
        "serpent",
        Path("/tools/python3.13"),
        NumericVersion.parse("0.1.0"),
        "explicit",
        "test",
        "c95ab517",
    )

    command = compiler_adapter("serpent").compiler_command(toolchain, project, BuildRequest())

    assert command[0] == "/tools/python3.13"
    assert '"operation": "build"' in command[2]
    assert '"main_class": "APPLICATION"' in command[2]
    assert "build/serpent/c95ab517/default/dev" in command[2]


def test_serpent_adapter_rejects_unsupported_capabilities(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello", scoop=True))

    error = compiler_adapter("serpent").compatibility_error(project)

    assert error == "Serpent does not support the requested concurrency capability"


def test_liberty_adapter_generates_ace_configuration(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    toolchain = Toolchain(
        "liberty",
        Path("/tools/se"),
        NumericVersion.parse("0.0"),
        "explicit",
        "test",
        "21b0813",
    )
    directory = project.directory / "build" / "liberty"
    directory.mkdir(parents=True)

    compiler_adapter("liberty").prepare_build(toolchain, project, BuildRequest(), directory)
    ace = (directory / "hello.ace").read_text()

    assert 'system\n   "hello"' in ace
    assert "root\n   APPLICATION: make" in ace
    assert str(project.directory / "src") in ace
    assert '"${path_liberty_core}/loadpath.se"' in ace
