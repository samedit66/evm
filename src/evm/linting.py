"""Static-analysis backend selection and execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from evm.companion_tools import companion_tool, installed_gobo_tool
from evm.errors import EvmError
from evm.model import Project
from evm.project import prepare_project
from evm.toolchains import Toolchain, select_toolchain, toolchain_environment_values

_BACKENDS = {"gelint", "code-analyzer"}


@dataclass(frozen=True)
class LintRequest:
    compiler: str | None = None
    backend: str | None = None
    target: str | None = None
    catcall: bool = False
    flat: bool = False
    standard: str | None = None
    threads: int | None = None
    rules: tuple[str, ...] = ()
    profile: Path | None = None
    offline: bool = False
    regenerate_ecf: bool = False


@dataclass(frozen=True)
class LintResult:
    status: str
    exit_code: int
    backend: str
    compiler: str
    stdout: str | None = None
    stderr: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "exit_code": self.exit_code,
            "backend": self.backend,
            "compiler": self.compiler,
        }
        if self.stdout:
            result["stdout"] = self.stdout
        if self.stderr:
            result["stderr"] = self.stderr
        return result


def lint_project(project: Project, request: LintRequest) -> LintResult:
    toolchain = select_toolchain(project, request.compiler)
    backend = _lint_backend(toolchain, request)
    _validate_backend_options(backend, request)
    prepare_project(
        project,
        regenerate=request.regenerate_ecf,
        offline=request.offline,
    )
    target = request.target or project.default_target
    command = _lint_command(project, toolchain, target, backend, request)
    environment = os.environ.copy()
    environment.update(dict(toolchain_environment_values(command)))
    completed = subprocess.run(
        command,
        cwd=project.directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return LintResult(
        "passed" if completed.returncode == 0 else "failed",
        completed.returncode,
        backend,
        toolchain.display_name,
        completed.stdout or None,
        completed.stderr or None,
    )


def _lint_backend(toolchain: Toolchain, request: LintRequest) -> str:
    backend = request.backend or ("gelint" if toolchain.adapter == "gobo" else "code-analyzer")
    if backend not in _BACKENDS:
        raise EvmError("lint backend must be one of: code-analyzer, gelint")
    return backend


def _validate_backend_options(backend: str, request: LintRequest) -> None:
    if backend == "code-analyzer" and any(
        (request.catcall, request.flat, request.standard is not None, request.threads is not None)
    ):
        raise EvmError("--catcall, --flat, --standard, and --threads require --backend gelint")
    if backend == "gelint" and (request.rules or request.profile is not None):
        raise EvmError("--rules and --profile require --backend code-analyzer")
    if request.standard not in {None, "ecma", "ise"}:
        raise EvmError("--standard must be either ecma or ise")


def _lint_command(
    project: Project,
    toolchain: Toolchain,
    target: str,
    backend: str,
    request: LintRequest,
) -> list[str]:
    if backend == "gelint":
        executable = (
            companion_tool(toolchain, "gelint")
            if toolchain.adapter == "gobo"
            else installed_gobo_tool("gelint")
        )
        command = [str(executable), str(project.ecf_path), f"--target={target}"]
        if toolchain.adapter == "ise":
            command.append(f"--ise={toolchain.version}")
        elif request.standard == "ise":
            ise_semantics = dict(project.requires).get("ise-semantics")
            command.append("--ise" if ise_semantics is None else f"--ise={ise_semantics}")
        elif request.standard == "ecma":
            command.append("--ecma")
        if request.catcall:
            command.append("--catcall")
        if request.flat:
            command.append("--flat")
        if request.threads is not None:
            command.append(f"--thread={request.threads}")
        return command
    if toolchain.adapter != "ise":
        raise EvmError("lint backend 'code-analyzer' requires an ISE Eiffel toolchain")
    command = [
        str(toolchain.executable),
        "-batch",
        "-config",
        str(project.ecf_path),
        "-target",
        target,
        "-ca_class",
        "-all",
    ]
    if request.profile is not None:
        command.extend(("-ca_setting", str(request.profile)))
    if request.rules:
        command.extend(("-ca_rule", ";".join(request.rules)))
    return command
