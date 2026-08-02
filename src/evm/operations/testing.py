"""Test adapter selection, execution, and result normalization."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from evm.errors import EvmError
from evm.model import BuildRequest, Project
from evm.operations.autotest import AutoTestDiagnostic, AutoTestRunRequest, run_autotest
from evm.project.workflow import compile_project, effective_sources, prepare_project
from evm.toolchain.selection import artifact_candidates, select_toolchain


@dataclass(frozen=True)
class TestRequest:
    compiler: str | None = None
    release: bool = False
    class_name: str | None = None
    feature: str | None = None
    offline: bool = False
    regenerate_ecf: bool = False
    raw: bool = False
    runner: str | None = None
    defines: tuple[str, ...] = ()
    default_test: bool = False


@dataclass(frozen=True)
class TestDiagnostic:
    name: str
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
    source_path: str | None = None
    source_line: int | None = None
    source_text: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"name": self.name, "status": self.status}
        for key, value in (
            ("assertion", self.assertion),
            ("exception_class", self.exception_class),
            ("exception_feature", self.exception_feature),
            ("exception_code", self.exception_code),
            ("exception_tag", self.exception_tag),
            ("breakpoint_slot", self.breakpoint_slot),
            ("test_invalid", self.test_invalid),
            ("trace_valid", self.trace_valid),
            ("output", self.output),
            ("stderr", self.stderr),
            ("trace", self.trace),
            ("source_path", self.source_path),
            ("source_line", self.source_line),
            ("source_text", self.source_text),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class TestResult:
    status: str
    exit_code: int
    elapsed_seconds: float
    compiler: str
    runner: str
    tests: int | None = None
    passed: int | None = None
    failed: int | None = None
    unresolved: int | None = None
    details: tuple[TestDiagnostic, ...] = ()
    stdout: str | None = None
    stderr: str | None = None

    @property
    def failed_tests(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.details if item.status == "failed")

    @property
    def unresolved_tests(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.details if item.status == "unresolved")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "exit_code": self.exit_code,
            "time_seconds": round(self.elapsed_seconds, 3),
            "compiler": self.compiler,
            "runner": self.runner,
        }
        for key, value in (
            ("tests", self.tests),
            ("passed", self.passed),
            ("failed", self.failed),
            ("unresolved", self.unresolved),
        ):
            if value is not None:
                result[key] = value
        if self.tests is not None:
            result["failed_tests"] = list(self.failed_tests)
            result["unresolved_tests"] = list(self.unresolved_tests)
            result["test_details"] = [item.as_dict() for item in self.details]
        if self.stdout:
            result["stdout"] = self.stdout
        if self.stderr:
            result["stderr"] = self.stderr
        return result


def test_project(project: Project, request: TestRequest) -> TestResult:
    if project.test is None:
        raise EvmError(
            "no test target is configured; define [test].target or a target named 'test'"
        )
    build_request = BuildRequest(
        compiler=request.compiler,
        target=project.test.target,
        release=request.release,
        regenerate_ecf=request.regenerate_ecf,
        offline=request.offline,
    )
    selected = select_toolchain(project, request.compiler)
    getest = shutil.which("getest")
    getest_configuration = _getest_configuration(project, selected.adapter)
    configured_runner = request.runner or project.test.runner
    _validate_runner_capabilities(configured_runner, request)
    started = time.monotonic()
    counts: tuple[int, int, int, int] | None = None
    details: tuple[TestDiagnostic, ...] = ()
    runner_stdout: str | None = None
    runner_stderr: str | None = None
    getest_capability_requested = bool(request.defines or request.default_test)
    if configured_runner == "autotest":
        execution = run_autotest(
            project,
            build_request,
            selected,
            AutoTestRunRequest(request.class_name, request.feature, request.raw),
        )
        exit_code = execution.exit_code
        runner = "autotest"
        counts = (
            execution.tests,
            execution.passed,
            execution.failed,
            execution.unresolved,
        )
        details = tuple(
            _test_diagnostic(project, project.test.target, item) for item in execution.diagnostics
        )
    elif configured_runner == "getest" or (
        configured_runner == "auto"
        and (
            getest_capability_requested or (getest is not None and getest_configuration is not None)
        )
    ):
        if getest is None:
            raise EvmError("test.runner is 'getest', but getest was not found in PATH")
        if getest_configuration is None:
            raise EvmError("test.runner is 'getest', but no getest configuration was found")
        exit_code, runner_stdout, runner_stderr = _run_getest(
            project,
            build_request,
            request,
            getest,
            getest_configuration,
        )
        runner = "getest"
        counts = _parse_getest_summary(runner_stdout)
    else:
        if request.class_name is not None or request.feature is not None:
            raise EvmError(f"filter unsupported by {selected.adapter} test target adapter")
        toolchain = compile_project(project, build_request, announce=False)
        executable = next(
            (
                candidate
                for candidate in artifact_candidates(toolchain, project, build_request)
                if candidate.is_file()
            ),
            None,
        )
        if executable is None:
            raise EvmError("test build succeeded but the test executable was not found")
        completed = subprocess.run(
            [str(executable)],
            cwd=project.directory,
            check=False,
            capture_output=not request.raw,
            text=not request.raw,
        )
        exit_code = completed.returncode
        runner_stdout = completed.stdout or None
        runner_stderr = completed.stderr or None
        runner = "test-target"
    elapsed = time.monotonic() - started
    return TestResult(
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        compiler=f"{selected.adapter} {selected.version}",
        runner=runner,
        tests=None if counts is None else counts[0],
        passed=None if counts is None else counts[1],
        failed=None if counts is None else counts[2],
        unresolved=None if counts is None else counts[3],
        details=details,
        stdout=runner_stdout,
        stderr=runner_stderr,
    )


def _test_diagnostic(
    project: Project,
    target: str,
    diagnostic: AutoTestDiagnostic,
) -> TestDiagnostic:
    source = _locate_test_source(project, target, diagnostic)
    return TestDiagnostic(
        name=diagnostic.test_case.qualified_name,
        status=diagnostic.status,
        assertion=diagnostic.assertion,
        exception_class=diagnostic.exception_class,
        exception_feature=diagnostic.exception_feature,
        exception_code=diagnostic.exception_code,
        exception_tag=diagnostic.exception_tag,
        breakpoint_slot=diagnostic.breakpoint_slot,
        test_invalid=diagnostic.test_invalid,
        trace_valid=diagnostic.trace_valid,
        output=diagnostic.output,
        stderr=diagnostic.stderr,
        trace=diagnostic.trace,
        source_path=None if source is None else source.path,
        source_line=None if source is None else source.line,
        source_text=None if source is None else source.text,
    )


@dataclass(frozen=True)
class _SourceLocation:
    path: str
    line: int | None = None
    text: str | None = None


def _locate_test_source(
    project: Project,
    target: str,
    diagnostic: AutoTestDiagnostic,
) -> _SourceLocation | None:
    source_path = _find_class_source(project, target, diagnostic.test_case.class_name)
    if source_path is None:
        return None
    try:
        relative_path = str(source_path.relative_to(project.directory))
    except ValueError:
        relative_path = str(source_path)
    lines = source_path.read_text(errors="replace").splitlines()
    feature_line = _find_feature_line(lines, diagnostic.test_case.feature)
    assertion_line = _find_assertion_line(lines, diagnostic.assertion)
    line = assertion_line or feature_line
    return _SourceLocation(
        relative_path,
        line,
        None if line is None else lines[line - 1].strip(),
    )


def _find_class_source(project: Project, target: str, class_name: str) -> Path | None:
    source_files = tuple(_eiffel_sources(project, target))
    conventional_name = f"{class_name.casefold()}.e"
    for source_path in source_files:
        if source_path.name.casefold() == conventional_name:
            return source_path
    class_pattern = re.compile(
        rf"^\s*(?:(?:deferred|expanded|frozen)\s+)?class\s+{re.escape(class_name)}\b",
        re.IGNORECASE | re.MULTILINE,
    )
    return next(
        (
            source_path
            for source_path in source_files
            if class_pattern.search(source_path.read_text(errors="replace"))
        ),
        None,
    )


def _eiffel_sources(project: Project, target: str) -> Iterator[Path]:
    for configured_source in effective_sources(project, target):
        source_path = project.directory / configured_source
        if source_path.is_file() and source_path.suffix.casefold() == ".e":
            yield source_path
        elif source_path.is_dir():
            yield from sorted(source_path.rglob("*.e"))


def _find_feature_line(lines: list[str], feature: str) -> int | None:
    declaration = re.compile(
        rf"^\s*{re.escape(feature)}(?:\s*(?:\(|:|alias\b|assign\b|$))",
        re.IGNORECASE,
    )
    return next(
        (line_number for line_number, line in enumerate(lines, 1) if declaration.match(line)),
        None,
    )


def _find_assertion_line(lines: list[str], assertion: str | None) -> int | None:
    if assertion is None:
        return None
    assertion_pattern = re.compile(
        rf"\bassert\s*\(\s*\"{re.escape(assertion)}\"\s*,",
        re.IGNORECASE,
    )
    matches = [
        line_number for line_number, line in enumerate(lines, 1) if assertion_pattern.search(line)
    ]
    return matches[0] if len(matches) == 1 else None


def _run_getest(
    project: Project,
    build_request: BuildRequest,
    request: TestRequest,
    executable: str,
    configuration: Path,
) -> tuple[int, str | None, str | None]:
    prepare_project(
        project,
        regenerate=build_request.regenerate_ecf,
        release=build_request.release,
        offline=build_request.offline,
    )
    command = [executable, str(configuration)]
    if request.class_name is not None:
        command.append(f"--class={_exact_getest_pattern(request.class_name)}")
    if request.feature is not None:
        command.append(f"--feature={_exact_getest_pattern(request.feature)}")
    for definition in request.defines:
        command.append(f"--define={definition}")
    if request.default_test:
        command.append("--default_test")
    completed = subprocess.run(
        command,
        cwd=project.directory,
        check=False,
        capture_output=not request.raw,
        text=not request.raw,
    )
    return completed.returncode, completed.stdout or None, completed.stderr or None


def _getest_configuration(project: Project, adapter: str) -> Path | None:
    adapter_filename = {"gobo": "getest.ge", "ise": "getest.ise"}.get(adapter)
    filenames = (adapter_filename, "getest.cfg") if adapter_filename else ("getest.cfg",)
    return next(
        (
            project.directory / filename
            for filename in filenames
            if filename is not None and (project.directory / filename).is_file()
        ),
        None,
    )


def _exact_getest_pattern(name: str) -> str:
    return f"^{re.escape(name)}$"


def _validate_runner_capabilities(runner: str, request: TestRequest) -> None:
    if runner not in {"auto", "autotest", "getest", "target"}:
        raise EvmError("test runner must be one of: auto, autotest, getest, target")
    getest_features = []
    if request.defines:
        getest_features.append("--define")
    if request.default_test:
        getest_features.append("--default-test")
    if getest_features and runner not in {"auto", "getest"}:
        options = ", ".join(getest_features)
        raise EvmError(
            f"{options} uses capabilities unsupported by {runner}; select the getest runner"
        )


def _parse_getest_summary(output: str | None) -> tuple[int, int, int, int] | None:
    if not output:
        return None
    patterns = {
        "passed": re.compile(r"^#\s*passed:\s*(\d+)\s+tests?", re.IGNORECASE | re.MULTILINE),
        "failed": re.compile(r"^#\s*failed:\s*(\d+)\s+tests?", re.IGNORECASE | re.MULTILINE),
        "aborted": re.compile(r"^#\s*aborted:\s*(\d+)\s+tests?", re.IGNORECASE | re.MULTILINE),
        "total": re.compile(r"^#\s*total:\s*(\d+)\s+tests?", re.IGNORECASE | re.MULTILINE),
    }
    matches = {name: pattern.search(output) for name, pattern in patterns.items()}
    if any(match is None for match in matches.values()):
        return None
    values = {name: int(match.group(1)) for name, match in matches.items() if match is not None}
    return values["total"], values["passed"], values["failed"], values["aborted"]
