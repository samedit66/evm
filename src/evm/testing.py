"""Test adapter selection, execution, and result normalization."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evm.autotest import run_autotest
from evm.errors import EvmError
from evm.model import BuildRequest, Project
from evm.project import compile_project, prepare_project
from evm.toolchains import artifact_candidates, select_toolchain


@dataclass(frozen=True)
class TestRequest:
    compiler: str | None = None
    release: bool = False
    class_name: str | None = None
    feature: str | None = None
    offline: bool = False
    regenerate_ecf: bool = False
    capture_output: bool = False


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

    def as_dict(self) -> dict[str, str | int | float]:
        result: dict[str, str | int | float] = {
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
    configured_runner = project.test.runner
    started = time.monotonic()
    counts: tuple[int, int, int, int] | None = None
    if configured_runner == "autotest":
        execution = run_autotest(
            project,
            build_request,
            selected,
            request.class_name,
            request.feature,
        )
        exit_code = execution.exit_code
        runner = "autotest"
        counts = (
            execution.tests,
            execution.passed,
            execution.failed,
            execution.unresolved,
        )
    elif configured_runner == "getest" or (
        configured_runner == "auto" and getest is not None and getest_configuration is not None
    ):
        if getest is None:
            raise EvmError("test.runner is 'getest', but getest was not found in PATH")
        if getest_configuration is None:
            raise EvmError("test.runner is 'getest', but no getest configuration was found")
        exit_code = _run_getest(
            project,
            build_request,
            request,
            getest,
            getest_configuration,
        )
        runner = "getest"
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
        exit_code = subprocess.run(
            [str(executable)],
            cwd=project.directory,
            check=False,
            capture_output=request.capture_output,
            text=request.capture_output,
        ).returncode
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
    )


def _run_getest(
    project: Project,
    build_request: BuildRequest,
    request: TestRequest,
    executable: str,
    configuration: Path,
) -> int:
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
    return subprocess.run(
        command,
        cwd=project.directory,
        check=False,
        capture_output=request.capture_output,
        text=request.capture_output,
    ).returncode


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
