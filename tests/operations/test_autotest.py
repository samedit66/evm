from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

from evm.errors import EvmError
from evm.model import BuildRequest
from evm.operations.autotest import (
    AutoTestCase,
    AutoTestDiagnostic,
    AutoTestRunRequest,
    _filter_autotest_cases,
    _generate_runner_project,
    _parse_descendants,
    _parse_test_features,
    _run_case,
    _run_raw_case,
    _runner_ecf,
    _runner_source,
    run_autotest,
)
from evm.project.creation import ProjectCreationRequest, create_project
from evm.project.templates import LIBRARY_TEMPLATE
from evm.toolchain.selection import Toolchain
from evm.versioning import NumericVersion


def test_parses_effective_autotest_descendants() -> None:
    output = """
Eiffel Compilation Manager

    EQA_TEST_SET*
        CALCULATOR_TESTS
        DEFERRED_TESTS*
        OTHER_TESTS
"""

    assert _parse_descendants(output) == ("CALCULATOR_TESTS", "OTHER_TESTS")


def test_parses_only_public_argumentless_procedures_from_short_view() -> None:
    output = """
class interface
    CALCULATOR_TESTS

feature -- Tests

    test_add

    test_with_argument (value: INTEGER)

    value: INTEGER

    test_subtract

end -- class CALCULATOR_TESTS
"""

    assert _parse_test_features(output) == ("test_add", "test_subtract")


def test_filters_autotest_cases_by_exact_case_insensitive_names() -> None:
    cases = (
        AutoTestCase("CALCULATOR_TESTS", "test_add"),
        AutoTestCase("CALCULATOR_TESTS", "test_subtract"),
        AutoTestCase("OTHER_TESTS", "test_add"),
    )

    assert _filter_autotest_cases(cases, "calculator_tests", "TEST_ADD") == (
        AutoTestCase("CALCULATOR_TESTS", "test_add"),
    )

    with pytest.raises(EvmError, match="no AutoTest tests matched"):
        _filter_autotest_cases(cases, "CALCULATOR", None)


def test_generated_runner_registers_each_test() -> None:
    source = _runner_source(
        (
            AutoTestCase("CALCULATOR_TESTS", "test_add"),
            AutoTestCase("CALCULATOR_TESTS", "test_subtract"),
        )
    )

    assert 'argument (2).same_string ("test_add")' in source
    assert "evaluator: EQA_TEST_EVALUATOR [CALCULATOR_TESTS]" in source
    assert "selected_test_set.test_subtract" in source
    assert "EVM_AUTOTEST_RESULT|" in source
    assert 'argument (3).same_string ("--raw")' in source
    assert "report_raw" in source


def test_generated_runner_is_application_for_library_project(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "library", LIBRARY_TEMPLATE))

    generated = _generate_runner_project(
        project,
        BuildRequest(target="default"),
        (AutoTestCase("LIBRARY_TESTS", "test_feature"),),
    )

    assert generated.kind == "application"


def test_runner_protocol_preserves_failure_diagnostics(tmp_path: Path, monkeypatch) -> None:
    output = """EVM_AUTOTEST_RESULT|failed
EVM_AUTOTEST_ASSERTION|value2
EVM_AUTOTEST_EXCEPTION_CLASS|DEVELOPER_EXCEPTION
EVM_AUTOTEST_EXCEPTION_FEATURE|assert
EVM_AUTOTEST_EXCEPTION_CODE|24
EVM_AUTOTEST_EXCEPTION_TAG|assertion violated
EVM_AUTOTEST_BREAKPOINT_SLOT|7
EVM_AUTOTEST_TEST_INVALID|false
EVM_AUTOTEST_TRACE_VALID|true
EVM_AUTOTEST_OUTPUT_BEGIN
diagnostic output
EVM_AUTOTEST_OUTPUT_END
EVM_AUTOTEST_TRACE_BEGIN
first trace line
second trace line
EVM_AUTOTEST_TRACE_END
"""
    monkeypatch.setattr(
        "evm.operations.autotest.subprocess.run",
        lambda *arguments, **options: SimpleNamespace(
            returncode=1,
            stdout=output,
            stderr="runner diagnostic on stderr\n",
        ),
    )

    diagnostic = _run_case(
        tmp_path / "runner",
        tmp_path,
        AutoTestCase("MATRIX_TESTS", "test_value"),
    )

    assert diagnostic.status == "failed"
    assert diagnostic.assertion == "value2"
    assert diagnostic.exception_class == "DEVELOPER_EXCEPTION"
    assert diagnostic.exception_feature == "assert"
    assert diagnostic.exception_code == 24
    assert diagnostic.exception_tag == "assertion violated"
    assert diagnostic.breakpoint_slot == 7
    assert diagnostic.test_invalid is False
    assert diagnostic.trace_valid is True
    assert diagnostic.output == "diagnostic output"
    assert diagnostic.stderr == "runner diagnostic on stderr"
    assert diagnostic.trace == "first trace line\nsecond trace line"


def test_runner_protocol_omits_unavailable_breakpoint_slot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "evm.operations.autotest.subprocess.run",
        lambda *arguments, **options: SimpleNamespace(
            returncode=1,
            stdout=("EVM_AUTOTEST_RESULT|failed\nEVM_AUTOTEST_BREAKPOINT_SLOT|0\n"),
            stderr="",
        ),
    )

    diagnostic = _run_case(
        tmp_path / "runner",
        tmp_path,
        AutoTestCase("MATRIX_TESTS", "test_value"),
    )

    assert diagnostic.breakpoint_slot is None


