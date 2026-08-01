from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from evm.cli import main
from evm.discovery import discover_environment, discovery_lines
from evm.toolchain_store import save_managed_installation
from evm.toolchain_types import InstallationKind, ToolchainInstallation, current_toolchain_platform


def _executable(directory: Path, name: str, output: str, exit_code: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text(f"#!/bin/sh\necho '{output}' >&2\nexit {exit_code}\n")
    executable.chmod(0o755)
    return executable


def test_discovers_eiffel_and_c_compilers_from_path(tmp_path: Path) -> None:
    bin_directory = tmp_path / "bin"
    ec = _executable(bin_directory, "ec", "EiffelStudio 25.12")
    gcc = _executable(bin_directory, "gcc", "gcc 15.1.0")

    result = discover_environment({"PATH": str(bin_directory)}, "Linux")

    assert [(item.component_id, item.version) for item in result.components] == [
        ("ise-ec", "25.12"),
        ("gcc", "15.1.0"),
    ]
    assert result.components[0].path == ec
    assert result.components[1].path == gcc
    assert all(item.active for item in result.components)


def test_reports_all_path_matches_and_marks_later_candidate_shadowed(
    tmp_path: Path,
) -> None:
    first = _executable(tmp_path / "first", "clang", "clang 19.1")
    second = _executable(tmp_path / "second", "clang", "clang 20.0")
    environment = {"PATH": os.pathsep.join((str(first.parent), str(second.parent)))}

    result = discover_environment(environment, "Linux")

    clang = [item for item in result.components if item.component_id == "clang"]
    assert [(item.path, item.active) for item in clang] == [(first, True), (second, False)]


def test_merges_path_and_environment_matches_for_gobo(tmp_path: Path) -> None:
    gobo = tmp_path / "gobo"
    gec = _executable(gobo / "bin", "gec", "Gobo Eiffel Compiler 26.07")
    environment = {"PATH": str(gobo / "bin"), "GOBO": str(gobo)}

    result = discover_environment(environment, "Linux")

    component = next(item for item in result.components if item.component_id == "gobo-gec")
    assert component.path == gec
    assert component.sources == ("environment", "path")
    assert component.active


def test_discovers_evm_managed_toolchain_without_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(store))
    platform = current_toolchain_platform()
    root = store / "managed" / "gobo" / "26.06.30" / platform.identifier / "root"
    executable = _executable(root / "bin", "gec", "Gobo Eiffel Compiler 26.06.30")
    save_managed_installation(
        ToolchainInstallation(
            "gobo",
            "26.06",
            "26.06.30",
            platform,
            root,
            executable,
            InstallationKind.MANAGED,
        )
    )

    result = discover_environment(
        {
            "PATH": str(tmp_path / "empty"),
            "EVM_TOOLCHAIN_HOME": str(store),
            "EVM_TOOLCHAIN": "gobo@26.06",
        },
        platform.operating_system.title(),
    )

    component = next(item for item in result.components if item.component_id == "gobo-gec")
    assert component.path == executable
    assert component.sources == ("evm-managed:gobo@26.06",)
    assert component.active


def test_deduplicates_symlink_to_same_executable(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "real", "ec", "EiffelStudio 25.12")
    link_directory = tmp_path / "link"
    link_directory.mkdir()
    (link_directory / "ec").symlink_to(executable)
    environment = {"PATH": os.pathsep.join((str(link_directory), str(executable.parent)))}

    result = discover_environment(environment, "Linux")

    components = [item for item in result.components if item.component_id == "ise-ec"]
    assert len(components) == 1
    assert components[0].path == link_directory / "ec"
    assert components[0].resolved_path == executable


def test_accepts_version_output_from_failed_command(tmp_path: Path) -> None:
    compiler = _executable(tmp_path, "clang", "clang version 19.44", exit_code=2)

    result = discover_environment({"PATH": str(tmp_path)}, "Linux")

    component = next(item for item in result.components if item.path == compiler)
    assert component.version == "19.44"
    assert component.availability == "available"
    assert component.diagnostics == ()


def test_classifies_apple_clang_from_version_output(tmp_path: Path) -> None:
    _executable(tmp_path, "gcc", "Apple clang version 17.0.0")

    result = discover_environment({"PATH": str(tmp_path)}, "Darwin")

    component = next(item for item in result.components if item.path == tmp_path / "gcc")
    assert component.component_id == "apple-clang"
    assert component.display_name == "Apple Clang"
    assert component.family == "apple-clang"


def test_reports_unparseable_version_without_aborting(tmp_path: Path) -> None:
    _executable(tmp_path, "gec", "Gobo development build")

    result = discover_environment({"PATH": str(tmp_path)}, "Linux")

    component = result.components[0]
    assert component.availability == "unverified"
    assert component.diagnostics == ("numeric version was not reported",)


