"""Compiler-specific translation of EVM build requests.

Adapters contain only behavior that differs between Eiffel compilers. Project
preparation, toolchain selection, and process execution remain independent
application concerns.
"""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evm.errors import EvmError
from evm.lockfile import LOCK_NAME, load_lock
from evm.model import BuildRequest, Project
from evm.versioning import NumericVersion


class CompilerToolchain(Protocol):
    """Installed compiler data required to create a build plan."""

    adapter: str
    executable: Path
    version: NumericVersion
    revision: str | None


class CompilerAdapter(Protocol):
    """Translate compiler-independent requests into native compiler details."""

    name: str

    def compiler_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        check_only: bool = False,
    ) -> list[str]:
        """Return the native command for a build or configuration check."""

    def artifact_candidates(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
    ) -> tuple[Path, ...]:
        """Return possible output paths in preferred lookup order."""

    def run_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        arguments: tuple[str, ...],
    ) -> list[str]:
        """Return the native command that runs a successful build."""

    def legacy_ecf_variables(self, toolchain: CompilerToolchain) -> Mapping[str, Path]:
        """Return variables required while preparing a legacy ECF file."""

    def compatibility_error(self, project: Project) -> str | None:
        """Describe an unsupported project capability, if one exists."""


class IseCompilerAdapter:
    """Map EVM operations to the ISE EiffelStudio command-line compiler."""

    name = "ise"

    def compiler_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        check_only: bool = False,
    ) -> list[str]:
        """Build an ``ec`` command for the requested target and mode."""
        command = [
            str(toolchain.executable),
            "-batch",
            "-config",
            str(project.ecf_path),
            "-target",
            request.target,
            "-project_path",
            str(build_directory(toolchain, project, request)),
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
        command.extend(project.compiler_arguments.get(self.name, ()))
        return command

    def artifact_candidates(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
    ) -> tuple[Path, ...]:
        """Return ISE workbench or finalized executable locations."""
        base = build_directory(toolchain, project, request)
        code_directory = "F_code" if request.release else "W_code"
        output = base / "EIFGENs" / request.target / code_directory
        executable = project.name + (".exe" if os.name == "nt" else "")
        driver = "driver" + (".exe" if os.name == "nt" else "")
        return (output / executable, output / driver)

    def legacy_ecf_variables(self, toolchain: CompilerToolchain) -> Mapping[str, Path]:
        """Resolve the ISE library root used by legacy ECF references."""
        configured = os.environ.get("ISE_LIBRARY")
        root = Path(configured) if configured else toolchain.executable.resolve().parent.parent
        return {"ISE_LIBRARY": root}

    def run_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        arguments: tuple[str, ...],
    ) -> list[str]:
        return _native_run_command(self.artifact_candidates(toolchain, project, request), arguments)

    def compatibility_error(self, project: Project) -> str | None:
        """Accept capabilities currently represented by EVM for ISE."""
        return None


class GoboCompilerAdapter:
    """Map EVM operations to the Gobo Eiffel Compiler."""

    name = "gobo"

    def compiler_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        check_only: bool = False,
    ) -> list[str]:
        """Build a ``gec`` command for the requested target and capabilities."""
        command = [
            str(toolchain.executable),
            str(project.ecf_path),
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
        command.extend(project.compiler_arguments.get(self.name, ()))
        return command

    def artifact_candidates(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
    ) -> tuple[Path, ...]:
        """Return conventional Gobo executable locations."""
        base = build_directory(toolchain, project, request)
        executable = project.name + (".exe" if os.name == "nt" else "")
        return (base / executable, base / request.target, base / f"{request.target}.exe")

    def legacy_ecf_variables(self, toolchain: CompilerToolchain) -> Mapping[str, Path]:
        """Return no extra variables because Gobo resolves its own ECF environment."""
        return {}

    def run_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        arguments: tuple[str, ...],
    ) -> list[str]:
        return _native_run_command(self.artifact_candidates(toolchain, project, request), arguments)

    def compatibility_error(self, project: Project) -> str | None:
        """Reject capabilities that Gobo cannot currently provide through EVM."""
        requirements = dict(project.requires)
        if requirements.get("concurrency") == "scoop":
            return "required capability concurrency=scoop is unsupported"
        return None


