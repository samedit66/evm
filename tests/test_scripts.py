from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

from evm.dependencies import resolve_dependencies
from evm.ecf import generate_ecf
from evm.errors import EvmError
from evm.manifest import load_manifest
from evm.model import BuildRequest
from evm.project import create_project
from evm.scripts import ScriptRunRequest, _compile_script, prepare_script, run_script
from evm.toolchains import Toolchain
from evm.versioning import NumericVersion


def test_standalone_script_stages_only_explicit_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    root = tmp_path / "hello.e"
    helper = tmp_path / "library" / "helper.e"
    ignored = tmp_path / "ignored.e"
    helper.parent.mkdir()
    root.write_text(
        "#!/usr/bin/env -S evm run\nclass HELLO\ncreate execute\nfeature execute do end\nend\n"
    )
    helper.write_text("class HELPER end\n")
    ignored.write_text("class IGNORED end\n")
    monkeypatch.setenv("EVM_CACHE_DIR", str(cache))
    monkeypatch.chdir(tmp_path)

    prepared = prepare_script(ScriptRunRequest(sources=(root, helper), standalone=True))

    assert prepared.root.class_name == "HELLO"
    assert prepared.root.feature == "execute"
    assert prepared.source_project is None
    staged = sorted((prepared.cache_directory / "sources").rglob("*.e"))
    assert [path.name for path in staged] == ["hello.e", "helper.e"]
    assert staged[0].read_bytes().startswith(b"\nclass HELLO")
    assert not list(tmp_path.glob("*.ecf"))
    assert not (tmp_path / "Eiffel.toml").exists()
    assert not (tmp_path / "Eiffel.lock").exists()


def test_script_inherits_nearest_project_without_changing_ecf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "project")
    project.manifest_path.write_text(
        project.manifest_path.read_text()
        + '\n[toolchain]\ndefault = "gobo@26.06"\nmatrix = ["gobo@26.06"]\n'
    )
    project = load_manifest(project.manifest_path)
    tool = project.directory / "tools" / "report.e"
    tool.parent.mkdir()
    tool.write_text("class REPORT create make feature make do end end\n")
    original_ecf = project.ecf_path.read_bytes()
    monkeypatch.chdir(project.directory)

    prepared = prepare_script(ScriptRunRequest(sources=(tool,)))

    assert prepared.source_project == project
    assert prepared.cache_directory.is_relative_to(project.directory / ".evm" / "scripts")
    assert prepared.project.configuration_directory == project.directory
    assert prepared.project.state_directory == project.state_directory
    assert prepared.project.build_root == prepared.cache_directory / "build"
    assert prepared.project.toolchain == project.toolchain
    assert project.ecf_path.read_bytes() == original_ecf


def test_script_rejects_sources_from_different_project_contexts(tmp_path: Path) -> None:
    first_project = create_project(tmp_path / "first")
    second_project = create_project(tmp_path / "second")
    first = first_project.directory / "src" / "application.e"
    second = second_project.directory / "src" / "application.e"

    with pytest.raises(EvmError, match="different project contexts"):
        prepare_script(ScriptRunRequest(sources=(first, second)))


def test_script_ecf_keeps_path_dependencies_relative_to_cached_ecf(tmp_path: Path) -> None:
    dependency = create_project(tmp_path / "shared", library=True)
    project = create_project(tmp_path / "application")
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        + '\n[dependencies]\nshared = { path = "../shared", ecf = "shared.ecf" }\n'
    )
    project = load_manifest(manifest)
    lock = resolve_dependencies(project)
    script = project.directory / "tool.e"
    script.write_text("class TOOL create make feature make do end end\n")

    prepared = prepare_script(ScriptRunRequest(sources=(script,)))
    ecf = etree.fromstring(generate_ecf(prepared.project, lock))

    location = ecf.xpath("//*[local-name()='library'][@name='shared']")[0].get("location")
    resolved = (prepared.cache_directory / location).resolve()
    assert resolved == dependency.ecf_path


def test_script_rejects_duplicate_source(tmp_path: Path) -> None:
    source = tmp_path / "hello.e"
    source.write_text("class HELLO end\n")

    with pytest.raises(EvmError, match="more than once"):
        prepare_script(ScriptRunRequest(sources=(source, source), standalone=True))


