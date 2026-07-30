from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from lxml import etree

from evm.cli import main
from evm.ecf import ECF_NAMESPACE
from evm.lockfile import load_lock


def test_new_creates_application_and_stable_ecf(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    project = tmp_path / "hello"

    created = runner.invoke(main, ["new", str(project)])

    assert created.exit_code == 0, created.output
    assert (project / "Eiffel.toml").is_file()
    lock = load_lock(project / "Eiffel.lock")
    assert lock.format_version == 1
    assert len(lock.manifest_fingerprint) == 64
    assert lock.packages == ()
    assert (project / "src" / "application.e").is_file()
    assert (project / "tests").is_dir()
    assert not (project / ".Eiffel.toml.tmp").exists()
    ecf = project / "hello.ecf"
    original = ecf.read_bytes()
    document = etree.parse(str(ecf))
    root = document.getroot()
    assert etree.QName(root).namespace == ECF_NAMESPACE
    assert root.get("name") == "hello"
    assert root.get("uuid")
    monkeypatch.chdir(project)

    checked = runner.invoke(main, ["check", "--configuration-only"], catch_exceptions=False)

    assert checked.exit_code == 0, checked.output
    assert ecf.read_bytes() == original


def test_new_library_has_all_classes_root(tmp_path: Path) -> None:
    project = tmp_path / "sample"

    result = CliRunner().invoke(main, ["new", str(project), "--lib"])

    assert result.exit_code == 0, result.output
    assert (project / "src" / "sample.e").is_file()
    root = etree.parse(str(project / "sample.ecf")).getroot()
    assert root.get("library_target") == "default"
    roots = root.xpath("./*[local-name()='target']/*[local-name()='root']")
    assert roots[0].get("all_classes") == "true"


def test_init_preserves_existing_gitignore(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "existing"
    project.mkdir()
    gitignore = project / ".gitignore"
    gitignore.write_text("custom/\n")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["init"])

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == "custom/\n"
    assert (project / "Eiffel.toml").is_file()


def test_check_reports_missing_cluster(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    (project / "src").rename(project / "missing")
    monkeypatch.chdir(project)

    result = runner.invoke(main, ["check", "--configuration-only"])

    assert result.exit_code != 0
    assert "source directory does not exist: src" in result.output


def test_manually_modified_managed_ecf_requires_explicit_regeneration(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    ecf = project / "hello.ecf"
    ecf.write_bytes(ecf.read_bytes().replace(b'feature="make"', b'feature="changed"'))
    monkeypatch.chdir(project)

    rejected = runner.invoke(main, ["check", "--configuration-only"])
    regenerated = runner.invoke(main, ["check", "--configuration-only", "--regenerate-ecf"])

    assert rejected.exit_code != 0
    assert "managed ECF was modified outside EVM" in rejected.output
    assert regenerated.exit_code == 0, regenerated.output
    assert b'feature="make"' in ecf.read_bytes()


def test_explain_json_includes_conditions(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    manifest = project / "Eiffel.toml"
    manifest.write_text(
        manifest.read_text()
        + '\n[[conditions]]\ntarget = "default"\n'
        + 'when = { os = "macos", mode = "dev" }\n'
        + 'sources = ["src"]\n'
    )
    monkeypatch.chdir(project)

    result = runner.invoke(main, ["explain", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["conditions"][0]["when"] == {"mode": "dev", "os": "macos"}
    assert data["conditions"][0]["matched"] is True


def test_import_preserves_source_and_uuid(tmp_path: Path) -> None:
    source_project = tmp_path / "source"
    destination = tmp_path / "imported"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(source_project)]).exit_code == 0
    ecf = source_project / "source.ecf"
    before = ecf.read_bytes()
    expected_uuid = etree.parse(str(ecf)).getroot().get("uuid")

    result = runner.invoke(main, ["import", str(ecf), "--destination", str(destination)])

    assert result.exit_code == 0, result.output
    assert "Import level: lossless-with-overlay" in result.output
    assert ecf.read_bytes() == before
    manifest = (destination / "Eiffel.toml").read_text()
    assert f'uuid = "{expected_uuid}"' in manifest
    assert "ecf-managed = false" in manifest
    assert '[ecf]\ninclude = ["config/imported.ecf"]' in manifest
    assert (destination / "config" / "imported.ecf").is_file()
    assert load_lock(destination / "Eiffel.lock").packages == ()


def test_import_preserves_all_targets_and_unknown_ecf_constructs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "src").mkdir()
    ecf = source / "legacy.ecf"
    ecf.write_text(
        '<?xml version="1.0"?>'
        f'<system xmlns="{ECF_NAMESPACE}" name="legacy" '
        'uuid="00000000-0000-4000-8000-000000000000">'
        '<target name="default"><root class="APPLICATION" feature="make"/>'
        '<cluster name="src" location="src"/>'
        '<library name="custom" location="$CUSTOM/custom.ecf"/></target>'
        '<target name="release" extends="default">'
        '<option warning="true"/></target></system>'
    )
    before = ecf.read_bytes()
    destination = tmp_path / "imported"

    result = CliRunner().invoke(
        main,
        ["import", str(ecf), "--destination", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert "Import level: lossless-with-overlay" in result.output
    assert "retained as opaque ECF overlay entries" in result.output
    manifest = (destination / "Eiffel.toml").read_text()
    assert '[targets."release"]' in manifest
    assert 'extends = "default"' in manifest
    assert "../legacy/src" in manifest
    assert ecf.read_bytes() == before
    monkeypatch.chdir(destination)
    diff = CliRunner().invoke(main, ["explain", "--ecf-diff"])
    assert diff.exit_code == 0, diff.output
    assert diff.output == "No semantic differences.\n"
    checked = CliRunner().invoke(main, ["check", "--configuration-only"])
    assert checked.exit_code == 0, checked.output
    assert ecf.read_bytes() == before


def test_import_classifies_unsupported_namespace(tmp_path: Path) -> None:
    ecf = tmp_path / "unsupported.ecf"
    ecf.write_text(
        '<system xmlns="https://example.invalid/ecf" name="old" '
        'uuid="00000000-0000-4000-8000-000000000000">'
        '<target name="default"/></system>'
    )

    result = CliRunner().invoke(
        main,
        ["import", str(ecf), "--destination", str(tmp_path / "imported")],
    )

    assert result.exit_code != 0
    assert "Import level: unsupported" in result.output
    assert not (tmp_path / "imported").exists()


def test_import_classifies_high_level_subset_as_lossless(tmp_path: Path) -> None:
    ecf = tmp_path / "simple.ecf"
    ecf.write_text(
        f'<system xmlns="{ECF_NAMESPACE}" name="simple" '
        'uuid="00000000-0000-4000-8000-000000000000">'
        '<target name="default"><root all_classes="true"/>'
        '<cluster name="src" location="src" recursive="true"/></target></system>'
    )
    destination = tmp_path / "imported"

    result = CliRunner().invoke(
        main,
        ["import", str(ecf), "--destination", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert "Import level: lossless\n" in result.output
    assert not (destination / "config" / "imported.ecf").exists()


def test_import_preserves_system_level_unknown_construct(tmp_path: Path) -> None:
    ecf = tmp_path / "described.ecf"
    ecf.write_text(
        f'<system xmlns="{ECF_NAMESPACE}" name="described" '
        'uuid="00000000-0000-4000-8000-000000000000">'
        "<description>Legacy system</description>"
        '<target name="default"><root all_classes="true"/>'
        '<cluster name="src" location="src"/></target></system>'
    )
    destination = tmp_path / "imported"

    result = CliRunner().invoke(
        main,
        ["import", str(ecf), "--destination", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert "Import level: lossless-with-overlay" in result.output
    overlay = etree.parse(str(destination / "config" / "imported.ecf"))
    descriptions = overlay.xpath("/*/*[local-name()='description']")
    assert descriptions[0].text == "Legacy system"


def test_import_classifies_doctype_as_partial_without_copying_it(tmp_path: Path) -> None:
    ecf = tmp_path / "doctype.ecf"
    ecf.write_text(
        "<!DOCTYPE system [<!ELEMENT system ANY>]>"
        f'<system xmlns="{ECF_NAMESPACE}" name="doctype" '
        'uuid="00000000-0000-4000-8000-000000000000">'
        '<target name="default"><root all_classes="true"/>'
        '<cluster name="src" location="src"/></target></system>'
    )
    destination = tmp_path / "imported"

    result = CliRunner().invoke(
        main,
        ["import", str(ecf), "--destination", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert "Import level: partial" in result.output
    assert "document type declarations" in result.output
    assert not (destination / "config" / "imported.ecf").exists()


def test_import_rejects_application_root_without_creation_feature(
    tmp_path: Path,
) -> None:
    ecf = tmp_path / "broken.ecf"
    ecf.write_text(
        '<?xml version="1.0"?>'
        '<system xmlns="http://www.eiffel.com/developers/xml/configuration-1-23-0" '
        'name="broken" uuid="00000000-0000-4000-8000-000000000000">'
        '<target name="default"><root class="APPLICATION"/>'
        '<cluster name="src" location="src"/></target></system>'
    )

    result = CliRunner().invoke(
        main,
        ["import", str(ecf), "--destination", str(tmp_path / "imported")],
    )

    assert result.exit_code != 0
    assert "root must define a creation feature" in result.output


def test_main_help_lists_stage_one_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    for command in ("new", "init", "check", "build", "run", "doctor", "explain", "import"):
        assert command in result.output
