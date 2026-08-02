from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evm.dependencies.resolution import install_dependencies, resolve_dependencies
from evm.manifest import load_manifest
from evm.project.creation import ProjectCreationRequest, create_project


@pytest.mark.network
@pytest.mark.integration
def test_real_iron_package_uses_verified_project_local_archives(tmp_path: Path) -> None:
    if shutil.which("iron") is None:
        pytest.skip("iron is not installed")
    project = create_project(ProjectCreationRequest(tmp_path / "iron_json"))
    manifest = project.manifest_path
    manifest.write_text(manifest.read_text() + '\n[dependencies]\njson = "25.02"\n')
    project = load_manifest(manifest)

    lock = resolve_dependencies(project)
    install_dependencies(project, lock=lock)

    assert {package.name for package in lock.packages} == {"base", "json", "time"}
    package = lock.package("json")
    assert package.checksum is not None and package.checksum.startswith("sha256:")
    archive = (
        project.directory
        / ".evm"
        / "sources"
        / "archives"
        / f"{package.checksum.removeprefix('sha256:')}.tar.bz2"
    )
    assert archive.is_file()
    ecf = project.directory / ".evm" / "deps" / package.materialized_name / "library" / "json.ecf"
    content = ecf.read_text()
    assert "iron:base:" not in content
    assert "iron:time:" not in content

    shutil.rmtree(project.directory / ".evm")
    install_dependencies(project, lock=lock)
    assert ecf.is_file()

    shutil.rmtree(project.directory / ".evm" / "deps")
    install_dependencies(project, lock=lock, offline=True)

    assert ecf.is_file()