def test_script_root_overrides_must_name_a_declared_class(tmp_path: Path) -> None:
    source = tmp_path / "hello.e"
    source.write_text("class HELLO create make feature make do end end\n")

    with pytest.raises(EvmError, match="was not found"):
        prepare_script(
            ScriptRunRequest(
                sources=(source,),
                class_name="OTHER",
                standalone=True,
            )
        )


def test_script_detects_creation_procedure_on_single_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "hello.e"
    source.write_text("class HELLO create make feature make do end end\n")
    monkeypatch.setenv("EVM_CACHE_DIR", str(tmp_path / "cache"))

    prepared = prepare_script(ScriptRunRequest(sources=(source,), standalone=True))

    assert prepared.root.feature == "make"


@pytest.mark.parametrize(
    ("sources", "message"),
    [
        ((), "at least one"),
        ((Path("hello.txt"),), "not an Eiffel source"),
        ((Path("missing.e"),), "not found"),
    ],
)
def test_script_validates_source_boundaries(
    tmp_path: Path,
    sources: tuple[Path, ...],
    message: str,
) -> None:
    resolved = tuple(tmp_path / source for source in sources)

    with pytest.raises(EvmError, match=message):
        prepare_script(ScriptRunRequest(sources=resolved, standalone=True))


def test_script_rejects_standalone_with_manifest(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    source = project.directory / "src" / "application.e"

    with pytest.raises(EvmError, match="cannot be used together"):
        prepare_script(
            ScriptRunRequest(
                sources=(source,),
                standalone=True,
                manifest_path=project.manifest_path,
            )
        )


def test_compile_script_materializes_ecf_and_finds_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "hello.e"
    source.write_text("class HELLO create make feature make do end end")
    monkeypatch.setenv("EVM_CACHE_DIR", str(tmp_path / "cache"))
    prepared = prepare_script(ScriptRunRequest(sources=(source,), standalone=True))
    toolchain = Toolchain(
        "gobo",
        tmp_path / "gec",
        NumericVersion.parse("26.06.30"),
        "explicit",
        "--toolchain",
    )
    executable = tmp_path / "application"
    executable.write_text("binary")
    monkeypatch.setattr("evm.scripts._prepare_script_dependencies", lambda *args: None)
    monkeypatch.setattr("evm.scripts.validate_configuration", lambda project: [])
    monkeypatch.setattr("evm.scripts.generate_ecf", lambda project, lock: b"<ecf/>")
    monkeypatch.setattr("evm.scripts.select_toolchain", lambda project, compiler: toolchain)
    monkeypatch.setattr(
        "evm.scripts.prepare_build_directory", lambda *args, **kwargs: tmp_path / "build"
    )
    monkeypatch.setattr("evm.scripts.compiler_command", lambda *args: ["gec"])
    monkeypatch.setattr("evm.scripts.run_compiler", lambda *args: None)
    monkeypatch.setattr("evm.scripts.artifact_candidates", lambda *args: (executable,))

    selected, result = _compile_script(prepared, BuildRequest(compiler="gobo"))

    assert selected == toolchain
    assert result == executable
    assert prepared.project.ecf_path.read_bytes() == b"<ecf/>"


def test_run_script_returns_application_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "hello.e"
    source.write_text("class HELLO create make feature make do end end")
    monkeypatch.setenv("EVM_CACHE_DIR", str(tmp_path / "cache"))
    prepared = prepare_script(ScriptRunRequest(sources=(source,), standalone=True))
    toolchain = Toolchain(
        "gobo",
        tmp_path / "gec",
        NumericVersion.parse("26.06"),
        "explicit",
        "--toolchain",
    )
    monkeypatch.setattr("evm.scripts.prepare_script", lambda request: prepared)
    monkeypatch.setattr(
        "evm.scripts._compile_script", lambda selected, request: (toolchain, tmp_path / "app")
    )
    monkeypatch.setattr(
        "evm.scripts.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=7)
    )

    assert run_script(ScriptRunRequest(sources=(source,), arguments=("arg",))) == 7


@pytest.mark.toolchain
@pytest.mark.integration
@pytest.mark.gobo
def test_standalone_script_runs_with_real_gobo_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("gec") is None or "GOBO" not in os.environ:
        pytest.skip("Gobo Eiffel is not configured")
    source = tmp_path / "hello.e"
    source.write_text('class HELLO create make feature make do print ("file mode%N") end end\n')
    monkeypatch.setenv("EVM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)

    exit_code = run_script(
        ScriptRunRequest(
            sources=(source,),
            compiler="gobo",
            standalone=True,
        )
    )

    assert exit_code == 0
