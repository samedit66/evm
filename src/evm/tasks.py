"""Safe execution of manifest-defined project workflows."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from evm.errors import EvmError
from evm.model import Project, Task

_PUBLIC_COMMANDS = {
    "add",
    "build",
    "check",
    "clean",
    "deps",
    "doc",
    "discover",
    "explain",
    "import",
    "init",
    "install",
    "lint",
    "new",
    "remove",
    "run",
    "task",
    "test",
    "update",
}


@dataclass(frozen=True)
class _TaskExecution:
    tasks: dict[str, Task]
    run_evm_command: Callable[[tuple[str, ...]], int]
    allow_build_scripts: bool
    working_directory: Path


def run_task(
    project: Project,
    name: str,
    run_evm_command: Callable[[tuple[str, ...]], int],
    *,
    allow_build_scripts: bool = False,
) -> None:
    execution = _TaskExecution(
        tasks={task.name: task for task in project.tasks},
        run_evm_command=run_evm_command,
        allow_build_scripts=allow_build_scripts,
        working_directory=project.directory,
    )
    _run_task(execution, name, ())


def _run_task(
    execution: _TaskExecution,
    name: str,
    active: tuple[str, ...],
) -> None:
    if name in active:
        cycle = " -> ".join((*active, name))
        raise EvmError(f"recursive task invocation detected: {cycle}")
    try:
        task = execution.tasks[name]
    except KeyError as error:
        raise EvmError(f"unknown task {name!r}") from error
    for step in task.steps:
        if step.shell is not None:
            if not execution.allow_build_scripts:
                raise EvmError(
                    f"task {name!r} contains a shell step; rerun with --allow-build-scripts"
                )
            completed = subprocess.run(
                step.shell,
                cwd=execution.working_directory,
                env=os.environ.copy(),
                shell=True,
                check=False,
            )
            if completed.returncode != 0:
                raise EvmError(
                    f"shell step in task {name!r} exited with status {completed.returncode}"
                )
            continue
        if step.command is None:
            raise AssertionError("validated task step has no action")
        if step.command not in _PUBLIC_COMMANDS:
            raise EvmError(f"task {name!r} uses unknown EVM command {step.command!r}")
        if step.command == "task":
            if not step.arguments:
                raise EvmError(f"task {name!r} invokes task without a name")
            _run_task(
                execution,
                step.arguments[0],
                (*active, name),
            )
            continue
        exit_code = execution.run_evm_command((step.command, *step.arguments))
        if exit_code != 0:
            raise EvmError(
                f"EVM step {step.command!r} in task {name!r} exited with status {exit_code}"
            )
