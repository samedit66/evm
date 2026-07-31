from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from lxml import etree

from evm.cli import main
from evm.ecf import ECF_NAMESPACE
from evm.lockfile import load_lock
from evm.manifest import load_manifest
from evm.testing import TestDiagnostic as EvmTestDiagnostic
from evm.testing import TestResult as EvmTestResult


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
    concurrency = root.xpath(
        "./*[local-name()='target']/*[local-name()='capability']/*[local-name()='concurrency']"
    )[0]
    assert concurrency.get("support") == "none"
    assert concurrency.get("use") is None
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


def test_new_scoop_project_declares_scoop_support(tmp_path: Path) -> None:
    project = tmp_path / "concurrent"

    result = CliRunner().invoke(main, ["new", str(project), "--scoop"])

    assert result.exit_code == 0, result.output
    loaded = load_manifest(project / "Eiffel.toml")
    assert dict(loaded.requires)["concurrency"] == "scoop"
    root = etree.parse(str(project / "concurrent.ecf")).getroot()
    concurrency = root.xpath(
        "./*[local-name()='target']/*[local-name()='capability']/*[local-name()='concurrency']"
    )[0]
    assert concurrency.get("support") == "scoop"
    assert concurrency.get("use") == "scoop"


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


def test_init_directs_existing_eiffel_project_to_import(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "legacy.ecf").write_text("existing")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"])

    assert result.exit_code != 0
    assert "existing Eiffel project detected" in result.output
    assert "evm import legacy.ecf" in result.output
    assert not (tmp_path / "Eiffel.toml").exists()


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
    monkeypatch.setattr("evm.project.platform.system", lambda: "Darwin")
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
    assert "Import level: lossless" in result.output
    assert ecf.read_bytes() == before
    manifest = (destination / "Eiffel.toml").read_text()
    assert f'uuid = "{expected_uuid}"' in manifest
    assert "ecf-managed = false" in manifest
    assert "[ecf]" not in manifest
    assert sorted(path.name for path in destination.iterdir()) == ["Eiffel.lock", "Eiffel.toml"]
    assert load_lock(destination / "Eiffel.lock").packages == ()


