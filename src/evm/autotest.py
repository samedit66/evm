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

from evm.ecf import ECF_NAMESPACE, parse_ecf
from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.model import BuildRequest, Project
from evm.project import prepare_project
from evm.toolchains import (
    Toolchain,
    artifact_candidates,
    compiler_command,
    prepare_build_directory,
    run_compiler,
)

_EIFFEL_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_RESULT_PREFIX = "EVM_AUTOTEST_RESULT|"


@dataclass(frozen=True, order=True)
class AutoTestCase:
    class_name: str
    feature: str


@dataclass(frozen=True)
class AutoTestExecution:
    tests: int
    passed: int
    failed: int
    unresolved: int

    @property
    def exit_code(self) -> int:
        return 0 if self.failed == 0 and self.unresolved == 0 else 1


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
    class_name: str | None,
    feature: str | None,
) -> AutoTestExecution:
    if toolchain.adapter != "ise":
        raise EvmError("AutoTest runner requires the ISE compiler adapter")
    prepare_project(
        project,
        regenerate=build_request.regenerate_ecf,
        release=build_request.release,
        offline=build_request.offline,
    )
    discovery_directory = prepare_build_directory(
        toolchain,
        project,
        build_request,
        check_only=True,
    )
    discovery = _DiscoveryContext(
        toolchain=toolchain,
        project=project,
        request=build_request,
        project_path=discovery_directory,
    )
    discovered = _discover_autotest_cases(discovery)
    selected = _filter_autotest_cases(discovered, class_name, feature)
    generated_project = _generate_runner_project(project, build_request, discovered)
    executable = _compile_runner(generated_project, build_request, toolchain)
    statuses = tuple(_run_case(executable, project.directory, test_case) for test_case in selected)
    return AutoTestExecution(
        tests=len(statuses),
        passed=statuses.count("passed"),
        failed=statuses.count("failed"),
        unresolved=statuses.count("unresolved"),
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
        ecf_path=ecf_path,
        ecf_managed=False,
        build_root=autotest_directory / "build",
    )


def _runner_ecf(ecf_path: Path, target_name: str, source_directory: Path) -> bytes:
    root = parse_ecf(ecf_path).getroot()
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
            f"{{{ECF_NAMESPACE}}}root",
            **{"class": "EVM_AUTOTEST_APPLICATION", "feature": "make"},
        ),
    )
    if not _inherits_testing_library(root, target):
        etree.SubElement(
            target,
            f"{{{ECF_NAMESPACE}}}library",
            name="testing",
            location="${ISE_LIBRARY}/library/testing/testing.ecf",
            readonly="true",
        )
    etree.SubElement(
        target,
        f"{{{ECF_NAMESPACE}}}cluster",
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
    build_directory = prepare_build_directory(toolchain, project, request)
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


def _run_case(executable: Path, working_directory: Path, test_case: AutoTestCase) -> str:
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
    expected_exit_code = 0 if statuses[0] == "passed" else 1
    if completed.returncode != expected_exit_code:
        raise EvmError(
            f"AutoTest runner exited with {completed.returncode} for "
            f"{test_case.class_name}.{test_case.feature}"
        )
    return statuses[0]


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
            if argument_count = 2 then
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
        do
            io.put_string ("{_RESULT_PREFIX}")
            if test_result.is_pass then
                io.put_string ("passed")
            elseif test_result.is_fail then
                io.put_string ("failed")
            else
                io.put_string ("unresolved")
            end
            io.put_new_line
            io.output.flush
            if not test_result.is_pass then
                die (1)
            end
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