def test_runner_protocol_preserves_unresolved_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "evm.operations.autotest.subprocess.run",
        lambda *arguments, **options: SimpleNamespace(
            returncode=2,
            stdout="EVM_AUTOTEST_RESULT|unresolved\n",
            stderr="",
        ),
    )

    diagnostic = _run_case(
        tmp_path / "runner",
        tmp_path,
        AutoTestCase("MATRIX_TESTS", "test_value"),
    )

    assert diagnostic.status == "unresolved"


@pytest.mark.parametrize(
    ("exit_code", "status"),
    [(0, "passed"), (1, "failed"), (2, "unresolved")],
)
def test_raw_runner_passes_output_through(
    tmp_path: Path,
    monkeypatch,
    exit_code: int,
    status: str,
) -> None:
    commands = []
    monkeypatch.setattr(
        "evm.operations.autotest.subprocess.run",
        lambda command, **options: (
            commands.append((command, options)) or SimpleNamespace(returncode=exit_code)
        ),
    )
    test_case = AutoTestCase("MATRIX_TESTS", "test_value")

    result = _run_raw_case(tmp_path / "runner", tmp_path, test_case)

    assert result == status
    assert commands[0][0][-1] == "--raw"
    assert "capture_output" not in commands[0][1]


def test_runner_ecf_rebases_relative_locations(tmp_path: Path) -> None:
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    ecf = project_directory / "project.ecf"
    ecf.write_text(
        """<?xml version="1.0"?>
<system xmlns="http://www.eiffel.com/developers/xml/configuration-1-23-0"
        name="sample" uuid="a7dc72e4-94d9-4c44-97eb-f8fa1e5651ad">
  <target name="test">
    <root class="APPLICATION" feature="make"/>
    <cluster name="tests" location="tests"/>
  </target>
</system>
"""
    )
    generated = tmp_path / "state" / "generated"

    overlay = _runner_ecf(ecf, "test", generated).decode()

    assert f'location="{project_directory / "tests"}"' in overlay
    assert 'class="EVM_AUTOTEST_APPLICATION"' in overlay
    assert 'name="testing"' in overlay
    assert f'location="{generated}"' in overlay


def test_runner_ecf_preserves_legacy_namespace(tmp_path: Path) -> None:
    ecf = tmp_path / "legacy.ecf"
    namespace = "http://www.eiffel.com/developers/xml/configuration-1-18-0"
    ecf.write_text(
        f'<system xmlns="{namespace}" name="legacy" '
        'uuid="00000000-0000-4000-8000-000000000000">'
        '<target name="tests"><root class="ANY" feature="default_create"/>'
        '<library name="testing" location="$ISE_LIBRARY/library/testing/testing.ecf"/>'
        "</target></system>"
    )

    generated = etree.fromstring(_runner_ecf(ecf, "tests", tmp_path / "generated"))

    namespaces = {etree.QName(element).namespace for element in generated.iter()}
    assert namespaces == {namespace}


def test_run_autotest_aggregates_normalized_and_raw_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    toolchain = Toolchain(
        "ise",
        tmp_path / "ec",
        NumericVersion.parse("25.12"),
        "explicit",
        "--toolchain",
    )
    cases = (
        AutoTestCase("HELLO_TESTS", "test_pass"),
        AutoTestCase("HELLO_TESTS", "test_fail"),
    )
    monkeypatch.setattr("evm.operations.autotest.prepare_project", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "evm.operations.autotest.prepare_compilation_project", lambda value, selected: value
    )
    monkeypatch.setattr(
        "evm.operations.autotest.prepare_build_directory",
        lambda *args, **kwargs: tmp_path / "build",
    )
    monkeypatch.setattr("evm.operations.autotest._discover_autotest_cases", lambda context: cases)
    monkeypatch.setattr("evm.operations.autotest._generate_runner_project", lambda *args: project)
    monkeypatch.setattr(
        "evm.operations.autotest._compile_runner", lambda *args: tmp_path / "runner"
    )

    def run_case(executable: Path, directory: Path, case: AutoTestCase) -> AutoTestDiagnostic:
        status = "passed" if case.feature == "test_pass" else "failed"
        return AutoTestDiagnostic(case, status)

    monkeypatch.setattr("evm.operations.autotest._run_case", run_case)
    normalized = run_autotest(
        project,
        BuildRequest(),
        toolchain,
        AutoTestRunRequest(),
    )
    monkeypatch.setattr(
        "evm.operations.autotest._run_raw_case",
        lambda executable, directory, case: (
            "passed" if case.feature == "test_pass" else "unresolved"
        ),
    )
    raw = run_autotest(
        project,
        BuildRequest(),
        toolchain,
        AutoTestRunRequest(raw=True),
    )

    assert normalized.tests == 2
    assert normalized.failed == 1
    assert [item.qualified_name for item in normalized.failed_tests] == ["HELLO_TESTS.test_fail"]
    assert normalized.exit_code == 1
    assert raw.unresolved == 1
    assert raw.unresolved_tests == ()


def test_run_autotest_requires_ise(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    toolchain = Toolchain(
        "gobo",
        tmp_path / "gec",
        NumericVersion.parse("26.06"),
        "explicit",
        "--toolchain",
    )

    with pytest.raises(EvmError, match="requires the ISE"):
        run_autotest(project, BuildRequest(), toolchain, AutoTestRunRequest())
