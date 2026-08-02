from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evm.documentation import (
    DocumentationRequest,
    _documentation_command,
    document_project,
)
from evm.errors import EvmError
from evm.manifest import parse_manifest
from evm.toolchains import Toolchain
from evm.versioning import NumericVersion


def _project(tmp_path: Path):
    return parse_manifest(
        """
[project]
name = "sample"
version = "1.0.0"
type = "library"
uuid = "00000000-0000-4000-8000-000000000000"
default-target = "default"
ecf = "sample.ecf"
ecf-managed = true

[sources]
clusters = ["src"]
""",
        tmp_path / "Eiffel.toml",
    )


def _toolchain(adapter: str) -> Toolchain:
    executable = Path("/tools/ec" if adapter == "ise" else "/tools/gec")
    version = NumericVersion.parse("25.12" if adapter == "ise" else "26.07")
    return Toolchain(adapter, executable, version, "explicit", "test")


def test_gedoc_command_generates_html_in_requested_directory(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    output = tmp_path / "public"
    monkeypatch.setattr(
        "evm.documentation.companion_tool", lambda selected, name: Path("/tools/gedoc")
    )

    command = _documentation_command(
        project,
        _toolchain("gobo"),
        "gedoc",
        "default",
        output,
    )

    assert command == [
        "/tools/gedoc",
        str(project.ecf_path),
        "--format=html_ise_stylesheet",
        "--target=default",
        f"--output={output}",
        "--force",
    ]


def test_gedoc_uses_gobo_tool_with_ise_semantics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("evm.documentation.installed_gobo_tool", lambda name: Path("/gobo/gedoc"))

    command = _documentation_command(
        _project(tmp_path),
        _toolchain("ise"),
        "gedoc",
        "default",
        tmp_path / "doc",
    )

    assert command[0] == "/gobo/gedoc"
    assert command[-1] == "--ise=25.12"


def test_eiffelstudio_documentation_command_uses_native_html_filter(tmp_path: Path) -> None:
    output = tmp_path / "doc"

    command = _documentation_command(
        _project(tmp_path),
        _toolchain("ise"),
        "eiffelstudio",
        "default",
        output,
    )

    assert command[-3:] == ["-filter", "html-stylesheet", "-all"]
    assert command[command.index("-project_path") + 1] == str(output)


def test_eiffelstudio_documentation_rejects_gobo_toolchain(tmp_path: Path) -> None:
    with pytest.raises(EvmError, match="requires an ISE Eiffel toolchain"):
        _documentation_command(
            _project(tmp_path),
            _toolchain("gobo"),
            "eiffelstudio",
            "default",
            tmp_path / "doc",
        )


def test_document_project_creates_default_output_and_preserves_output(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    toolchain = _toolchain("ise")
    monkeypatch.setattr("evm.documentation.select_toolchain", lambda project, compiler: toolchain)
    monkeypatch.setattr("evm.documentation.prepare_project", lambda *args, **kwargs: None)
    monkeypatch.setattr("evm.documentation.toolchain_environment_values", lambda command: ())
    monkeypatch.setattr(
        "evm.documentation.subprocess.run",
        lambda command, **options: SimpleNamespace(returncode=0, stdout="done\n", stderr=""),
    )

    result = document_project(project, DocumentationRequest())

    assert result.status == "passed"
    assert result.backend == "eiffelstudio"
    assert result.output_directory == tmp_path / "build" / "doc" / "default" / "Documentation"
    assert result.output_directory.parent.is_dir()
    assert result.as_dict()["stdout"] == "done\n"


def test_document_project_rejects_unknown_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "evm.documentation.select_toolchain", lambda project, compiler: _toolchain("ise")
    )

    with pytest.raises(EvmError, match="backend must be one of"):
        document_project(_project(tmp_path), DocumentationRequest(backend="unknown"))
