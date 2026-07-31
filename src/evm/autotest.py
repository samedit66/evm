"""Console execution for EiffelStudio AutoTest test sets."""

from __future__ import annotations

import dataclasses
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from evm.ecf import parse_ecf
from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.model import BuildRequest, Project
from evm.project import prepare_compilation_project, prepare_project
from evm.toolchains import (
    Toolchain,
    artifact_candidates,
    compiler_command,
    prepare_build_directory,
    run_compiler,
)

_EIFFEL_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_RESULT_PREFIX = "EVM_AUTOTEST_RESULT|"
_ASSERTION_PREFIX = "EVM_AUTOTEST_ASSERTION|"
_EXCEPTION_CLASS_PREFIX = "EVM_AUTOTEST_EXCEPTION_CLASS|"
_EXCEPTION_FEATURE_PREFIX = "EVM_AUTOTEST_EXCEPTION_FEATURE|"
_EXCEPTION_CODE_PREFIX = "EVM_AUTOTEST_EXCEPTION_CODE|"
_EXCEPTION_TAG_PREFIX = "EVM_AUTOTEST_EXCEPTION_TAG|"
_BREAKPOINT_SLOT_PREFIX = "EVM_AUTOTEST_BREAKPOINT_SLOT|"
_TEST_INVALID_PREFIX = "EVM_AUTOTEST_TEST_INVALID|"
_TRACE_VALID_PREFIX = "EVM_AUTOTEST_TRACE_VALID|"
_OUTPUT_BEGIN = "EVM_AUTOTEST_OUTPUT_BEGIN"
_OUTPUT_END = "EVM_AUTOTEST_OUTPUT_END"
_TRACE_BEGIN = "EVM_AUTOTEST_TRACE_BEGIN"
_TRACE_END = "EVM_AUTOTEST_TRACE_END"


@dataclass(frozen=True, order=True)
class AutoTestCase:
    class_name: str
    feature: str

    @property
    def qualified_name(self) -> str:
        return f"{self.class_name}.{self.feature}"


@dataclass(frozen=True)
class AutoTestDiagnostic:
    test_case: AutoTestCase
    status: str
    assertion: str | None = None
    exception_class: str | None = None
    exception_feature: str | None = None
    exception_code: int | None = None
    exception_tag: str | None = None
    breakpoint_slot: int | None = None
    test_invalid: bool | None = None
    trace_valid: bool | None = None
    output: str | None = None
    stderr: str | None = None
    trace: str | None = None


@dataclass(frozen=True)
class AutoTestExecution:
    tests: int
    passed: int
    failed: int
    unresolved: int
    diagnostics: tuple[AutoTestDiagnostic, ...] = ()

    @property
    def failed_tests(self) -> tuple[AutoTestCase, ...]:
        return tuple(item.test_case for item in self.diagnostics if item.status == "failed")

    @property
    def unresolved_tests(self) -> tuple[AutoTestCase, ...]:
        return tuple(item.test_case for item in self.diagnostics if item.status == "unresolved")

    @property
    def exit_code(self) -> int:
        return 0 if self.failed == 0 and self.unresolved == 0 else 1


@dataclass(frozen=True)
class AutoTestRunRequest:
    class_name: str | None = None
    feature: str | None = None
    raw: bool = False


@dataclass(frozen=True)
class _DiscoveryContext:
    toolchain: Toolchain
    project: Project
    request: BuildRequest
    project_path: Path


def run_autotest(
    project: Project,
    build_request: BuildRequest,
    toolchain: Toolchain,
    request: AutoTestRunRequest,
) -> AutoTestExecution:
    if toolchain.adapter != "ise":
        raise EvmError("AutoTest runner requires the ISE compiler adapter")
    prepare_project(
        project,
        regenerate=build_request.regenerate_ecf,
        release=build_request.release,
        offline=build_request.offline,
    )
    compilation_project = prepare_compilation_project(project, toolchain)
    discovery_directory = prepare_build_directory(
        toolchain,
        project,
        build_request,
        check_only=True,
    )
    discovery = _DiscoveryContext(
        toolchain=toolchain,
        project=compilation_project,
        request=build_request,
        project_path=discovery_directory,
    )
    discovered = _discover_autotest_cases(discovery)
    selected = _filter_autotest_cases(discovered, request.class_name, request.feature)
    generated_project = _generate_runner_project(
        compilation_project,
        build_request,
        discovered,
    )
    executable = _compile_runner(generated_project, build_request, toolchain)
    if request.raw:
        statuses = tuple(
            _run_raw_case(executable, project.directory, test_case) for test_case in selected
        )
        diagnostics: tuple[AutoTestDiagnostic, ...] = ()
    else:
        results = tuple(
            _run_case(executable, project.directory, test_case) for test_case in selected
        )
        statuses = tuple(result.status for result in results)
        diagnostics = tuple(result for result in results if result.status != "passed")
    return AutoTestExecution(
        tests=len(statuses),
        passed=statuses.count("passed"),
        failed=statuses.count("failed"),
        unresolved=statuses.count("unresolved"),
        diagnostics=diagnostics,
    )


