"""Built-in templates for new Eiffel project scaffolds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evm.model import Root


@dataclass(frozen=True)
class ProjectSource:
    """Describe the initial source file supplied by a project template."""

    relative_path: Path
    content: str


class ProjectTemplate(Protocol):
    """Describe the project-specific parts of a new project scaffold."""

    kind: str
    root: Root | None

    def source(self, project_name: str) -> ProjectSource:
        """Return the initial source file for a normalized project name."""


class ApplicationProjectTemplate:
    """Describe the scaffold for an executable Eiffel application."""

    kind = "application"
    root = Root("APPLICATION", "make")

    def source(self, project_name: str) -> ProjectSource:
        """Return the conventional application entry-point source."""
        return ProjectSource(
            Path("src/application.e"),
            (
                "class\n"
                "    APPLICATION\n\n"
                "create\n"
                "    make\n\n"
                "feature {NONE} -- Initialization\n\n"
                "    make\n"
                "            -- Run the application.\n"
                "        do\n"
                '            print ("Hello from EVM!%N")\n'
                "        end\n\n"
                "end\n"
            ),
        )


class LibraryProjectTemplate:
    """Describe the scaffold for a reusable Eiffel library."""

    kind = "library"
    root = None

    def source(self, project_name: str) -> ProjectSource:
        """Return a library class named after the normalized project."""
        class_name = _eiffel_class_name(project_name)
        return ProjectSource(
            Path(f"src/{class_name.lower()}.e"),
            f"class\n    {class_name}\n\nend\n",
        )


APPLICATION_TEMPLATE: ProjectTemplate = ApplicationProjectTemplate()
LIBRARY_TEMPLATE: ProjectTemplate = LibraryProjectTemplate()


def _eiffel_class_name(project_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", project_name).upper()
