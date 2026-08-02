from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from evm.errors import EvmError
from evm.model import BuildRequest
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import LIBRARY_TEMPLATE
from evm.project.workflow import (
    compile_project,
    prepare_compilation_project,
    run_project,
)
from evm.toolchain.selection import Toolchain
from evm.versioning import NumericVersion


def _toolchain(adapter: str = "gobo") -> Toolchain:
    executable = Path("/tools/gec" if adapter == "gobo" else "/tools/ec")
    return Toolchain(adapter, executable, NumericVersion.parse("26.06"), "explicit", "test")


def test_compile_project_orchestrates_preparation_and_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    selected = _toolchain()
    build_directory = tmp_path / "build"
    calls: list[object] = []
    monkeypatch.setattr(
        "evm.project.workflow.prepare_project",
        lambda *args, **kwargs: calls.append(("prepare", kwargs)),
    )
    monkeypatch.setattr("evm.project.workflow.select_toolchain", lambda value, compiler: selected)
    monkeypatch.setattr(
        "evm.project.workflow.prepare_build_directory",
        lambda *args, **kwargs: build_directory,
    )
    monkeypatch.setattr(
        "evm.project.workflow.compiler_command",
        lambda *args, **kwargs: ["/tools/gec", "hello.ecf"],
    )
    monkeypatch.setattr(
        "evm.project.workflow.run_compiler",
        lambda command, directory, capture_output: calls.append(
            ("run", command, directory, capture_output)
        ),
    )

    result = compile_project(
        project,
        BuildRequest(compiler="gobo", release=True, offline=True),
        check_only=True,
        announce=False,
    )

    assert result == selected
    assert calls[0][0] == "prepare"
    assert calls[1] == ("run", ["/tools/gec", "hello.ecf"], build_directory, True)


def test_compile_project_rejects_unknown_target(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))

    with pytest.raises(EvmError, match="unknown target"):
        compile_project(project, BuildRequest(target="missing"))


def test_run_project_executes_adapter_run_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    selected = _toolchain()
    executable = tmp_path / "hello-executable"
    executable.write_text("binary")
    observed: list[object] = []
    monkeypatch.setattr("evm.project.workflow.compile_project", lambda *args, **kwargs: selected)
    monkeypatch.setattr(
        "evm.project.workflow.run_command",
        lambda *args: [str(executable), "--verbose"],
    )
    monkeypatch.setattr(
        "evm.project.workflow.subprocess.run",
        lambda command, cwd: observed.append((command, cwd)) or SimpleNamespace(returncode=7),
    )

    status = run_project(project, BuildRequest(), ("--verbose",))

    assert status == 7
    assert observed == [([str(executable), "--verbose"], project.directory)]


def test_run_project_rejects_libraries_and_adapter_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = create_project(ProjectCreationRequest(tmp_path / "library", LIBRARY_TEMPLATE))
    with pytest.raises(EvmError, match="type 'application'"):
        run_project(library, BuildRequest(), ())

    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.setattr(
        "evm.project.workflow.compile_project", lambda *args, **kwargs: _toolchain()
    )

    def reject_run(*args: object) -> list[str]:
        raise EvmError("build succeeded but executable was not found")

    monkeypatch.setattr("evm.project.workflow.run_command", reject_run)
    with pytest.raises(EvmError, match="executable was not found"):
        run_project(project, BuildRequest(), ())


def test_legacy_compilation_project_uses_prepared_ecf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = replace(create_project(ProjectCreationRequest(tmp_path / "hello")), ecf_managed=False)
    prepared = tmp_path / "prepared.ecf"
    monkeypatch.setattr(
        "evm.project.workflow.prepare_legacy_ecf", lambda value, variables: prepared
    )
    monkeypatch.setenv("ISE_LIBRARY", str(tmp_path / "ise-library"))

    result = prepare_compilation_project(project, _toolchain("ise"))

    assert result.ecf_path == prepared
    assert prepare_compilation_project(replace(project, ecf_managed=True), _toolchain()) == replace(
        project, ecf_managed=True
    )