def test_imports_algae_style_legacy_project_in_place(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for directory in ("library", "tests", "benchmark"):
        (tmp_path / directory).mkdir()
    ecf = tmp_path / "algae.ecf"
    ecf.write_text(
        '<?xml version="1.0"?>'
        '<system xmlns="http://www.eiffel.com/developers/xml/configuration-1-18-0" '
        'name="algae" uuid="0BD05554-129B-443B-817C-3E8699BB3295" '
        'library_target="algae">'
        '<target name="algae"><root all_classes="true"/>'
        '<option warning="true"/><library name="base" '
        'location="$ISE_LIBRARY\\library\\base\\base-safe.ecf"/>'
        '<cluster name="algae" location="library\\" recursive="true"/></target>'
        '<target name="tests" extends="algae">'
        '<root class="ANY" feature="default_create"/>'
        '<library name="testing" location="$ISE_LIBRARY\\library\\testing\\testing.ecf"/>'
        '<cluster name="tests" location=".\\tests\\" recursive="true"/></target>'
        '<target name="benchmark" extends="algae">'
        '<root class="RUN_BENCHMARKS" feature="make"/>'
        '<cluster name="benchmark" location=".\\benchmark\\" recursive="true"/></target>'
        "</system>"
    )
    before = ecf.read_bytes()
    monkeypatch.chdir(tmp_path)

    imported = CliRunner().invoke(main, ["import", "algae.ecf"])

    assert imported.exit_code == 0, imported.output
    assert sorted(path.name for path in tmp_path.glob("Eiffel.*")) == [
        "Eiffel.lock",
        "Eiffel.toml",
    ]
    assert not (tmp_path / "config").exists()
    assert ecf.read_bytes() == before
    project = load_manifest(tmp_path / "Eiffel.toml")
    assert project.kind == "library"
    assert project.default_target == "algae"
    assert project.target("algae").sources == ("library",)
    assert project.target("tests").sources == ("tests",)
    assert project.target("tests").extends == "algae"
    assert project.target("benchmark").sources == ("benchmark",)
    assert [target.name for target in project.targets] == ["algae", "tests", "benchmark"]
    assert project.test is not None
    assert project.test.target == "tests"
    assert project.test.runner == "autotest"
    manifest = (tmp_path / "Eiffel.toml").read_text()
    assert '[test]\ntarget = "tests"\nrunner = "autotest"' in manifest
    checked = CliRunner().invoke(main, ["check", "--configuration-only"])
    assert checked.exit_code == 0, checked.output


def test_build_uses_imported_default_target(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "library").mkdir()
    (tmp_path / "algae.ecf").write_text(
        f'<system xmlns="{ECF_NAMESPACE}" name="algae" '
        'uuid="00000000-0000-4000-8000-000000000000" library_target="algae">'
        '<target name="algae"><root all_classes="true"/>'
        '<cluster name="algae" location="library"/></target></system>'
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["import", "algae.ecf"]).exit_code == 0
    requests = []

    def compile_without_toolchain(project, request, check_only=False, *, announce=True):
        requests.append(request)

    monkeypatch.setattr("evm.cli.compile_project", compile_without_toolchain)

    built = runner.invoke(main, ["build"])

    assert built.exit_code == 0, built.output
    assert requests[0].target == "algae"


def test_test_output_lists_failed_and_unresolved_names(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    monkeypatch.chdir(project)
    result = EvmTestResult(
        status="failed",
        exit_code=1,
        elapsed_seconds=0.5,
        compiler="ise 25.12",
        runner="autotest",
        tests=3,
        passed=1,
        failed=1,
        unresolved=1,
        details=(
            EvmTestDiagnostic(
                "HELLO_TESTS.test_failure",
                "failed",
                assertion="value2",
                exception_class="DEVELOPER_EXCEPTION",
                exception_tag="assertion violated",
                stderr="runtime diagnostic",
                trace="first trace line\nsecond trace line",
                trace_valid=True,
                source_path="tests/hello_tests.e",
                source_line=12,
                source_text='assert ("value2", actual = expected)',
            ),
            EvmTestDiagnostic("HELLO_TESTS.test_unresolved", "unresolved"),
        ),
        stdout="runner summary\n",
        stderr="runner warning\n",
    )
    requests = []

    def return_result(project, request):
        requests.append(request)
        return result

    monkeypatch.setattr("evm.cli.test_project", return_result)

    text_result = runner.invoke(main, ["test"])
    trace_result = runner.invoke(main, ["test", "--trace"])
    json_result = runner.invoke(main, ["test", "--json"])
    raw_result = runner.invoke(main, ["test", "--raw"])

    assert text_result.exit_code == 1
    assert "FAILED HELLO_TESTS.test_failure" in text_result.output
    assert "tests/hello_tests.e:12" in text_result.output
    assert 'assert ("value2", actual = expected)' in text_result.output
    assert "Assertion failed: value2" in text_result.output
    assert "first trace line" not in text_result.output
    assert "UNRESOLVED HELLO_TESTS.test_unresolved" in text_result.output
    assert "1 failed, 1 unresolved, 1 passed, 3 total in 0.50s" in text_result.output
    assert "Failed tests:\n  HELLO_TESTS.test_failure" in trace_result.output
    assert "Exception tag: assertion violated" in trace_result.output
    assert "Trace valid: True" in trace_result.output
    assert "Trace: first trace line\n      second trace line" in trace_result.output
    assert raw_result.output == ""
    assert requests[-1].raw is True
    payload = json.loads(json_result.output)
    package = payload["packages"][0]
    assert package["failed_tests"] == ["HELLO_TESTS.test_failure"]
    assert package["unresolved_tests"] == ["HELLO_TESTS.test_unresolved"]
    assert package["test_details"][0]["exception_class"] == "DEVELOPER_EXCEPTION"
    assert package["test_details"][0]["source_path"] == "tests/hello_tests.e"
    assert package["test_details"][0]["source_line"] == 12
    assert package["stdout"] == "runner summary\n"
    assert package["stderr"] == "runner warning\n"


def test_test_rejects_conflicting_output_modes(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    monkeypatch.chdir(project)

    result = runner.invoke(main, ["test", "--raw", "--trace"])

    assert result.exit_code == 1
    assert "--json, --trace, and --raw are mutually exclusive" in result.output


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
    assert "Import level: lossless" in result.output
    assert "defined by the source legacy ECF" in result.output
    manifest = (destination / "Eiffel.toml").read_text()
    assert '[targets."release"]' in manifest
    assert 'extends = "default"' in manifest
    assert "../legacy/src" in manifest
    assert ecf.read_bytes() == before
    monkeypatch.chdir(destination)
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
    assert "Import level: lossless" in result.output
    assert sorted(path.name for path in destination.iterdir()) == ["Eiffel.lock", "Eiffel.toml"]
    assert b"<description>Legacy system</description>" in ecf.read_bytes()


def test_import_keeps_doctype_only_in_source_legacy_ecf(tmp_path: Path) -> None:
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
    assert "Import level: lossless" in result.output
    assert sorted(path.name for path in destination.iterdir()) == ["Eiffel.lock", "Eiffel.toml"]
    assert ecf.read_text().startswith("<!DOCTYPE system")


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
    for command in (
        "new",
        "init",
        "check",
        "build",
        "run",
        "discover",
        "explain",
        "import",
    ):
        assert command in result.output


def test_doctor_command_is_not_available() -> None:
    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code != 0
    assert "No such command 'doctor'" in result.output


def test_run_treats_leading_eiffel_files_as_script_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "hello.e"
    helper = tmp_path / "helper.e"
    root.write_text("class HELLO end\n")
    helper.write_text("class HELPER end\n")
    requests = []

    def run(request) -> int:
        requests.append(request)
        return 0

    monkeypatch.setattr("evm.cli.run_script", run)

    result = CliRunner().invoke(
        main,
        ["run", "--standalone", str(root), str(helper), "--", "input.txt", "--verbose"],
    )

    assert result.exit_code == 0, result.output
    assert requests[0].sources == (root, helper)
    assert requests[0].arguments == ("input.txt", "--verbose")
    assert requests[0].standalone is True


def test_run_rejects_project_only_target_in_file_mode(tmp_path: Path) -> None:
    source = tmp_path / "hello.e"
    source.write_text("class HELLO end\n")

    result = CliRunner().invoke(main, ["run", "--target", "server", str(source)])

    assert result.exit_code != 0
    assert "--target is not supported in file mode" in result.output
