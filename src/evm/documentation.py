"""HTML documentation generation through native Eiffel toolchains."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from evm.companion_tools import companion_tool, installed_gobo_tool
from evm.errors import EvmError
from evm.model import Project
from evm.project.workflow import prepare_project
from evm.toolchains import Toolchain, select_toolchain, toolchain_environment_values

_BACKENDS = {"gedoc", "eiffelstudio"}


@dataclass(frozen=True)
class DocumentationRequest:
    compiler: str | None = None
    backend: str | None = None
    target: str | None = None
    output: Path | None = None
    offline: bool = False
    regenerate_ecf: bool = False


@dataclass(frozen=True)
class DocumentationResult:
    status: str
    exit_code: int
    backend: str
    compiler: str
    output_directory: Path
    stdout: str | None = None
    stderr: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "exit_code": self.exit_code,
            "backend": self.backend,
            "compiler": self.compiler,
            "output_directory": str(self.output_directory),
        }
        if self.stdout:
            result["stdout"] = self.stdout
        if self.stderr:
            result["stderr"] = self.stderr
        return result


def document_project(project: Project, request: DocumentationRequest) -> DocumentationResult:
    toolchain = select_toolchain(project, request.compiler)
    backend = request.backend or ("gedoc" if toolchain.adapter == "gobo" else "eiffelstudio")
    if backend not in _BACKENDS:
        raise EvmError("documentation backend must be one of: eiffelstudio, gedoc")
    prepare_project(
        project,
        regenerate=request.regenerate_ecf,
        offline=request.offline,
    )
    output = _output_directory(project, request)
    output.mkdir(parents=True, exist_ok=True)
    target = request.target or project.default_target
    command = _documentation_command(project, toolchain, backend, target, output)
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
    generated_output = output / "Documentation" if backend == "eiffelstudio" else output
    return DocumentationResult(
        "passed" if completed.returncode == 0 else "failed",
        completed.returncode,
        backend,
        toolchain.display_name,
        generated_output,
        completed.stdout or None,
        completed.stderr or None,
    )


def _output_directory(project: Project, request: DocumentationRequest) -> Path:
    if request.output is None:
        return project.directory / "build" / "doc" / (request.target or project.default_target)
    return request.output if request.output.is_absolute() else project.directory / request.output


def _documentation_command(
    project: Project,
    toolchain: Toolchain,
    backend: str,
    target: str,
    output: Path,
) -> list[str]:
    if backend == "gedoc":
        executable = (
            companion_tool(toolchain, "gedoc")
            if toolchain.adapter == "gobo"
            else installed_gobo_tool("gedoc")
        )
        command = [
            str(executable),
            str(project.ecf_path),
            "--format=html_ise_stylesheet",
            f"--target={target}",
            f"--output={output}",
            "--force",
        ]
        if toolchain.adapter == "ise":
            command.append(f"--ise={toolchain.version}")
        return command
    if toolchain.adapter != "ise":
        raise EvmError("documentation backend 'eiffelstudio' requires an ISE Eiffel toolchain")
    return [
        str(toolchain.executable),
        "-batch",
        "-config",
        str(project.ecf_path),
        "-target",
        target,
        "-project_path",
        str(output),
        "-filter",
        "html-stylesheet",
        "-all",
    ]
