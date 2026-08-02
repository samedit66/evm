"""Contract tests for compiler-specific build translation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.model import BuildRequest
from evm.project.creation import ProjectCreationRequest, create_project
from evm.toolchain.compilers import (
    automatic_compiler_adapter_names,
    build_directory,
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


def test_serpent_adapter_maps_run_and_rejects_checks(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    toolchain = Toolchain(
        "serpent",
        Path("/tools/python3.13"),
        NumericVersion.parse("0.1.0"),
        "explicit",
        "test",
        "c95ab517",
    )
    adapter = compiler_adapter("serpent")

    command = adapter.run_command(toolchain, project, BuildRequest(), ("first", "second"))
    payload = json.loads(command[2])

    assert payload["operation"] == "run"
    assert payload["main_class"] == "APPLICATION"
    assert payload["arguments"] == ["first", "second"]
    assert adapter.artifact_candidates(toolchain, project, BuildRequest()) == (
        build_directory(toolchain, project, BuildRequest()),
    )
    assert adapter.legacy_ecf_variables(toolchain) == {}
    with pytest.raises(EvmError, match="configuration-only"):
        adapter.compiler_command(toolchain, project, BuildRequest(), check_only=True)


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        (("standard", "ise"), "ISE language semantics"),
        (("void-safety", "all"), "void-safety"),
    ],
)
def test_serpent_adapter_rejects_language_requirements(
    tmp_path: Path,
    requirements: tuple[str, str],
    message: str,
) -> None:
    project = replace(
        create_project(ProjectCreationRequest(tmp_path / message.replace(" ", "-"))),
        requires=(requirements,),
    )

    assert message in (compiler_adapter("serpent").compatibility_error(project) or "")


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
    directory = build_directory(toolchain, project, BuildRequest())
    directory.mkdir(parents=True)

    compiler_adapter("liberty").prepare_build(toolchain, project, BuildRequest(), directory)
    ace = (directory / "hello.ace").read_text()

    assert 'system\n   "hello"' in ace
    assert "root\n   APPLICATION: make" in ace
    assert str(project.directory / "src") in ace
    assert '"${path_liberty_core}/loadpath.se"' in ace


def test_liberty_adapter_builds_and_runs_native_artifact(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    toolchain = Toolchain(
        "liberty",
        Path("/tools/se"),
        NumericVersion.parse("0.0"),
        "explicit",
        "test",
        "21b0813",
    )
    request = BuildRequest(release=True)
    adapter = compiler_adapter("liberty")
    output = build_directory(toolchain, project, request)
    output.mkdir(parents=True)
    adapter.prepare_build(toolchain, project, request, output)
    executable = adapter.artifact_candidates(toolchain, project, request)[0]
    executable.write_text("binary")

    assert "assertion (boost)" in (output / "hello.ace").read_text()
    assert adapter.compiler_command(toolchain, project, request) == [
        "/tools/se",
        "c",
        str(output / "hello.ace"),
    ]
    assert adapter.run_command(toolchain, project, request, ("argument",)) == [
        str(executable),
        "argument",
    ]
    assert adapter.legacy_ecf_variables(toolchain) == {}
    with pytest.raises(EvmError, match="configuration-only"):
        adapter.compiler_command(toolchain, project, request, check_only=True)


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        (("standard", "ise"), "ISE language semantics"),
        (("concurrency", "thread"), "concurrency"),
        (("void-safety", "all"), "void-safety"),
    ],
)
def test_liberty_adapter_rejects_language_requirements(
    tmp_path: Path,
    requirements: tuple[str, str],
    message: str,
) -> None:
    project = replace(
        create_project(ProjectCreationRequest(tmp_path / message.replace(" ", "-"))),
        requires=(requirements,),
    )

    assert message in (compiler_adapter("liberty").compatibility_error(project) or "")
