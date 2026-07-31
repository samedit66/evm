"""ISE Eiffel and Gobo compiler adapters."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from evm.errors import EvmError
from evm.model import BuildRequest, CompilerRequirement, Project
from evm.versioning import NumericVersion, satisfies

KNOWN_ADAPTERS = ("ise", "gobo")
_ADAPTER_EXECUTABLES = {"ise": "ec", "gobo": "gec"}
_VERSION_TIMEOUT_SECONDS = 15
_NUMERIC_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


@dataclass(frozen=True)
class Toolchain:
    adapter: str
    executable: Path
    version: NumericVersion
    selection: str
    reason: str

    @property
    def display_name(self) -> str:
        label = "ISE EiffelStudio" if self.adapter == "ise" else "Gobo Eiffel"
        return f"{label} {self.version}"


@dataclass(frozen=True)
class Detection:
    executable: Path | None
    version: NumericVersion | None
    error: str | None


@dataclass(frozen=True)
class _SelectionPolicy:
    candidates: tuple[CompilerRequirement, ...]
    mode: str
    reason: str


def detect_all() -> dict[str, Detection]:
    return {adapter: detect(adapter) for adapter in KNOWN_ADAPTERS}


def detect(adapter: str) -> Detection:
    _validate_adapter(adapter)
    executable_name = _ADAPTER_EXECUTABLES[adapter]
    found = shutil.which(executable_name)
    if found is None:
        return Detection(None, None, f"{executable_name} was not found in PATH")
    command = [found, "-version"] if adapter == "ise" else [found, "--version"]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Detection(Path(found), None, f"version detection failed: {error}")
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        return Detection(
            Path(found),
            None,
            f"version command exited with {completed.returncode}",
        )
    match = _NUMERIC_VERSION_RE.search(output)
    if match is None:
        return Detection(Path(found), None, "numeric version was not reported")
    return Detection(
        Path(found),
        NumericVersion.parse(match.group(0)),
        None,
    )


def select_toolchain(project: Project, explicit: str | None = None) -> Toolchain:
    policy = _selection_policy(project, explicit)
    detections = detect_all()
    rejected: list[str] = []
    for requirement in policy.candidates:
        detected = detections[requirement.adapter]
        rejection = _candidate_rejection(project, requirement, detected)
        if rejection is not None:
            rejected.append(f"{requirement.adapter}: {rejection}")
            continue
        if detected.executable is None or detected.version is None:
            raise AssertionError("accepted toolchain has incomplete detection data")
        return Toolchain(
            requirement.adapter,
            detected.executable,
            detected.version,
            policy.mode,
            policy.reason,
        )
    details = "\n".join(f"  - {item}" for item in rejected)
    raise EvmError(f"no compatible Eiffel toolchain found:\n{details}\nrun `evm discover`")


def _selection_policy(project: Project, explicit: str | None) -> _SelectionPolicy:
    environment = os.environ.get("EVM_COMPILER")
    if explicit is not None:
        _validate_adapter(explicit)
        candidates = (CompilerRequirement(explicit, _constraint_for(project, explicit)),)
        return _SelectionPolicy(candidates, "explicit", "--compiler")
    if environment:
        _validate_adapter(environment)
        candidates = (CompilerRequirement(environment, _constraint_for(project, environment)),)
        return _SelectionPolicy(candidates, "environment", "EVM_COMPILER")
    if project.compilers:
        return _SelectionPolicy(
            project.compilers,
            "automatic",
            "first compatible adapter in compatibility.compilers",
        )
    candidates = tuple(CompilerRequirement(name, None) for name in KNOWN_ADAPTERS)
    return _SelectionPolicy(
        candidates,
        "automatic",
        "built-in adapter priority: ise before gobo",
    )


def _candidate_rejection(
    project: Project,
    requirement: CompilerRequirement,
    detected: Detection,
) -> str | None:
    if detected.executable is None or detected.version is None:
        return detected.error or "toolchain detection is incomplete"
    if not satisfies(detected.version, requirement.constraint):
        return f"version {detected.version} does not satisfy {requirement.constraint}"
    ise_semantics = dict(project.requires).get("ise-semantics")
    if (
        requirement.adapter == "ise"
        and ise_semantics is not None
        and detected.version < NumericVersion.parse(ise_semantics)
    ):
        return f"version {detected.version} is older than required ISE semantics {ise_semantics}"
    return _capability_error(project, requirement.adapter)


def prepare_build_directory(
    toolchain: Toolchain,
    project: Project,
    request: BuildRequest,
    check_only: bool = False,
    *,
    clean: bool = False,
) -> Path:
    directory = _build_directory(toolchain, project, request)
    if (
        clean or (toolchain.adapter == "ise" and project.kind == "library" and not check_only)
    ) and directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def compiler_command(
    toolchain: Toolchain,
    project: Project,
    request: BuildRequest,
    check_only: bool = False,
) -> list[str]:
    ecf = str(project.ecf_path)
    if toolchain.adapter == "ise":
        command = [
            str(toolchain.executable),
            "-batch",
            "-config",
            ecf,
            "-target",
            request.target,
            "-project_path",
            str(_build_directory(toolchain, project, request)),
        ]
        if check_only:
            command.append("-finalize" if request.release else "-melt")
        elif project.kind == "library":
            command.append("-precompile")
            if request.release:
                command.append("-finalize")
            command.append("-c_compile")
        elif request.release:
            command.extend(("-finalize", "-c_compile"))
        else:
            command.extend(("-freeze", "-c_compile"))
    else:
        command = [
            str(toolchain.executable),
            ecf,
            f"--target={request.target}",
            "--variable=evm_compiler=gobo",
            f"--variable=evm_architecture={platform.machine().lower()}",
        ]
        if request.release:
            command.append("--finalize")
        for key, value in project.requires:
            if key == "ise-semantics":
                command.append(f"--ise={value}")
            elif key in {"concurrency", "void-safety"}:
                command.append(f"--capability={key.replace('-', '_')}={value}")
        command.extend(project.compiler_arguments.get("gobo", ()))
    if toolchain.adapter == "ise":
        command.extend(project.compiler_arguments.get("ise", ()))
    return command


def run_compiler(
    command: list[str],
    working_directory: Path,
    *,
    capture_output: bool = False,
) -> None:
    environment = os.environ.copy()
    adapter = "gobo" if Path(command[0]).name == "gec" else "ise"
    environment["evm_compiler"] = adapter
    environment["evm_architecture"] = platform.machine().lower()
    environment["ZIG_GLOBAL_CACHE_DIR"] = str(working_directory / ".zig-global-cache")
    environment["ZIG_LOCAL_CACHE_DIR"] = str(working_directory / ".zig-local-cache")
    completed = subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        check=False,
        capture_output=capture_output,
        text=capture_output,
    )
    if completed.returncode != 0:
        details = ""
        if capture_output:
            output = "\n".join(
                item.strip()
                for item in (completed.stdout, completed.stderr)
                if isinstance(item, str) and item.strip()
            )
            if output:
                details = f"\n{output}"
        raise EvmError(
            f"compiler exited with status {completed.returncode}: {' '.join(command)}{details}"
        )


def artifact_candidates(
    toolchain: Toolchain,
    project: Project,
    request: BuildRequest,
) -> tuple[Path, ...]:
    base = _build_directory(toolchain, project, request)
    executable = project.name + (".exe" if os.name == "nt" else "")
    if toolchain.adapter == "ise":
        code = "F_code" if request.release else "W_code"
        output = base / "EIFGENs" / request.target / code
        driver = "driver" + (".exe" if os.name == "nt" else "")
        return (output / executable, output / driver)
    return (base / executable, base / request.target, base / f"{request.target}.exe")


def _validate_adapter(adapter: str) -> None:
    if adapter not in KNOWN_ADAPTERS:
        raise EvmError(f"unknown compiler adapter {adapter!r}; known adapters: ise, gobo")


def _constraint_for(project: Project, adapter: str) -> str | None:
    for requirement in project.compilers:
        if requirement.adapter == adapter:
            return requirement.constraint
    if project.compilers:
        raise EvmError(f"compiler {adapter!r} is not allowed by compatibility.compilers")
    return None


def _capability_error(project: Project, adapter: str) -> str | None:
    requirements = dict(project.requires)
    if adapter == "gobo" and requirements.get("concurrency") == "scoop":
        return "required capability concurrency=scoop is unsupported"
    return None


def _build_directory(
    toolchain: Toolchain,
    project: Project,
    request: BuildRequest,
) -> Path:
    mode = "release" if request.release else "dev"
    root = project.build_root or project.directory / "build"
    return root / toolchain.adapter / request.target / mode
