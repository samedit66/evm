from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from evm.dependencies import install_dependencies, resolve_dependencies
from evm.manifest import load_manifest
from evm.model import BuildRequest
from evm.project import compile_project, create_project


@pytest.mark.toolchain
@pytest.mark.integration
@pytest.mark.parametrize(
    ("adapter", "executable"),
    [("ise", "ec"), ("gobo", "gec")],
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
    project = create_project(tmp_path / f"hello_{adapter}")

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
        ("ise", "time", None),
        ("gobo", "gobo_xml", "xml"),
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
    project = create_project(tmp_path / f"dependency_{adapter}")
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
