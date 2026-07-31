"""Test adapter selection, execution, and result normalization."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass

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

    def as_dict(self) -> dict[str, str | int | float]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "time_seconds": round(self.elapsed_seconds, 3),
            "compiler": self.compiler,
            "runner": self.runner,
        }


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
    getest = shutil.which("getest") if selected.adapter == "gobo" else None
    started = time.monotonic()
    if getest is not None:
        exit_code = _run_getest(project, build_request, request, getest)
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
    )


def _run_getest(
    project: Project,
    build_request: BuildRequest,
    request: TestRequest,
    executable: str,
) -> int:
    prepare_project(
        project,
        regenerate=build_request.regenerate_ecf,
        release=build_request.release,
        offline=build_request.offline,
    )
    command = [
        executable,
        f"--config={project.ecf_path}",
        f"--target={build_request.target}",
    ]
    if request.class_name is not None:
        command.append(f"--class={request.class_name}")
    if request.feature is not None:
        command.append(f"--feature={request.feature}")
    return subprocess.run(
        command,
        cwd=project.directory,
        check=False,
        capture_output=request.capture_output,
        text=request.capture_output,
    ).returncode
