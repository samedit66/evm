"""Cross-platform discovery of Eiffel and native compiler components."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evm.toolchain_store import list_installations, user_toolchain_root

_DISCOVERY_SCHEMA_VERSION = 1
_VERSION_TIMEOUT_SECONDS = 5
_VERSION_OUTPUT_LIMIT = 16_384
_NUMERIC_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_WINDOWS_SYSTEM = "Windows"
_MACOS_SYSTEM = "Darwin"


@dataclass(frozen=True)
class _ComponentSpecification:
    component_id: str
    display_name: str
    ecosystem: str
    family: str
    kind: str
    executable: str
    version_arguments: tuple[str, ...] | None
    support: str
    systems: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    specification: _ComponentSpecification
    path: Path
    sources: tuple[str, ...]
    active: bool


@dataclass(frozen=True)
class _VersionProbe:
    version: str | None
    diagnostic: str | None
    output: str


@dataclass(frozen=True)
class DiscoveredComponent:
    component_id: str
    display_name: str
    ecosystem: str
    family: str
    kind: str
    path: Path
    resolved_path: Path
    version: str | None
    availability: str
    support: str
    active: bool
    sources: tuple[str, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class EnvironmentVariable:
    name: str
    defined: bool
    valid: bool | None


@dataclass(frozen=True)
class DiscoveryResult:
    system: str
    machine: str
    components: tuple[DiscoveredComponent, ...]
    environment: tuple[EnvironmentVariable, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _DISCOVERY_SCHEMA_VERSION,
            "platform": {"system": self.system.lower(), "machine": self.machine.lower()},
            "components": [_component_data(component) for component in self.components],
            "environment": [
                {"name": variable.name, "defined": variable.defined, "valid": variable.valid}
                for variable in self.environment
            ],
            "diagnostics": [],
        }


_COMPONENTS = (
    _ComponentSpecification(
        "ise-ec",
        "ISE Eiffel Compiler",
        "eiffel",
        "ise",
        "compiler",
        "ec",
        ("-version",),
        "supported",
    ),
    _ComponentSpecification(
        "gobo-gec",
        "Gobo Eiffel Compiler",
        "eiffel",
        "gobo",
        "compiler",
        "gec",
        ("--version",),
        "supported",
    ),
    _ComponentSpecification(
        "gobo-gecc",
        "Gobo Eiffel C Compilation Driver",
        "eiffel",
        "gobo",
        "tool",
        "gecc",
        ("--version",),
        "partial",
    ),
    _ComponentSpecification(
        "gobo-gelint",
        "Gobo Eiffel Linter",
        "eiffel",
        "gobo",
        "tool",
        "gelint",
        ("--version",),
        "partial",
    ),
    _ComponentSpecification(
        "gobo-gedoc",
        "Gobo Eiffel Documentation Generator",
        "eiffel",
        "gobo",
        "tool",
        "gedoc",
        ("--version",),
        "partial",
    ),
    _ComponentSpecification(
        "gobo-getest",
        "Gobo Eiffel Test Runner",
        "eiffel",
        "gobo",
        "tool",
        "getest",
        ("--version",),
        "partial",
    ),
    _ComponentSpecification(
        "iron", "IRON Package Manager", "eiffel", "iron", "tool", "iron", ("--version",), "partial"
    ),
    _ComponentSpecification(
        "gcc", "GCC", "native", "gcc", "compiler", "gcc", ("--version",), "unknown"
    ),
    _ComponentSpecification(
        "cc", "System C Compiler", "native", "cc", "compiler", "cc", ("--version",), "unknown"
    ),
    _ComponentSpecification(
        "clang", "Clang", "native", "clang", "compiler", "clang", ("--version",), "unknown"
    ),
    _ComponentSpecification(
        "clang-cl",
        "Clang for MSVC ABI",
        "native",
        "clang-cl",
        "compiler",
        "clang-cl",
        ("--version",),
        "unknown",
    ),
    _ComponentSpecification(
        "msvc-cl",
        "Microsoft Visual C++",
        "native",
        "msvc",
        "compiler",
        "cl",
        (),
        "unknown",
        (_WINDOWS_SYSTEM,),
    ),
    _ComponentSpecification(
        "zig", "Zig C Compiler", "native", "zig", "compiler", "zig", ("version",), "unknown"
    ),
)

_ENVIRONMENT_VARIABLES = (
    "ISE_EIFFEL",
    "ISE_PLATFORM",
    "ISE_LIBRARY",
    "GOBO",
    "EVM_TOOLCHAIN",
)


def discover_environment(
    environment: Mapping[str, str] | None = None,
    system: str | None = None,
) -> DiscoveryResult:
    current_environment = os.environ if environment is None else environment
    current_system = platform.system() if system is None else system
    candidates = [
        *_evm_candidates(current_environment),
        *_environment_candidates(current_environment, current_system),
        *_path_candidates(current_environment, current_system),
        *_platform_candidates(current_environment, current_system),
    ]
    merged = _merge_candidates(candidates, current_system)
    components = tuple(_inspect_candidate(candidate) for candidate in merged)
    ordered = tuple(sorted(components, key=_component_sort_key))
    return DiscoveryResult(
        current_system,
        platform.machine(),
        ordered,
        _environment_status(current_environment),
    )


def _evm_candidates(environment: Mapping[str, str]) -> list[_Candidate]:
    selected = environment.get("EVM_TOOLCHAIN")
    candidates: list[_Candidate] = []
    for installation in list_installations(user_toolchain_root(environment)):
        component_id = "ise-ec" if installation.provider == "ise" else "gobo-gec"
        source = f"evm-{installation.kind.value}:{installation.selector}"
        candidates.append(
            _Candidate(
                _specification(component_id),
                installation.executable,
                (source,),
                selected in {installation.provider, installation.selector},
            )
        )
    return candidates


def discovery_lines(result: DiscoveryResult) -> list[str]:
    lines = [f"Platform: {result.system} {result.machine}".rstrip()]
    for ecosystem, heading in (("eiffel", "Eiffel components:"), ("native", "C toolchains:")):
        lines.extend(("", heading))
        matching = [item for item in result.components if item.ecosystem == ecosystem]
        if not matching:
            lines.append("  none found")
            continue
        for component in matching:
            version = f" {component.version}" if component.version else ""
            state = "active" if component.active else "shadowed"
            lines.append(f"  {component.display_name}{version}")
            lines.append(f"    path     {component.path}")
            lines.append(f"    source   {', '.join(component.sources)}")
            lines.append(f"    status   {component.availability}, {state}, {component.support}")
            for diagnostic in component.diagnostics:
                lines.append(f"    warning  {diagnostic}")
    lines.extend(("", "Environment:"))
    for variable in result.environment:
        if not variable.defined:
            status = "not defined"
        elif variable.valid is False:
            status = "defined, path not found"
        else:
            status = "defined"
        lines.append(f"  {variable.name}: {status}")
    return lines


def _path_candidates(environment: Mapping[str, str], system: str) -> list[_Candidate]:
    path_value = environment.get("PATH", "")
    path_extensions = _path_extensions(environment, system)
    candidates: list[_Candidate] = []
    active_names: set[str] = set()
    path_separator = ";" if system == _WINDOWS_SYSTEM else ":"
    for raw_directory in path_value.split(path_separator):
        directory = Path(raw_directory) if raw_directory else Path.cwd()
        for specification in _COMPONENTS:
            if specification.systems and system not in specification.systems:
                continue
            for filename in _candidate_filenames(specification.executable, path_extensions):
                candidate_path = directory / filename
                if not _is_executable(candidate_path, system):
                    continue
                name_key = specification.executable.casefold()
                active = name_key not in active_names
                active_names.add(name_key)
                candidates.append(_Candidate(specification, candidate_path, ("path",), active))
                break
    return candidates


def _environment_candidates(
    environment: Mapping[str, str],
    system: str,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    ise_root = environment.get("ISE_EIFFEL")
    ise_platform = environment.get("ISE_PLATFORM")
    if ise_root and ise_platform:
        ise_bin = Path(ise_root) / "studio" / "spec" / ise_platform / "bin"
        candidates.extend(_known_root_candidates(ise_bin, "ise", "environment", system))
    gobo_root = environment.get("GOBO")
    if gobo_root:
        candidates.extend(
            _known_root_candidates(Path(gobo_root) / "bin", "gobo", "environment", system)
        )
    return candidates


def _known_root_candidates(
    directory: Path,
    family: str,
    source: str,
    system: str,
) -> list[_Candidate]:
    extensions = ("", ".exe", ".cmd", ".bat") if system == _WINDOWS_SYSTEM else ("",)
    candidates: list[_Candidate] = []
    for specification in _COMPONENTS:
        if specification.family != family:
            continue
        for suffix in extensions:
            path = directory / f"{specification.executable}{suffix}"
            if _is_executable(path, system):
                candidates.append(_Candidate(specification, path, (source,), False))
                break
    return candidates


def _platform_candidates(
    environment: Mapping[str, str],
    system: str,
) -> list[_Candidate]:
    if system == _MACOS_SYSTEM:
        return _xcrun_candidates()
    if system == _WINDOWS_SYSTEM:
        return _visual_studio_candidates(environment)
    return []


def _xcrun_candidates() -> list[_Candidate]:
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        return []
    try:
        completed = subprocess.run(
            [xcrun, "--find", "clang"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    path = Path(completed.stdout.strip())
    if completed.returncode != 0 or not path.is_file():
        return []
    specification = _specification("clang")
    return [_Candidate(specification, path, ("xcrun",), False)]


def _visual_studio_candidates(environment: Mapping[str, str]) -> list[_Candidate]:
    vswhere = shutil.which("vswhere", path=environment.get("PATH"))
    if vswhere is None:
        program_files = environment.get("ProgramFiles(x86)")
        if program_files:
            bundled = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
            if bundled.is_file():
                vswhere = str(bundled)
    if vswhere is None:
        return []
    try:
        completed = subprocess.run(
            [
                vswhere,
                "-all",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    candidates: list[_Candidate] = []
    for line in completed.stdout.splitlines():
        location = line.strip()
        if not location:
            continue
        installation = Path(location)
        paths = sorted(installation.glob("VC/Tools/MSVC/*/bin/Host*/**/cl.exe"), reverse=True)
        if paths:
            candidates.append(
                _Candidate(_specification("msvc-cl"), paths[0], ("visual-studio",), False)
            )
    return candidates


def _merge_candidates(candidates: list[_Candidate], system: str) -> list[_Candidate]:
    merged: dict[tuple[str, str], _Candidate] = {}
    for candidate in candidates:
        key = (candidate.specification.component_id, _path_key(candidate.path, system))
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        sources = tuple(dict.fromkeys((*existing.sources, *candidate.sources)))
        merged[key] = _Candidate(
            candidate.specification,
            existing.path,
            sources,
            existing.active or candidate.active,
        )
    return list(merged.values())


def _inspect_candidate(candidate: _Candidate) -> DiscoveredComponent:
    if candidate.specification.version_arguments is None:
        probe = _VersionProbe(None, None, "")
        availability = "available"
    else:
        probe = _probe_version(candidate)
        availability = "available" if probe.version is not None else "unverified"
    diagnostics = (probe.diagnostic,) if probe.diagnostic else ()
    component_id, display_name, family = _classified_identity(candidate, probe.output)
    return DiscoveredComponent(
        component_id,
        display_name,
        candidate.specification.ecosystem,
        family,
        candidate.specification.kind,
        candidate.path,
        candidate.path.resolve(strict=False),
        probe.version,
        availability,
        candidate.specification.support,
        candidate.active,
        candidate.sources,
        diagnostics,
    )


def _probe_version(candidate: _Candidate) -> _VersionProbe:
    arguments = candidate.specification.version_arguments
    if arguments is None:
        raise AssertionError("component does not define a version probe")
    command = [str(candidate.path), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _VersionProbe(None, "version detection timed out", "")
    except OSError as error:
        return _VersionProbe(None, f"version detection failed: {error}", "")
    output = (completed.stdout + "\n" + completed.stderr)[:_VERSION_OUTPUT_LIMIT]
    match = _NUMERIC_VERSION_RE.search(output)
    if match is not None:
        return _VersionProbe(match.group(0), None, output)
    if completed.returncode != 0:
        return _VersionProbe(
            None,
            f"version command exited with {completed.returncode}",
            output,
        )
    return _VersionProbe(None, "numeric version was not reported", output)


def _classified_identity(candidate: _Candidate, output: str) -> tuple[str, str, str]:
    specification = candidate.specification
    if specification.component_id not in {"cc", "gcc", "clang"}:
        return specification.component_id, specification.display_name, specification.family
    normalized = output.casefold()
    if "apple clang" in normalized:
        return "apple-clang", "Apple Clang", "apple-clang"
    if "clang" in normalized:
        return "clang", "Clang", "clang"
    if "gcc" in normalized or "free software foundation" in normalized:
        return "gcc", "GCC", "gcc"
    return specification.component_id, specification.display_name, specification.family


def _environment_status(environment: Mapping[str, str]) -> tuple[EnvironmentVariable, ...]:
    statuses: list[EnvironmentVariable] = []
    for name in _ENVIRONMENT_VARIABLES:
        value = environment.get(name)
        if not value:
            statuses.append(EnvironmentVariable(name, False, None))
        elif name in {"ISE_EIFFEL", "ISE_LIBRARY", "GOBO"}:
            statuses.append(EnvironmentVariable(name, True, Path(value).exists()))
        else:
            statuses.append(EnvironmentVariable(name, True, None))
    return tuple(statuses)


def _path_extensions(environment: Mapping[str, str], system: str) -> tuple[str, ...]:
    if system != _WINDOWS_SYSTEM:
        return ("",)
    raw_extensions = environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    extensions = tuple(item.lower() for item in raw_extensions.split(";") if item)
    return ("", *extensions)


def _candidate_filenames(executable: str, extensions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{executable}{extension}" for extension in extensions)


def _is_executable(path: Path, system: str) -> bool:
    if not path.is_file():
        return False
    return system == _WINDOWS_SYSTEM or os.access(path, os.X_OK)


def _path_key(path: Path, system: str) -> str:
    normalized = str(path.resolve(strict=False))
    return normalized.casefold() if system == _WINDOWS_SYSTEM else normalized


def _specification(component_id: str) -> _ComponentSpecification:
    for specification in _COMPONENTS:
        if specification.component_id == component_id:
            return specification
    raise AssertionError(f"unknown component specification {component_id!r}")


def _component_sort_key(component: DiscoveredComponent) -> tuple[object, ...]:
    ecosystem_order = 0 if component.ecosystem == "eiffel" else 1
    return (
        ecosystem_order,
        component.family,
        not component.active,
        component.component_id,
        str(component.path).casefold(),
    )


def _component_data(component: DiscoveredComponent) -> dict[str, object]:
    return {
        "id": component.component_id,
        "name": component.display_name,
        "kind": component.kind,
        "ecosystem": component.ecosystem,
        "family": component.family,
        "path": str(component.path),
        "resolved_path": str(component.resolved_path),
        "version": component.version,
        "availability": component.availability,
        "support": component.support,
        "active": component.active,
        "sources": list(component.sources),
        "diagnostics": list(component.diagnostics),
    }
