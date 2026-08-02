"""Compiler-specific translation of EVM build requests.

Adapters contain only behavior that differs between Eiffel compilers. Project
preparation, toolchain selection, and process execution remain independent
application concerns.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evm.errors import EvmError
from evm.model import BuildRequest, Project
from evm.versioning import NumericVersion


class CompilerToolchain(Protocol):
    """Installed compiler data required to create a build plan."""

    adapter: str
    executable: Path
    version: NumericVersion


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

    def compatibility_error(self, project: Project) -> str | None:
        """Reject capabilities that Gobo cannot currently provide through EVM."""
        requirements = dict(project.requires)
        if requirements.get("concurrency") == "scoop":
            return "required capability concurrency=scoop is unsupported"
        return None


_COMPILER_ADAPTERS: dict[str, CompilerAdapter] = {
    "ise": IseCompilerAdapter(),
    "gobo": GoboCompilerAdapter(),
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
    return root / toolchain.adapter / str(toolchain.version) / request.target / mode