def test_windows_discovery_uses_pathext_case_insensitively(tmp_path: Path) -> None:
    compiler = tmp_path / "clang.exe"
    compiler.write_text("not a native executable")

    result = discover_environment(
        {"PATH": str(tmp_path), "PATHEXT": ".EXE;.CMD"},
        "Windows",
    )

    component = next(item for item in result.components if item.component_id == "clang")
    assert component.path == compiler
    assert component.active
    assert component.availability == "unverified"


def test_macos_discovers_active_developer_clang_through_xcrun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clang = _executable(tmp_path, "clang", "Apple clang version 17.0.0")
    monkeypatch.setattr("evm.discovery.shutil.which", lambda name: f"/usr/bin/{name}")

    def run(command: list[str], **_options: object) -> SimpleNamespace:
        if "--find" in command:
            return SimpleNamespace(returncode=0, stdout=str(clang), stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="Apple clang version 17.0.0",
            stderr="",
        )

    monkeypatch.setattr("evm.discovery.subprocess.run", run)

    result = discover_environment({"PATH": str(tmp_path / "empty")}, "Darwin")

    component = result.components[0]
    assert component.component_id == "apple-clang"
    assert component.sources == ("xcrun",)
    assert not component.active


def test_windows_discovers_visual_studio_outside_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installation = tmp_path / "Visual Studio"
    compiler = (
        installation / "VC" / "Tools" / "MSVC" / "14.44" / "bin" / "Hostx64" / "x64" / "cl.exe"
    )
    compiler.parent.mkdir(parents=True)
    compiler.write_text("compiler")
    older_installation = tmp_path / "Visual Studio Build Tools"
    older_compiler = (
        older_installation
        / "VC"
        / "Tools"
        / "MSVC"
        / "14.42"
        / "bin"
        / "Hostx64"
        / "x64"
        / "cl.exe"
    )
    older_compiler.parent.mkdir(parents=True)
    older_compiler.write_text("compiler")
    monkeypatch.setattr(
        "evm.discovery.shutil.which",
        lambda name, **_options: "C:/tools/vswhere.exe" if name == "vswhere" else None,
    )

    def run(command: list[str], **_options: object) -> SimpleNamespace:
        if "-property" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{installation}\n{older_installation}\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="Microsoft C/C++ Compiler Version 19.44.0",
        )

    monkeypatch.setattr("evm.discovery.subprocess.run", run)

    result = discover_environment({"PATH": ""}, "Windows")

    components = [item for item in result.components if item.component_id == "msvc-cl"]
    component = next(item for item in components if item.path == compiler)
    assert {item.path for item in components} == {compiler, older_compiler}
    assert component.path == compiler
    assert component.version == "19.44.0"
    assert component.sources == ("visual-studio",)
    assert not component.active


def test_version_timeout_does_not_abort_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _executable(tmp_path, "ec", "EiffelStudio 25.12")

    def timeout(*_arguments: object, **_options: object) -> None:
        raise subprocess.TimeoutExpired("ec", 5)

    monkeypatch.setattr("evm.discovery.subprocess.run", timeout)

    result = discover_environment({"PATH": str(tmp_path)}, "Linux")

    assert result.components[0].availability == "unverified"
    assert result.components[0].diagnostics == ("version detection timed out",)


def test_empty_discovery_is_successful_and_json_is_stable(tmp_path: Path) -> None:
    environment = {"PATH": str(tmp_path), "GOBO": str(tmp_path / "missing")}

    result = discover_environment(environment, "Linux")
    data = result.to_dict()

    assert data["schema_version"] == 1
    assert data["components"] == []
    assert data["environment"][3] == {
        "name": "GOBO",
        "defined": True,
        "valid": False,
    }
    assert str(tmp_path / "missing") not in json.dumps(data)
    assert "none found" in "\n".join(discovery_lines(result))


def test_discovery_ignores_deprecated_compiler_environment_variable(tmp_path: Path) -> None:
    result = discover_environment(
        {"PATH": str(tmp_path), "EVM_COMPILER": "gobo"},
        "Linux",
    )

    assert "EVM_COMPILER" not in {variable.name for variable in result.environment}


def test_discover_cli_emits_structured_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = discover_environment({"PATH": str(tmp_path)}, "Linux")
    monkeypatch.setattr("evm.cli.discover_environment", lambda: result)

    invocation = CliRunner().invoke(main, ["discover", "--json"])

    assert invocation.exit_code == 0, invocation.output
    assert json.loads(invocation.output)["schema_version"] == 1
