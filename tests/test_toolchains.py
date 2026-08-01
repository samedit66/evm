from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from evm.errors import EvmError
from evm.model import BuildRequest, CompilerRequirement
from evm.project import create_project
from evm.toolchains import (
    Detection,
    Toolchain,
    artifact_candidates,
    compiler_command,
    detect,
    prepare_build_directory,
    run_compiler,
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


def test_ise_workbench_artifacts_include_driver(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    toolchain = Toolchain(
        "ise",
        Path("/bin/ec"),
        NumericVersion.parse("25.12"),
        "explicit",
        "--compiler",
    )

    candidates = artifact_candidates(toolchain, project, BuildRequest())

    assert candidates[1].name in {"driver", "driver.exe"}


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
    stale_file = project.directory / "build" / "ise" / "25.12" / "default" / "dev" / "stale"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale")

    directory = prepare_build_directory(toolchain, project, request)

    assert directory.is_dir()
    assert not stale_file.exists()


def test_detection_reports_missing_failed_and_unversioned_compilers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchains.shutil.which", lambda name: None)
    assert detect("gobo").error == "gec was not found in PATH"

    monkeypatch.setattr("evm.toolchains.shutil.which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(
        "evm.toolchains.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="failure"),
    )
    assert detect("ise").error == "version command exited with 2"

    monkeypatch.setattr(
        "evm.toolchains.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="development", stderr=""),
    )
    assert detect("gobo").error == "numeric version was not reported"


def test_detection_parses_compiler_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evm.toolchains.shutil.which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(
        "evm.toolchains.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Gobo Eiffel Compiler 26.06.30",
            stderr="",
        ),
    )

    detection = detect("gobo")

    assert detection.executable == Path("/tools/gec")
    assert detection.version == NumericVersion.parse("26.06.30")


def test_selection_reports_all_rejection_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = replace(
        create_project(tmp_path / "hello"),
        compilers=(CompilerRequirement("gobo", ">=99"),),
    )
    monkeypatch.setattr(
        "evm.toolchains.detect_all",
        lambda: {
            "ise": Detection(None, None, "missing"),
            "gobo": Detection(Path("/tools/gec"), NumericVersion.parse("26.06"), None),
        },
    )

    with pytest.raises(EvmError, match=r"gobo: version 26\.06 does not satisfy >=99"):
        select_toolchain(project)


def test_selection_enforces_capabilities_and_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = create_project(tmp_path / "hello")
    detections = {
        "ise": Detection(Path("/tools/ec"), NumericVersion.parse("25.12"), None),
        "gobo": Detection(Path("/tools/gec"), NumericVersion.parse("26.06"), None),
    }
    monkeypatch.setattr("evm.toolchains.detect_all", lambda: detections)
    scoop = replace(base, requires=(("concurrency", "scoop"),))
    with pytest.raises(EvmError, match="unsupported"):
        select_toolchain(scoop, "gobo")

    restricted = replace(base, compilers=(CompilerRequirement("ise", ">=25"),))
    with pytest.raises(EvmError, match="not allowed"):
        select_toolchain(restricted, "gobo")

    semantics = replace(base, requires=(("ise-semantics", "26.01"),))
    with pytest.raises(EvmError, match="older than required"):
        select_toolchain(semantics, "ise")


def test_run_compiler_sets_runtime_environment_and_reports_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def run(command: list[str], **options: object) -> SimpleNamespace:
        observed.update(options)
        return SimpleNamespace(returncode=3, stdout="compile output", stderr="compile error")

    monkeypatch.setattr("evm.toolchains.subprocess.run", run)
    monkeypatch.setattr("evm.toolchains.toolchain_environment_values", lambda command: ())

    with pytest.raises(EvmError, match=r"compile output[\s\S]*compile error"):
        run_compiler(["/tools/gec.exe", "project.ecf"], tmp_path, capture_output=True)

    environment = observed["env"]
    assert isinstance(environment, dict)
    assert environment["evm_compiler"] == "gobo"
    assert environment["ZIG_LOCAL_CACHE_DIR"].endswith(".zig-local-cache")


def test_compiler_commands_cover_check_library_and_capability_modes(tmp_path: Path) -> None:
    application = replace(
        create_project(tmp_path / "app"),
        requires=(("ise-semantics", "25.12"), ("void-safety", "all")),
        compiler_arguments={"gobo": ("--cc=zig",), "ise": ("-keep",)},
    )
    library = create_project(tmp_path / "library", library=True)
    ise = Toolchain("ise", Path("/tools/ec"), NumericVersion.parse("25.12"), "explicit", "x")
    gobo = Toolchain("gobo", Path("/tools/gec"), NumericVersion.parse("26.06"), "explicit", "x")

    assert "-melt" in compiler_command(ise, application, BuildRequest(), check_only=True)
    assert "-precompile" in compiler_command(ise, library, BuildRequest())
    assert "-keep" in compiler_command(ise, application, BuildRequest())
    gobo_command = compiler_command(gobo, application, BuildRequest())
    assert "--ise=25.12" in gobo_command
    assert "--capability=void_safety=all" in gobo_command
    assert "--cc=zig" in gobo_command
