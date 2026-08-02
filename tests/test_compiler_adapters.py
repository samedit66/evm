"""Contract tests for compiler-specific build translation."""

from __future__ import annotations

from pathlib import Path

import pytest

from evm.compiler_adapters import compiler_adapter, compiler_adapter_names
from evm.errors import EvmError
from evm.model import BuildRequest
from evm.project.creation import ProjectCreationRequest, create_project
from evm.toolchains import Toolchain
from evm.versioning import NumericVersion


def test_registry_preserves_automatic_selection_priority() -> None:
    assert compiler_adapter_names() == ("ise", "gobo")


def test_registry_rejects_unknown_adapter() -> None:
    with pytest.raises(EvmError, match="known adapters: ise, gobo"):
        compiler_adapter("serpent")


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