def _discover_autotest_cases(context: _DiscoveryContext) -> tuple[AutoTestCase, ...]:
    descendants_output = _compiler_view(
        context,
        "-descendants",
        "EQA_TEST_SET",
    )
    class_names = _parse_descendants(descendants_output)
    cases: list[AutoTestCase] = []
    for class_name in class_names:
        short_output = _compiler_view(
            context,
            "-short",
            class_name,
        )
        cases.extend(
            AutoTestCase(class_name, feature) for feature in _parse_test_features(short_output)
        )
    if not cases:
        raise EvmError("no AutoTest test procedures were discovered")
    return tuple(sorted(cases))


def _filter_autotest_cases(
    cases: tuple[AutoTestCase, ...],
    class_name: str | None,
    feature: str | None,
) -> tuple[AutoTestCase, ...]:
    selected = tuple(
        test_case
        for test_case in cases
        if (class_name is None or test_case.class_name.casefold() == class_name.casefold())
        and (feature is None or test_case.feature.casefold() == feature.casefold())
    )
    if not selected:
        requested = ".".join(item for item in (class_name, feature) if item is not None)
        raise EvmError(f"no AutoTest tests matched {requested!r}")
    return selected


def _compiler_view(
    context: _DiscoveryContext,
    option: str,
    subject: str,
) -> str:
    command = [
        str(context.toolchain.executable),
        "-batch",
        "-config",
        str(context.project.ecf_path),
        "-target",
        context.request.target,
        "-project_path",
        str(context.project_path),
        option,
        subject,
    ]
    completed = subprocess.run(
        command,
        cwd=context.project.directory,
        env={
            **os.environ,
            "evm_compiler": "ise",
            "evm_architecture": platform.machine().lower(),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        details = (completed.stdout + "\n" + completed.stderr).strip()
        raise EvmError(
            f"AutoTest discovery command exited with {completed.returncode}: "
            f"{' '.join(command)}\n{details}"
        )
    return completed.stdout + "\n" + completed.stderr


def _parse_descendants(output: str) -> tuple[str, ...]:
    descendants = {
        line.strip()
        for line in output.splitlines()
        if line[:1].isspace()
        and _EIFFEL_IDENTIFIER_RE.fullmatch(line.strip()) is not None
        and line.strip().upper() != "EQA_TEST_SET"
    }
    return tuple(sorted(descendants))


def _parse_test_features(output: str) -> tuple[str, ...]:
    in_features = False
    features: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "feature" or stripped.startswith("feature --"):
            in_features = True
            continue
        if in_features and stripped.startswith("end -- class"):
            break
        if in_features and _EIFFEL_IDENTIFIER_RE.fullmatch(stripped) is not None:
            features.append(stripped)
    return tuple(features)


def _generate_runner_project(
    project: Project,
    request: BuildRequest,
    cases: tuple[AutoTestCase, ...],
) -> Project:
    autotest_directory = project.state_directory / "autotest" / project.name
    state_directory = autotest_directory / request.target
    source_directory = state_directory / "generated"
    source_path = source_directory / "evm_autotest_application.e"
    ecf_path = state_directory / "autotest.ecf"
    atomic_write(source_path, _runner_source(cases).encode())
    atomic_write(ecf_path, _runner_ecf(project.ecf_path, request.target, source_directory))
    return dataclasses.replace(
        project,
        kind="application",
        ecf_path=ecf_path,
        ecf_managed=False,
        build_root=autotest_directory / "build",
    )


def _runner_ecf(ecf_path: Path, target_name: str, source_directory: Path) -> bytes:
    root = parse_ecf(ecf_path).getroot()
    namespace = etree.QName(root).namespace
    _make_locations_absolute(root, ecf_path.parent)
    targets = root.xpath(
        "/*[local-name()='system']/*[local-name()='target'][@name=$name]",
        name=target_name,
    )
    if len(targets) != 1:
        raise EvmError(f"AutoTest target {target_name!r} was not found in {ecf_path}")
    target = targets[0]
    for root_element in target.xpath("./*[local-name()='root']"):
        target.remove(root_element)
    target.insert(
        0,
        etree.Element(
            f"{{{namespace}}}root",
            **{"class": "EVM_AUTOTEST_APPLICATION", "feature": "make"},
        ),
    )
    if not _inherits_testing_library(root, target):
        etree.SubElement(
            target,
            f"{{{namespace}}}library",
            name="testing",
            location="${ISE_LIBRARY}/library/testing/testing.ecf",
            readonly="true",
        )
    etree.SubElement(
        target,
        f"{{{namespace}}}cluster",
        name="evm_autotest_generated",
        location=str(source_directory),
        recursive="false",
    )
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def _make_locations_absolute(system: etree._Element, base_directory: Path) -> None:
    for element in system.xpath("//*[@location]"):
        location = element.get("location")
        if location is None or "$" in location or Path(location).is_absolute():
            continue
        element.set("location", str((base_directory / location).resolve()))


def _inherits_testing_library(system: etree._Element, target: etree._Element) -> bool:
    current: etree._Element | None = target
    while current is not None:
        if current.xpath("./*[local-name()='library'][@name='testing']"):
            return True
        parent_name = current.get("extends")
        if parent_name is None:
            return False
        parents = system.xpath(
            "./*[local-name()='target'][@name=$name]",
            name=parent_name,
        )
        current = parents[0] if len(parents) == 1 else None
    return False


def _compile_runner(
    project: Project,
    request: BuildRequest,
    toolchain: Toolchain,
) -> Path:
    build_directory = prepare_build_directory(toolchain, project, request, clean=True)
    command = compiler_command(toolchain, project, request)
    run_compiler(command, build_directory, capture_output=True)
    executable = next(
        (
            candidate
            for candidate in artifact_candidates(toolchain, project, request)
            if candidate.is_file()
        ),
        None,
    )
    if executable is None:
        raise EvmError("AutoTest runner compiled but its executable was not found")
    return executable


def _run_case(
    executable: Path,
    working_directory: Path,
    test_case: AutoTestCase,
) -> AutoTestDiagnostic:
    completed = subprocess.run(
        [str(executable), test_case.class_name, test_case.feature],
        cwd=working_directory,
        check=False,
        capture_output=True,
        text=True,
    )
    statuses = [
        line.removeprefix(_RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(_RESULT_PREFIX)
    ]
    if len(statuses) != 1 or statuses[0] not in {"passed", "failed", "unresolved"}:
        details = (completed.stdout + "\n" + completed.stderr).strip()
        raise EvmError(
            f"AutoTest runner returned an invalid result for "
            f"{test_case.class_name}.{test_case.feature}\n{details}"
        )
    expected_exit_code = {"passed": 0, "failed": 1, "unresolved": 2}[statuses[0]]
    if completed.returncode != expected_exit_code:
        raise EvmError(
            f"AutoTest runner exited with {completed.returncode} for "
            f"{test_case.class_name}.{test_case.feature}"
        )
    return AutoTestDiagnostic(
        test_case=test_case,
        status=statuses[0],
        assertion=_prefixed_value(completed.stdout, _ASSERTION_PREFIX),
        exception_class=_prefixed_value(completed.stdout, _EXCEPTION_CLASS_PREFIX),
        exception_feature=_prefixed_value(completed.stdout, _EXCEPTION_FEATURE_PREFIX),
        exception_code=_prefixed_integer(completed.stdout, _EXCEPTION_CODE_PREFIX),
        exception_tag=_prefixed_value(completed.stdout, _EXCEPTION_TAG_PREFIX),
        breakpoint_slot=_prefixed_positive_integer(
            completed.stdout,
            _BREAKPOINT_SLOT_PREFIX,
        ),
        test_invalid=_prefixed_boolean(completed.stdout, _TEST_INVALID_PREFIX),
        trace_valid=_prefixed_boolean(completed.stdout, _TRACE_VALID_PREFIX),
        output=_marked_section(completed.stdout, _OUTPUT_BEGIN, _OUTPUT_END),
        stderr=completed.stderr.strip() or None,
        trace=_marked_section(completed.stdout, _TRACE_BEGIN, _TRACE_END),
    )


def _run_raw_case(
    executable: Path,
    working_directory: Path,
    test_case: AutoTestCase,
) -> str:
    completed = subprocess.run(
        [str(executable), test_case.class_name, test_case.feature, "--raw"],
        cwd=working_directory,
        check=False,
    )
    statuses = {0: "passed", 1: "failed", 2: "unresolved"}
    if completed.returncode not in statuses:
        raise EvmError(
            f"AutoTest runner exited with {completed.returncode} for {test_case.qualified_name}"
        )
    return statuses[completed.returncode]


def _prefixed_value(output: str, prefix: str) -> str | None:
    values = [line.removeprefix(prefix) for line in output.splitlines() if line.startswith(prefix)]
    return values[0] if values and values[0] else None


def _prefixed_integer(output: str, prefix: str) -> int | None:
    value = _prefixed_value(output, prefix)
    return int(value) if value is not None and value.isdigit() else None


def _prefixed_positive_integer(output: str, prefix: str) -> int | None:
    value = _prefixed_integer(output, prefix)
    return value if value is not None and value > 0 else None


def _prefixed_boolean(output: str, prefix: str) -> bool | None:
    value = _prefixed_value(output, prefix)
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _marked_section(output: str, begin: str, end: str) -> str | None:
    lines = output.splitlines()
    try:
        first = lines.index(begin) + 1
        last = lines.index(end, first)
    except ValueError:
        return None
    value = "\n".join(lines[first:last]).strip()
    return value or None


def _runner_source(cases: tuple[AutoTestCase, ...]) -> str:
    branches = "\n".join(
        _selection_branch(index, test_case) for index, test_case in enumerate(cases)
    )
    executions = "\n\n".join(
        _execution_feature(index, test_case) for index, test_case in enumerate(cases)
    )
    invocations = "\n\n".join(
        _invocation_feature(index, test_case) for index, test_case in enumerate(cases)
    )
    return f"""class
    EVM_AUTOTEST_APPLICATION

inherit
    ARGUMENTS_32
    EXCEPTIONS

create
    make

feature {{NONE}} -- Initialization

    make
        do
            if
                argument_count = 2 or else
                (argument_count = 3 and then argument (3).same_string ("--raw"))
            then
{branches}
                else
                    die (2)
                end
            else
                die (2)
            end
        end

feature {{NONE}} -- Execution

{executions}

{invocations}

    report (test_result: EQA_PARTIAL_RESULT)
        local
            test_exception: detachable EQA_TEST_INVOCATION_EXCEPTION
        do
            if argument_count = 3 then
                report_raw (test_result)
            else
                io.put_string ("{_RESULT_PREFIX}")
                if test_result.is_pass then
                    io.put_string ("passed")
                elseif test_result.is_fail then
                    io.put_string ("failed")
                else
                    io.put_string ("unresolved")
                end
                io.put_new_line
                if not test_result.is_pass then
                    if not test_result.tag.is_empty then
                        report_string_32 ("{_ASSERTION_PREFIX}", test_result.tag)
                    end
                    if attached test_result.setup_response.exception as setup_exception then
                        test_exception := setup_exception
                    elseif attached {{EQA_RESULT}} test_result as full_result then
                        if attached full_result.test_response.exception as feature_exception then
                            test_exception := feature_exception
                        elseif
                            attached full_result.teardown_response.exception as teardown_exception
                        then
                            test_exception := teardown_exception
                        end
                    end
                    if attached test_exception as reported_exception then
                        report_string_8 (
                            "{_EXCEPTION_CLASS_PREFIX}", reported_exception.class_name
                        )
                        report_string_8 (
                            "{_EXCEPTION_FEATURE_PREFIX}", reported_exception.recipient_name
                        )
                        report_string_8 (
                            "{_EXCEPTION_CODE_PREFIX}", reported_exception.code.out
                        )
                        report_string_32 (
                            "{_EXCEPTION_TAG_PREFIX}", reported_exception.tag_name
                        )
                        report_string_8 (
                            "{_BREAKPOINT_SLOT_PREFIX}",
                            reported_exception.break_point_slot.out
                        )
                        io.put_string ("{_TEST_INVALID_PREFIX}")
                        if reported_exception.is_test_invalid then
                            io.put_string ("true")
                        else
                            io.put_string ("false")
                        end
                        io.put_new_line
                        io.put_string ("{_TRACE_VALID_PREFIX}")
                        if reported_exception.is_trace_valid then
                            io.put_string ("true")
                        else
                            io.put_string ("false")
                        end
                        io.put_new_line
                        if not reported_exception.trace.is_empty then
                            report_section_32 (
                                "{_TRACE_BEGIN}", reported_exception.trace, "{_TRACE_END}"
                            )
                        end
                    end
                    if not test_result.output.is_empty then
                        report_section_8 (
                            "{_OUTPUT_BEGIN}", test_result.output, "{_OUTPUT_END}"
                        )
                    end
                end
            end
            io.output.flush
            if test_result.is_fail then
                die (1)
            elseif test_result.is_unresolved then
                die (2)
            end
        end

    report_raw (test_result: EQA_PARTIAL_RESULT)
        local
            test_exception: detachable EQA_TEST_INVOCATION_EXCEPTION
        do
            if test_result.is_pass then
                io.put_string ("PASS ")
            elseif test_result.is_fail then
                io.put_string ("FAIL ")
            else
                io.put_string ("UNRESOLVED ")
            end
            io.put_string_32 (argument (1))
            io.put_character ('.')
            io.put_string_32 (argument (2))
            io.put_new_line
            if not test_result.tag.is_empty then
                report_string_32 ("Assertion: ", test_result.tag)
            end
            if attached test_result.setup_response.exception as setup_exception then
                test_exception := setup_exception
            elseif attached {{EQA_RESULT}} test_result as full_result then
                if attached full_result.test_response.exception as feature_exception then
                    test_exception := feature_exception
                elseif attached full_result.teardown_response.exception as teardown_exception then
                    test_exception := teardown_exception
                end
            end
            if attached test_exception as reported_exception then
                report_string_8 ("Exception class: ", reported_exception.class_name)
                report_string_8 ("Exception feature: ", reported_exception.recipient_name)
                report_string_32 ("Exception tag: ", reported_exception.tag_name)
                if not reported_exception.trace.is_empty then
                    io.put_string ("Trace:%N")
                    io.put_string_32 (reported_exception.trace)
                    io.put_new_line
                end
            end
            if not test_result.output.is_empty then
                io.put_string ("Output:%N")
                io.put_string (test_result.output)
                io.put_new_line
            end
        end

    report_string_8 (prefix, value: READABLE_STRING_8)
        do
            io.put_string (prefix)
            io.put_string (value)
            io.put_new_line
        end

    report_string_32 (prefix: READABLE_STRING_8; value: READABLE_STRING_32)
        do
            io.put_string (prefix)
            io.put_string_32 (value)
            io.put_new_line
        end

    report_section_8 (
        opening: READABLE_STRING_8;
        value: READABLE_STRING_8;
        closing: READABLE_STRING_8
    )
        do
            io.put_string (opening)
            io.put_new_line
            io.put_string (value)
            io.put_new_line
            io.put_string (closing)
            io.put_new_line
        end

    report_section_32 (
        opening: READABLE_STRING_8;
        value: READABLE_STRING_32;
        closing: READABLE_STRING_8
    )
        do
            io.put_string (opening)
            io.put_new_line
            io.put_string_32 (value)
            io.put_new_line
            io.put_string (closing)
            io.put_new_line
        end

end
"""


def _selection_branch(index: int, test_case: AutoTestCase) -> str:
    keyword = "if" if index == 0 else "elseif"
    return (
        f'                {keyword} argument (1).same_string ("{test_case.class_name}") and '
        f'argument (2).same_string ("{test_case.feature}") then\n'
        f"                    execute_{index}"
    )


def _execution_feature(index: int, test_case: AutoTestCase) -> str:
    return f"""    execute_{index}
        local
            evaluator: EQA_TEST_EVALUATOR [{test_case.class_name}]
        do
            create evaluator
            report (evaluator.execute (agent invoke_{index}))
        end"""


def _invocation_feature(index: int, test_case: AutoTestCase) -> str:
    return f"""    invoke_{index} (test_set: EQA_TEST_SET)
        do
            check attached {{{test_case.class_name}}} test_set as selected_test_set then
                selected_test_set.{test_case.feature}
            end
        end"""
