from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evm.dependencies import install_dependencies, resolve_dependencies
from evm.manifest import load_manifest
from evm.project.creation import ProjectCreationRequest, create_project


@pytest.mark.network
@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "url", "ecf"),
    [
        (
            "json",
            "https://github.com/eiffelhub/json.git",
            "library/json.ecf",
        ),
        (
            "ewf_jwt",
            "https://github.com/EiffelWebFramework/EWF.git",
            "library/security/jwt/jwt.ecf",
        ),
    ],
)
def test_real_github_eiffel_project_resolves_and_reinstalls_offline(
    tmp_path: Path,
    name: str,
    url: str,
    ecf: str,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / name))
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        + f'\n[dependencies]\n{name} = {{ git = "{url}", branch = "master", ecf = "{ecf}" }}\n'
    )
    project = load_manifest(manifest)

    lock = resolve_dependencies(project)
    install_dependencies(project, lock=lock)

    package = lock.package(name)
    assert package.revision is not None and len(package.revision) == 40
    assert package.tree is not None and len(package.tree) == 40
    destination = project.directory / ".evm" / "deps" / package.materialized_name
    assert (destination / ecf).is_file()
    assert "iron:" not in (destination / ecf).read_text()

    shutil.rmtree(project.directory / ".evm")
    install_dependencies(project, lock=lock)
    assert (destination / ecf).is_file()

    shutil.rmtree(destination)
    install_dependencies(project, lock=lock, offline=True)

    assert (destination / ecf).is_file()
