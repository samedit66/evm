"""Direct execution of explicit Eiffel source files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from filelock import FileLock

from evm.dependencies import install_dependencies
from evm.ecf import generate_ecf
from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.lockfile import LOCK_NAME, LockFile, load_lock
from evm.manifest import MANIFEST_NAME, load_manifest
from evm.model import BuildRequest, Project, Root, Target
from evm.project import print_toolchain, validate_configuration
from evm.toolchains import (
    Toolchain,
    artifact_candidates,
    compiler_command,
    prepare_build_directory,
    run_compiler,
    select_toolchain,
)
from evm.workspace import load_project_context

_SCRIPT_CACHE_FORMAT = 1
_SCRIPT_CACHE_KEY_LENGTH = 32
_SCRIPT_FINGERPRINT_FILE = ".fingerprint"
_WINDOWS_MAX_PATH = 260
_GOBO_GENERATED_FILE_SAMPLE = "_9999.c"
_CLASS_DECLARATION_RE = re.compile(
    r"(?im)^\s*(?:(?:deferred|expanded|external|frozen|once)\s+)*class\s+"
    r"([A-Za-z][A-Za-z0-9_]*)\b"
)
_CREATE_CLAUSE_RE = re.compile(
    r"(?is)\bcreate(?:\s*\{[^}]*\})?\s*(.*?)"
    r"(?=\b(?:convert|create|feature|inherit|invariant|note|obsolete|end)\b)"
)
_EIFFEL_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@dataclass(frozen=True)
class ScriptRunRequest:
    sources: tuple[Path, ...]
    arguments: tuple[str, ...] = ()
    compiler: str | None = None
    release: bool = False
    class_name: str | None = None
    feature: str | None = None
    manifest_path: Path | None = None
    standalone: bool = False
    offline: bool = False


@dataclass(frozen=True)
class PreparedScript:
    project: Project
    source_project: Project | None
    original_sources: tuple[Path, ...]
    root: Root
    cache_directory: Path


def run_script(request: ScriptRunRequest) -> int:
    prepared = prepare_script(request)
    build_request = BuildRequest(
        compiler=request.compiler,
        release=request.release,
        offline=request.offline,
    )
    toolchain, executable = _compile_script(prepared, build_request)
    print(f"Mode: file\nRoot: {prepared.root.class_name}.{prepared.root.feature}", flush=True)
    print_toolchain(toolchain, request=build_request)
    return subprocess.run(
        [str(executable), *request.arguments],
        cwd=Path.cwd(),
        check=False,
    ).returncode


def prepare_script(request: ScriptRunRequest) -> PreparedScript:
    sources = _validated_sources(request.sources)
    source_project = _source_project(request, sources)
    root = _script_root(sources[0], request.class_name, request.feature)
    cache_directory = _cache_directory(request, sources, source_project, root)
    staged_sources = _stage_sources(sources, cache_directory)
    project = _script_project(source_project, staged_sources, cache_directory, root)
    return PreparedScript(project, source_project, sources, root, cache_directory)


def _validated_sources(raw_sources: tuple[Path, ...]) -> tuple[Path, ...]:
    if not raw_sources:
        raise EvmError("file mode requires at least one Eiffel source file")
    sources = tuple(path.resolve() for path in raw_sources)
    if len(sources) != len(set(sources)):
        raise EvmError("the same Eiffel source file was passed more than once")
    for source in sources:
        if source.suffix.lower() != ".e":
            raise EvmError(f"not an Eiffel source file: {source}")
        if not source.is_file():
            raise EvmError(f"Eiffel source file not found: {source}")
    return sources


def _source_project(request: ScriptRunRequest, sources: tuple[Path, ...]) -> Project | None:
    if request.standalone and request.manifest_path is not None:
        raise EvmError("--standalone and --manifest cannot be used together")
    if request.standalone:
        return None
    if request.manifest_path is not None:
        return load_manifest(request.manifest_path.resolve())
    projects = tuple(_project_containing(source) for source in sources)
    configured = {project.manifest_path for project in projects if project is not None}
    if not configured:
        return None
    if len(configured) != 1 or any(project is None for project in projects):
        raise EvmError("Eiffel files from different project contexts cannot be run together")
    project = next(project for project in projects if project is not None)
    return project


def _project_containing(source: Path) -> Project | None:
    manifest_path = _nearest_manifest(source.parent)
    if manifest_path is None:
        return None
    context = load_project_context(source.parent)
    if context.project is None:
        raise EvmError(f"{source} is not inside an EVM workspace package")
    return context.project


def _nearest_manifest(directory: Path) -> Path | None:
    for candidate_directory in (directory, *directory.parents):
        candidate = candidate_directory / MANIFEST_NAME
        if candidate.is_file():
            return candidate.resolve()
    return None


def _script_root(source: Path, class_override: str | None, feature_override: str | None) -> Root:
    text = _read_eiffel_source(source)
    declared_classes = _CLASS_DECLARATION_RE.findall(text)
    if not declared_classes:
        raise EvmError(f"no Eiffel class declaration found in root file: {source}")
    if len(declared_classes) != 1 and class_override is None:
        raise EvmError(
            f"multiple Eiffel classes found in root file; select one with --class: {source}"
        )
    class_name = class_override or declared_classes[0]
    if class_name.upper() not in {name.upper() for name in declared_classes}:
        raise EvmError(f"class {class_name!r} was not found in root file: {source}")
    feature = feature_override or _first_creation_procedure(text) or "default_create"
    return Root(class_name.upper(), feature.lower())


def _read_eiffel_source(source: Path) -> str:
    try:
        return source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise EvmError(f"cannot read Eiffel source {source}: {error}") from error


def _first_creation_procedure(text: str) -> str | None:
    match = _CREATE_CLAUSE_RE.search(text)
    if match is None:
        return None
    clause = re.sub(r"--[^\n]*|\{[^}]*\}", " ", match.group(1))
    identifier = _EIFFEL_IDENTIFIER_RE.search(clause)
    return identifier.group(0) if identifier is not None else None


def _cache_directory(
    request: ScriptRunRequest,
    sources: tuple[Path, ...],
    source_project: Project | None,
    root: Root,
) -> Path:
    fingerprint = _script_fingerprint(request, sources, source_project, root)
    root_directory = (
        source_project.state_directory / "scripts"
        if source_project is not None
        else _standalone_cache_root() / "scripts"
    )
    cache_directory = root_directory / fingerprint[:_SCRIPT_CACHE_KEY_LENGTH]
    _verify_cache_fingerprint(cache_directory, fingerprint)
    return cache_directory


def _verify_cache_fingerprint(cache_directory: Path, fingerprint: str) -> None:
    fingerprint_path = cache_directory / _SCRIPT_FINGERPRINT_FILE
    if fingerprint_path.is_file():
        stored = fingerprint_path.read_text(encoding="ascii").strip()
        if stored != fingerprint:
            raise EvmError(f"script cache fingerprint collision: {cache_directory}")
        return
    atomic_write(fingerprint_path, f"{fingerprint}\n".encode("ascii"))


def _script_fingerprint(
    request: ScriptRunRequest,
    sources: tuple[Path, ...],
    source_project: Project | None,
    root: Root,
) -> str:
    digest = hashlib.sha256()
    settings = {
        "format": _SCRIPT_CACHE_FORMAT,
        "root": [root.class_name, root.feature],
        "sources": [str(source) for source in sources],
        "release": request.release,
    }
    digest.update(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode())
    for source in sources:
        digest.update(_read_source_bytes(source))
    if source_project is not None:
        digest.update(source_project.manifest_path.read_bytes())
        lock_path = source_project.directory / LOCK_NAME
        if lock_path.is_file():
            digest.update(lock_path.read_bytes())
    return digest.hexdigest()


def _standalone_cache_root() -> Path:
    configured = os.environ.get("EVM_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "evm"
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "evm"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg_cache) if xdg_cache else Path.home() / ".cache") / "evm"


def _stage_sources(sources: tuple[Path, ...], cache_directory: Path) -> tuple[Path, ...]:
    staged: list[Path] = []
    for index, source in enumerate(sources):
        destination = cache_directory / "sources" / f"{index:04d}" / source.name
        content = _read_source_bytes(source)
        if content.startswith(b"#!"):
            newline = content.find(b"\n")
            content = b"\n" if newline < 0 else b"\n" + content[newline + 1 :]
        atomic_write(destination, content)
        staged.append(destination)
    return tuple(staged)


def _script_project(
    source_project: Project | None,
    staged_sources: tuple[Path, ...],
    cache_directory: Path,
    root: Root,
) -> Project:
    configuration_directory = (
        source_project.directory if source_project is not None else Path.cwd().resolve()
    )
    source_clusters = tuple(
        source.parent.relative_to(cache_directory).as_posix() for source in staged_sources
    )
    fingerprint = cache_directory.name
    conditions = ()
    if source_project is not None:
        conditions = tuple(
            replace(
                condition,
                sources=(),
                external_objects=tuple(
                    _cached_external_path(
                        external,
                        source_project.directory,
                        cache_directory,
                    )
                    for external in condition.external_objects
                ),
            )
            for condition in source_project.conditions
        )
    return Project(
        manifest_path=cache_directory / MANIFEST_NAME,
        name=f"script_{fingerprint[:16]}",
        version="0.0.0",
        kind="application",
        uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"evm-script:{fingerprint}")),
        ecf_path=cache_directory / "script.ecf",
        ecf_managed=True,
        targets=(Target("default", root, source_clusters),),
        conditions=conditions,
        compilers=source_project.compilers if source_project is not None else (),
        toolchain=source_project.toolchain if source_project is not None else None,
        requires=source_project.requires if source_project is not None else (),
        compiler_arguments=(
            source_project.compiler_arguments if source_project is not None else {}
        ),
        dependencies=source_project.dependencies if source_project is not None else (),
        workspace_root=source_project.workspace_root if source_project is not None else None,
        build_root=cache_directory / "build",
        configuration_root=configuration_directory,
        state_root=source_project.state_directory if source_project is not None else None,
    )


def _read_source_bytes(source: Path) -> bytes:
    try:
        return source.read_bytes()
    except OSError as error:
        raise EvmError(f"cannot read Eiffel source {source}: {error}") from error


def _cached_external_path(external: str, project_directory: Path, cache_directory: Path) -> str:
    path = Path(external)
    if external.startswith("-l") or path.is_absolute() or "$" in external:
        return external
    absolute_path = (project_directory / path).resolve()
    return Path(os.path.relpath(absolute_path, cache_directory)).as_posix()


def _compile_script(
    prepared: PreparedScript,
    request: BuildRequest,
) -> tuple[Toolchain, Path]:
    lock = _prepare_script_dependencies(prepared, request)
    diagnostics = validate_configuration(prepared.project)
    if diagnostics:
        details = "\n".join(f"  - {item}" for item in diagnostics)
        raise EvmError(f"configuration errors:\n{details}")
    atomic_write(prepared.project.ecf_path, generate_ecf(prepared.project, lock))
    toolchain = select_toolchain(prepared.project, request.compiler)
    lock_path = prepared.cache_directory / f"compile-{toolchain.adapter}.lock"
    with FileLock(lock_path):
        build_directory = prepare_build_directory(toolchain, prepared.project, request)
        _validate_gobo_build_path(toolchain, build_directory, prepared.project.name)
        command = compiler_command(toolchain, prepared.project, request)
        run_compiler(command, build_directory)
    executable = next(
        (
            candidate
            for candidate in artifact_candidates(toolchain, prepared.project, request)
            if candidate.is_file()
        ),
        None,
    )
    if executable is None:
        checked = "\n".join(
            f"  - {candidate}"
            for candidate in artifact_candidates(toolchain, prepared.project, request)
        )
        raise EvmError(f"build succeeded but executable was not found; checked:\n{checked}")
    return toolchain, executable


def _validate_gobo_build_path(
    toolchain: Toolchain,
    build_directory: Path,
    project_name: str,
) -> None:
    if sys.platform != "win32" or toolchain.adapter != "gobo":
        return
    generated_file = build_directory / ".gobo" / f"{project_name}{_GOBO_GENERATED_FILE_SAMPLE}"
    if len(str(generated_file)) < _WINDOWS_MAX_PATH:
        return
    raise EvmError(
        "Gobo build path is too long for Windows; set EVM_CACHE_DIR to a shorter path "
        "for standalone files or move the project closer to the drive root: "
        f"{generated_file}"
    )


def _prepare_script_dependencies(
    prepared: PreparedScript,
    request: BuildRequest,
) -> LockFile | None:
    if prepared.source_project is None:
        return None
    lock = load_lock(prepared.source_project.directory / LOCK_NAME)
    install_dependencies(prepared.project, offline=request.offline, lock=lock)
    return lock
