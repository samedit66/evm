from __future__ import annotations

from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.iron import parse_iron_package, select_iron_project, serialize_iron_package
from evm.manifest import load_manifest
from evm.project import create_project


def test_parse_iron_package_reads_projects_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "package.iron"
    source.write_text(
        """package json

project
    json = "library/json.ecf"
    json_gobo = "library/json_gobo.ecf"

note
    title: Eiffel JSON
    description: "[Eiffel JSON parser.
Second line.
                ]"
    tags: json,parser,text
    license: MIT
    link[github]: "project" https://example.com/json
    maps: /com.eiffel/library/json

setup
    compile_library = Clib
end
"""
    )

    package = parse_iron_package(source.read_text(), source)

    assert package.name == "json"
    assert [(project.name, project.ecf) for project in package.projects] == [
        ("json", "library/json.ecf"),
        ("json_gobo", "library/json_gobo.ecf"),
    ]
    assert package.metadata.title == "Eiffel JSON"
    assert package.metadata.description == "Eiffel JSON parser.\nSecond line."
    assert package.metadata.tags == ("json", "parser", "text")
    assert package.metadata.license == "MIT"
    assert package.metadata.links[0].category == "github"
    assert package.metadata.links[0].title == "project"
    assert package.metadata.links[0].url == "https://example.com/json"
    assert package.metadata.iron_maps == ("/com.eiffel/library/json",)
    assert package.has_setup is True


def test_parse_iron_package_reports_line_for_invalid_project(tmp_path: Path) -> None:
    path = tmp_path / "package.iron"

    with pytest.raises(EvmError, match=r"package\.iron:3: invalid project declaration"):
        parse_iron_package("package example\nproject\ninvalid\nend\n", path)


def test_select_iron_project_rejects_path_outside_package(tmp_path: Path) -> None:
    package = parse_iron_package(
        'package example\nproject\nexample = "../outside.ecf"\nend\n',
        tmp_path / "package.iron",
    )

    with pytest.raises(EvmError, match="escapes the package root"):
        select_iron_project(package, None, tmp_path)


def test_serialize_iron_package_is_stable(tmp_path: Path) -> None:
    project = create_project(tmp_path / "example", library=True)
    manifest = project.manifest_path
    manifest.write_text(
        manifest.read_text()
        + '\n[package]\ntitle = "Example"\ntags = ["eiffel", "library"]\n'
        + '\n[package.links]\nsource = { title = "Source", url = "https://example.com" }\n'
    )
    loaded = load_manifest(manifest)

    first = serialize_iron_package(loaded)
    second = serialize_iron_package(loaded)

    assert first == second
    assert b"package example" in first
    assert b'example = "example.ecf"' in first
    assert b'title: "Example"' in first
    assert b"tags: eiffel,library" in first
    assert b'link[source]: "Source" "https://example.com"' in first
    reparsed = parse_iron_package(first.decode(), tmp_path / "package.iron")
    assert reparsed.metadata == loaded.package


def test_parse_iron_package_preserves_unknown_note_names(tmp_path: Path) -> None:
    package = parse_iron_package(
        "package example\nnote\n    custom-field: custom value\nend\n",
        tmp_path / "package.iron",
    )

    assert package.unknown_notes == ("custom-field",)