class SerpentCompilerAdapter:
    """Compile the Serpent Eiffel subset to JVM class files."""

    name = "serpent"

    def compiler_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        check_only: bool = False,
    ) -> list[str]:
        if check_only:
            raise EvmError("Serpent does not support configuration-only checks")
        root = _effective_root(project, request.target)
        if project.kind != "application" or root is None:
            raise EvmError("Serpent currently supports application projects only")
        worker = Path(__file__).with_name("serpent_worker.py")
        payload = {
            "operation": "build",
            "sources": [
                str(path) for path in _effective_source_directories(project, request.target)
            ],
            "output": str(build_directory(toolchain, project, request)),
            "main_class": root.class_name,
            "main_routine": root.feature,
            "java_version": 11,
        }
        return [str(toolchain.executable), str(worker), json.dumps(payload)]

    def artifact_candidates(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
    ) -> tuple[Path, ...]:
        return (build_directory(toolchain, project, request),)

    def run_command(
        self,
        toolchain: CompilerToolchain,
        project: Project,
        request: BuildRequest,
        arguments: tuple[str, ...],
    ) -> list[str]:
        root = _effective_root(project, request.target)
        if root is None:
            raise EvmError("Serpent run requires an application root")
        worker = Path(__file__).with_name("serpent_worker.py")
        payload = {
            "operation": "run",
            "classpath": str(build_directory(toolchain, project, request)),
            "main_class": root.class_name,
            "arguments": list(arguments),
        }
        return [str(toolchain.executable), str(worker), json.dumps(payload)]

    def legacy_ecf_variables(self, toolchain: CompilerToolchain) -> Mapping[str, Path]:
        return {}

    def compatibility_error(self, project: Project) -> str | None:
        requirements = dict(project.requires)
        if project.kind != "application":
            return "Serpent currently supports application projects only"
        if requirements.get("standard") == "ise" or "ise-semantics" in requirements:
            return "Serpent does not support ISE language semantics"
        if requirements.get("concurrency") not in {None, "none"}:
            return "Serpent does not support the requested concurrency capability"
        if requirements.get("void-safety") not in {None, "none"}:
            return "Serpent does not support the requested void-safety capability"
        return None


_COMPILER_ADAPTERS: dict[str, CompilerAdapter] = {
    "ise": IseCompilerAdapter(),
    "gobo": GoboCompilerAdapter(),
    "serpent": SerpentCompilerAdapter(),
}


@dataclass(frozen=True)
class CompilerAdapterMetadata:
    """Stable identity and discovery details for a compiler adapter."""

    name: str
    display_name: str
    executable: str
    version_arguments: tuple[str, ...]
    automatic: bool = False


_ADAPTER_METADATA = {
    "ise": CompilerAdapterMetadata("ise", "ISE EiffelStudio", "ec", ("-version",), True),
    "gobo": CompilerAdapterMetadata("gobo", "Gobo Eiffel", "gec", ("--version",), True),
    "serpent": CompilerAdapterMetadata("serpent", "Serpent Eiffel", "serpent", ()),
}


def compiler_adapter(name: str) -> CompilerAdapter:
    """Return the registered adapter for a stable toolchain code name."""
    try:
        return _COMPILER_ADAPTERS[name]
    except KeyError as error:
        known = ", ".join(_COMPILER_ADAPTERS)
        raise EvmError(f"unknown compiler adapter {name!r}; known adapters: {known}") from error


def compiler_adapter_names() -> tuple[str, ...]:
    """Return compiler code names in automatic-selection priority order."""
    return tuple(_COMPILER_ADAPTERS)


def automatic_compiler_adapter_names() -> tuple[str, ...]:
    """Return adapters eligible for implicit toolchain selection."""
    return tuple(name for name, metadata in _ADAPTER_METADATA.items() if metadata.automatic)


def compiler_adapter_metadata(name: str) -> CompilerAdapterMetadata:
    """Return discovery metadata for a registered compiler adapter."""
    try:
        return _ADAPTER_METADATA[name]
    except KeyError as error:
        known = ", ".join(_ADAPTER_METADATA)
        raise EvmError(f"unknown compiler adapter {name!r}; known adapters: {known}") from error


def build_directory(
    toolchain: CompilerToolchain,
    project: Project,
    request: BuildRequest,
) -> Path:
    """Return the isolated output directory for a compiler, target, and mode."""
    mode = "release" if request.release else "dev"
    root = project.build_root or project.directory / "build"
    identity = toolchain.revision or str(toolchain.version)
    return root / toolchain.adapter / identity / request.target / mode


def _native_run_command(candidates: tuple[Path, ...], arguments: tuple[str, ...]) -> list[str]:
    executable = next((candidate for candidate in candidates if candidate.is_file()), None)
    if executable is None:
        checked = "\n".join(f"  - {candidate}" for candidate in candidates)
        raise EvmError(f"build succeeded but executable was not found; checked:\n{checked}")
    return [str(executable), *arguments]


def _effective_root(project: Project, target_name: str):
    root = None
    for target in _target_chain(project, target_name):
        if target.root is not None:
            root = target.root
    return root


def _effective_source_directories(project: Project, target_name: str) -> tuple[Path, ...]:
    sources: list[Path] = []
    for target in _target_chain(project, target_name):
        for source in target.sources:
            path = (project.directory / os.path.expandvars(source)).resolve()
            if path not in sources:
                sources.append(path)
    lock = load_lock(project.directory / LOCK_NAME)
    for package in lock.packages:
        if package.path is not None:
            path = (project.configuration_directory / package.path).resolve()
        else:
            path = project.state_directory / "deps" / package.materialized_name
        if path not in sources:
            sources.append(path)
    return tuple(sources)


def _target_chain(project: Project, target_name: str):
    target = project.target(target_name)
    if target.extends is None:
        return (target,)
    return (*_target_chain(project, target.extends), target)
