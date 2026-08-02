from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from evm.dependencies.resolution import install_dependencies, resolve_dependencies
from evm.manifest import load_manifest
from evm.model import BuildRequest
from evm.operations.testing import TestRequest as WorkflowTestRequest
from evm.operations.testing import test_project as run_project_tests
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.workflow import compile_project


@pytest.mark.toolchain
@pytest.mark.integration
@pytest.mark.parametrize(
    ("adapter", "executable"),
    [
        pytest.param("ise", "ec", marks=pytest.mark.ise),
        pytest.param("gobo", "gec", marks=pytest.mark.gobo),
    ],
)
def test_generated_application_builds_with_real_toolchain(
    tmp_path: Path,
    adapter: str,
    executable: str,
) -> None:
    if shutil.which(executable) is None:
        pytest.skip(f"{executable} is not installed")
    if adapter == "gobo" and "GOBO" not in os.environ:
        pytest.skip("GOBO is not defined")
    project = create_project(ProjectCreationRequest(tmp_path / f"hello_{adapter}"))

    selected = compile_project(
        project,
        BuildRequest(compiler=adapter),
    )

    assert selected.adapter == adapter


@pytest.mark.toolchain
@pytest.mark.integration
@pytest.mark.parametrize(
    ("adapter", "name", "library"),
    [
        pytest.param("ise", "testing", None, marks=pytest.mark.ise),
        pytest.param("gobo", "gobo_xml", "xml", marks=pytest.mark.gobo),
    ],
)
def test_real_distribution_library_is_locked_and_materialized(
    tmp_path: Path,
    adapter: str,
    name: str,
    library: str | None,
) -> None:
    executable = "ec" if adapter == "ise" else "gec"
    if shutil.which(executable) is None:
        pytest.skip(f"{executable} is not installed")
    if adapter == "gobo" and "GOBO" not in os.environ:
        pytest.skip("GOBO is not defined")
    project = create_project(ProjectCreationRequest(tmp_path / f"dependency_{adapter}"))
    manifest = project.manifest_path
    library_field = f', library = "{library}"' if library else ""
    manifest.write_text(
        manifest.read_text()
        + f'\n[dependencies]\n{name} = {{ source = "{adapter}"{library_field} }}\n'
    )
    project = load_manifest(manifest)

    lock = resolve_dependencies(project)
    install_dependencies(project, lock=lock)

    package = lock.package(name)
    assert package.version
    assert package.checksum is not None
    assert (project.directory / ".evm" / "deps" / package.materialized_name / package.ecf).is_file()


@pytest.mark.toolchain
@pytest.mark.integration
@pytest.mark.ise
def test_calculator_autotest_example_runs_with_ise(tmp_path: Path) -> None:
    if shutil.which("ec") is None:
        pytest.skip("ec is not installed")
    source = Path(__file__).parents[1] / "examples" / "calculator_autotest"
    example = tmp_path / "calculator_autotest"
    shutil.copytree(source, example)
    project = load_manifest(example / "Eiffel.toml")

    result = run_project_tests(project, WorkflowTestRequest(compiler="ise"))

    assert result.status == "passed"
    assert result.runner == "autotest"
    assert result.tests == 4
    assert result.passed == 4

    filtered = run_project_tests(
        project,
        WorkflowTestRequest(
            compiler="ise",
            class_name="CALCULATOR_TESTS",
            feature="test_divide",
        ),
    )

    assert filtered.status == "passed"
    assert filtered.tests == 1
    assert filtered.passed == 1
