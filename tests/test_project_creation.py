from __future__ import annotations

from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import (
    APPLICATION_TEMPLATE,
    LIBRARY_TEMPLATE,
    ProjectSource,
    ProjectTemplate,
)


@pytest.mark.parametrize(
    ("template", "kind", "root", "source_path"),
    [
        (
            APPLICATION_TEMPLATE,
            "application",
            ("APPLICATION", "make"),
            Path("src/application.e"),
        ),
        (LIBRARY_TEMPLATE, "library", None, Path("src/sample_library.e")),
    ],
)
def test_builtin_project_templates_describe_project_variants(
    template: ProjectTemplate,
    kind: str,
    root: tuple[str, str] | None,
    source_path: Path,
) -> None:
    source = template.source("sample_library")

    assert template.kind == kind
    if root is None:
        assert template.root is None
    else:
        assert template.root is not None
        assert (template.root.class_name, template.root.feature) == root
    assert source.relative_path == source_path
    assert source.content.startswith("class\n")


def test_create_project_combines_template_with_common_scaffold(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "shared", LIBRARY_TEMPLATE))

    assert project.kind == "library"
    assert project.targets[0].root is None
    assert (project.directory / "src/shared.e").read_text() == "class\n    SHARED\n\nend\n"
    assert (project.directory / "Eiffel.lock").is_file()
    assert (project.directory / "shared.ecf").is_file()


def test_create_project_rejects_template_source_outside_project(tmp_path: Path) -> None:
    class EscapingTemplate:
        """Provide an invalid source path for boundary validation."""

        kind = "library"
        root = None

        def source(self, project_name: str) -> ProjectSource:
            """Return a path that escapes the project directory."""
            return ProjectSource(Path("../outside.e"), "class OUTSIDE end\n")

    request = ProjectCreationRequest(tmp_path / "unsafe", EscapingTemplate())

    with pytest.raises(EvmError, match="must stay inside the project"):
        create_project(request)
