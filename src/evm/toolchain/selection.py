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
from evm.toolchain.compilers import (
    automatic_compiler_adapter_names,
    build_directory,
    compiler_adapter,
    compiler_adapter_metadata,
    compiler_adapter_names,
)
from evm.toolchain.store import list_installations, toolchain_environment
from evm.toolchain.types import (
    ToolchainInstallation,
    ToolchainSelector,
    current_toolchain_platform,
)
from evm.versioning import NumericVersion, satisfies

KNOWN_ADAPTERS = compiler_adapter_names()
_VERSION_TIMEOUT_SECONDS = 15
_NUMERIC_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


@dataclass(frozen=True)
class Toolchain:
    """A compiler selected for one project operation.

    This value represents a usable compiler executable, not a release catalog
    or the lifecycle of an installed distribution.
    """

    adapter: str
    executable: Path
    version: NumericVersion
    selection: str
    reason: str

    @property
    def display_name(self) -> str:
        label = compiler_adapter_metadata(self.adapter).display_name
        return f"{label} {self.version}"


@dataclass(frozen=True)
class Detection:
    """Result of probing a compiler executable without selecting it."""

    executable: Path | None
    version: NumericVersion | None
    error: str | None


@dataclass(frozen=True)
class _SelectionPolicy:
    """Ordered compiler requirements and the reason for their priority."""

    candidates: tuple[CompilerRequirement, ...]
    mode: str
    reason: str


def detect_all() -> dict[str, Detection]:
    managed = list_installations()
    return {
        adapter: _managed_detection(adapter, managed) or detect(adapter)
        for adapter in KNOWN_ADAPTERS
    }


def detect(adapter: str) -> Detection:
    _validate_adapter(adapter)
    metadata = compiler_adapter_metadata(adapter)
    executable_name = metadata.executable
    found = shutil.which(executable_name)
    if found is None:
        return Detection(None, None, f"{executable_name} was not found in PATH")
    command = [found, *metadata.version_arguments]
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
    selector, selector_source = _requested_selector(project, explicit)
    policy = _selection_policy(project, selector, selector_source)
    detections = detect_all()
    if selector is not None:
        selected_detection = _detection_for_selector(selector)
        if selected_detection is not None:
            detections[selector.provider] = selected_detection
        elif selector.version not in {None, "latest"}:
            raise EvmError(
                f"toolchain {selector} is not installed; run `evm toolchain install {selector}`"
            )
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


def _selection_policy(
    project: Project,
    selector: ToolchainSelector | None,
    selector_source: str | None,
) -> _SelectionPolicy:
    if selector is not None:
        constraint = _constraint_for(project, selector.provider)
        candidates = (CompilerRequirement(selector.provider, constraint),)
        mode = "environment" if selector_source == "EVM_TOOLCHAIN" else "explicit"
        return _SelectionPolicy(candidates, mode, selector_source or "--toolchain")
    if project.compilers:
        return _SelectionPolicy(
            project.compilers,
            "automatic",
            "first compatible adapter in compatibility.compilers",
        )
    candidates = tuple(
        CompilerRequirement(name, None) for name in automatic_compiler_adapter_names()
    )
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
    directory = build_directory(toolchain, project, request)
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
    return compiler_adapter(toolchain.adapter).compiler_command(
        toolchain,
        project,
        request,
        check_only,
    )


def run_compiler(
    command: list[str],
    working_directory: Path,
    *,
    capture_output: bool = False,
) -> None:
    environment = os.environ.copy()
    environment.update(dict(toolchain_environment_values(command)))
    adapter = "gobo" if Path(command[0]).stem.casefold() == "gec" else "ise"
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


def toolchain_environment_values(command: list[str]) -> tuple[tuple[str, str], ...]:
    executable = Path(command[0]).resolve()
    installation = _installation_for_executable(executable)
    if installation is None:
        return ()
    inherited_environment = {"PATH": os.environ.get("PATH", "")}
    if gobo_cc := os.environ.get("GOBO_CC"):
        inherited_environment["GOBO_CC"] = gobo_cc
    return tuple(toolchain_environment(installation, inherited_environment).items())


def artifact_candidates(
    toolchain: Toolchain,
    project: Project,
    request: BuildRequest,
) -> tuple[Path, ...]:
    return compiler_adapter(toolchain.adapter).artifact_candidates(toolchain, project, request)


def _validate_adapter(adapter: str) -> None:
    if adapter not in KNOWN_ADAPTERS:
        known = ", ".join(KNOWN_ADAPTERS)
        raise EvmError(f"unknown compiler adapter {adapter!r}; known adapters: {known}")


def _requested_selector(
    project: Project,
    explicit: str | None,
) -> tuple[ToolchainSelector | None, str | None]:
    if explicit is not None:
        return ToolchainSelector.parse(explicit), "--toolchain"
    environment = os.environ.get("EVM_TOOLCHAIN")
    if environment:
        return ToolchainSelector.parse(environment), "EVM_TOOLCHAIN"
    if project.toolchain is not None:
        return ToolchainSelector.parse(project.toolchain.default), "toolchain.default"
    return None, None


def _managed_detection(
    adapter: str,
    installations: tuple[ToolchainInstallation, ...],
) -> Detection | None:
    platform_identifier = current_toolchain_platform().identifier
    installation = next(
        (
            item
            for item in installations
            if item.provider == adapter
            and item.platform.identifier == platform_identifier
            and item.executable.is_file()
        ),
        None,
    )
    if installation is None:
        return None
    return Detection(
        installation.executable,
        NumericVersion.parse(installation.revision),
        None,
    )


def _installation_for_executable(executable: Path) -> ToolchainInstallation | None:
    resolved = executable.resolve()
    return next(
        (
            item
            for item in list_installations()
            if item.executable.resolve() == resolved
            or item.executable.resolve().parent == resolved.parent
        ),
        None,
    )


def _detection_for_selector(selector: ToolchainSelector) -> Detection | None:
    platform_identifier = current_toolchain_platform().identifier
    installation = next(
        (
            item
            for item in list_installations()
            if item.matches(selector) and item.platform.identifier == platform_identifier
        ),
        None,
    )
    if installation is None:
        return None
    return Detection(
        installation.executable,
        NumericVersion.parse(installation.revision),
        None,
    )


def _constraint_for(project: Project, adapter: str) -> str | None:
    for requirement in project.compilers:
        if requirement.adapter == adapter:
            return requirement.constraint
    if project.compilers:
        raise EvmError(f"compiler {adapter!r} is not allowed by compatibility.compilers")
    return None


def _capability_error(project: Project, adapter: str) -> str | None:
    return compiler_adapter(adapter).compatibility_error(project)
