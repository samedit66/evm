from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from evm.cli import main
from evm.manifest import load_manifest
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import LIBRARY_TEMPLATE


def test_import_package_iron_selects_project_and_preserves_metadata(tmp_path: Path) -> None:
    source_project = create_project(ProjectCreationRequest(tmp_path / "source", LIBRARY_TEMPLATE))
    package = source_project.directory / "package.iron"
    package.write_text(
        """package json
project
    json = "source.ecf"
    alternate = "source.ecf"
note
    title: Eiffel JSON
    tags: json,parser
    license: MIT
    link[source]: "Source" https://example.com/json
end
"""
    )
    destination = tmp_path / "imported"

    result = CliRunner().invoke(
        main,
        [
            "import",
            str(package),
            "--project",
            "json",
            "--destination",
            str(destination),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "IRON package name 'json' differs from ECF system name 'source'" in result.output
    project = load_manifest(destination / "Eiffel.toml")
    assert project.name == "json"
    assert project.package is not None
    assert project.package.title == "Eiffel JSON"
    assert project.package.tags == ("json", "parser")
    assert project.package.links[0].url == "https://example.com/json"
    assert package.is_file()


def test_import_package_iron_requires_project_for_multiple_ecfs(tmp_path: Path) -> None:
    source = create_project(ProjectCreationRequest(tmp_path / "source", LIBRARY_TEMPLATE))
    package = source.directory / "package.iron"
    package.write_text(
        'package source\nproject\n    first = "source.ecf"\n    second = "source.ecf"\nend\n'
    )

    result = CliRunner().invoke(
        main,
        ["import", str(package), "--destination", str(tmp_path / "imported")],
    )

    assert result.exit_code != 0
    assert "declares multiple projects; use --project" in result.output
    assert not (tmp_path / "imported").exists()


def test_import_package_iron_does_not_execute_setup(tmp_path: Path) -> None:
    source = create_project(ProjectCreationRequest(tmp_path / "source", LIBRARY_TEMPLATE))
    package = source.directory / "package.iron"
    package.write_text(
        "package source\nproject\n"
        '    source = "source.ecf"\n'
        "setup\n    compile_library = Clib\nend\n"
    )

    result = CliRunner().invoke(
        main,
        ["import", str(package), "--destination", str(tmp_path / "imported")],
    )

    assert result.exit_code == 0, result.output
    assert "Import level: partial" in result.output
    assert "setup declarations were not imported or executed" in result.output


def test_import_package_iron_reports_unknown_notes_as_partial(tmp_path: Path) -> None:
    source = create_project(ProjectCreationRequest(tmp_path / "source", LIBRARY_TEMPLATE))
    package = source.directory / "package.iron"
    package.write_text(
        "package source\nproject\n"
        '    source = "source.ecf"\n'
        "note\n    custom-field: custom value\nend\n"
    )

    result = CliRunner().invoke(
        main,
        ["import", str(package), "--destination", str(tmp_path / "imported")],
    )

    assert result.exit_code == 0, result.output
    assert "Import level: partial" in result.output
    assert "unsupported IRON notes were not imported: custom-field" in result.output


def test_iron_export_refuses_overwrite_and_supports_check(tmp_path: Path, monkeypatch) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "example", LIBRARY_TEMPLATE))
    monkeypatch.chdir(project.directory)
    runner = CliRunner()

    exported = runner.invoke(main, ["iron", "export"])
    checked = runner.invoke(main, ["iron", "export", "--check"])

    assert exported.exit_code == 0, exported.output
    assert checked.exit_code == 0, checked.output
    package = project.directory / "package.iron"
    package.write_text("package manually_edited\nend\n")

    refused = runner.invoke(main, ["iron", "export"])
    stale = runner.invoke(main, ["iron", "export", "--check"])
    forced = runner.invoke(main, ["iron", "export", "--force"])

    assert refused.exit_code != 0
    assert "refusing to overwrite" in refused.output
    assert stale.exit_code != 0
    assert "missing or out of date" in stale.output
    assert forced.exit_code == 0, forced.output
    assert package.read_text().startswith("package example\n")


def test_check_validates_existing_package_iron(tmp_path: Path, monkeypatch) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "example", LIBRARY_TEMPLATE))
    (project.directory / "package.iron").write_text(
        'package different\nproject\n    example = "missing.ecf"\nend\n'
    )
    monkeypatch.chdir(project.directory)

    result = CliRunner().invoke(main, ["check", "--configuration-only"])

    assert result.exit_code != 0
    assert "package name 'different' does not match 'example'" in result.output
    assert "project ECF not found: missing.ecf" in result.output
    assert "current ECF is not declared: example.ecf" in result.output
