"""Project creation, validation, import, and execution orchestration."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import etree

from evm.dependencies import install_dependencies
from evm.ecf import ensure_managed_ecf, generate_ecf, parse_ecf, validate_ecf
from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.lockfile import LOCK_NAME, empty_lock, load_lock, serialize_lock
from evm.manifest import load_manifest, parse_manifest
from evm.model import BuildRequest, Project, Root, Target
from evm.toolchains import (
    Toolchain,
    artifact_candidates,
    compiler_command,
    prepare_build_directory,
    run_compiler,
    select_toolchain,
)


@dataclass(frozen=True)
class _ImportedProject:
    name: str
    uuid: str
    library: bool
    root: Root | None
    clusters: tuple[str, ...]
    ecf: str


@dataclass(frozen=True)
class _ProjectFiles:
    manifest: Path
    lock: Path
    ecf: Path
    gitignore: Path
    source: Path


def create_project(directory: Path, *, library: bool = False, initialize: bool = False) -> Project:
    directory = directory.resolve()
    if directory.exists() and not directory.is_dir():
        raise EvmError(f"project path is not a directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    name = _normalized_name(directory.name)
    files = _project_files(directory, name, library)
    _ensure_scaffold_available(files, initialize)
    project_uuid = str(uuid.uuid4())
    manifest = _new_manifest(name, project_uuid, library)
    source = _library_source(name) if library else _application_source()
    project = parse_manifest(manifest, directory / "Eiffel.toml")
    lock = empty_lock(project)
    ecf = generate_ecf(project, lock)
    _write_project_files(files, manifest, serialize_lock(lock), ecf, source)
    return load_manifest(files.manifest)


def _project_files(directory: Path, name: str, library: bool) -> _ProjectFiles:
    source_name = f"{_eiffel_class_name(name).lower()}.e" if library else "application.e"
    return _ProjectFiles(
        manifest=directory / "Eiffel.toml",
        lock=directory / "Eiffel.lock",
        ecf=directory / f"{name}.ecf",
        gitignore=directory / ".gitignore",
        source=directory / "src" / source_name,
    )


def _ensure_scaffold_available(files: _ProjectFiles, initialize: bool) -> None:
    protected = (files.manifest, files.lock, files.ecf, files.source)
    if not initialize:
        protected = (*protected, files.gitignore)
    occupied = [str(path) for path in protected if path.exists()]
    if not occupied:
        return
    action = "initialize" if initialize else "create"
    raise EvmError(
        f"cannot {action} project without overwriting existing files: {', '.join(occupied)}"
    )


def _write_project_files(
    files: _ProjectFiles,
    manifest: str,
    lock: bytes,
    ecf: bytes,
    source: str,
) -> None:
    files.source.parent.mkdir(exist_ok=True)
    (files.manifest.parent / "tests").mkdir(exist_ok=True)
    atomic_write(files.manifest, manifest.encode())
    atomic_write(files.lock, lock)
    atomic_write(files.ecf, ecf)
    atomic_write(files.source, source.encode())
    if not files.gitignore.exists():
        atomic_write(files.gitignore, b".evm/\nbuild/\n")


def validate_configuration(project: Project) -> list[str]:
    diagnostics: list[str] = []
    for target in project.targets:
        diagnostics.extend(
            _source_diagnostics(
                project,
                f"target {target.name}",
                effective_sources(project, target.name),
            )
        )
    for index, condition in enumerate(project.conditions):
        diagnostics.extend(_source_diagnostics(project, f"condition {index}", condition.sources))
    if project.kind == "application":
        for target in project.targets:
            if effective_root(project, target.name) is None:
                diagnostics.append(f"target {target.name}: application target has no root")
    if project.ecf_path.exists() and not project.ecf_managed:
        validate_ecf(project.ecf_path)
    return diagnostics


def _source_diagnostics(
    project: Project,
    owner: str,
    sources: tuple[str, ...],
) -> list[str]:
    diagnostics: list[str] = []
    seen: dict[Path, str] = {}
    for source in sources:
        unresolved = _unresolved_variables(source)
        if unresolved:
            diagnostics.append(
                f"{owner}: unresolved environment variable in source "
                f"{source!r}: {', '.join(unresolved)}"
            )
            continue
        resolved = (project.directory / os.path.expandvars(source)).resolve()
        if not resolved.is_dir():
            diagnostics.append(f"{owner}: source directory does not exist: {source}")
        if resolved in seen:
            diagnostics.append(
                f"{owner}: duplicate source {source!r} (already used as {seen[resolved]!r})"
            )
        else:
            seen[resolved] = source
    return diagnostics


def _release_dependency_diagnostics(project: Project) -> list[str]:
    diagnostics: list[str] = []
    for dependency in project.dependencies:
        if dependency.source == "path":
            diagnostics.append(
                f"dependency {dependency.name}: external path dependency is not allowed "
                "in release mode"
            )
        if dependency.patched_path is not None:
            diagnostics.append(
                f"dependency {dependency.name}: local source patch is not allowed in release mode"
            )
    return diagnostics


def prepare_project(
    project: Project,
    *,
    regenerate: bool = False,
    release: bool = False,
    offline: bool = False,
) -> bool:
    diagnostics = validate_configuration(project)
    if release:
        diagnostics.extend(_release_dependency_diagnostics(project))
    if diagnostics:
        raise EvmError("configuration errors:\n" + "\n".join(f"  - {item}" for item in diagnostics))
    lock = load_lock(project.directory / LOCK_NAME)
    install_dependencies(project, offline=offline, lock=lock)
    return ensure_managed_ecf(project, regenerate=regenerate, lock=lock)


def compile_project(
    project: Project,
    request: BuildRequest,
    check_only: bool = False,
) -> Toolchain:
    _require_target(project, request.target)
    prepare_project(
        project,
        regenerate=request.regenerate_ecf,
        release=request.release,
        offline=request.offline,
    )
    toolchain = select_toolchain(project, request.compiler)
    build_directory = prepare_build_directory(toolchain, project, request, check_only)
    command = compiler_command(toolchain, project, request, check_only)
    print_toolchain(toolchain, request=request)
    run_compiler(command, build_directory)
    return toolchain


def run_project(
    project: Project,
    request: BuildRequest,
    arguments: tuple[str, ...],
) -> int:
    if project.kind != "application":
        raise EvmError("run requires a project of type 'application'")
    toolchain = compile_project(
        project,
        request,
    )
    for candidate in artifact_candidates(toolchain, project, request):
        if candidate.is_file():
            return subprocess.run([str(candidate), *arguments], cwd=project.directory).returncode
    candidates = "\n".join(
        f"  - {item}" for item in artifact_candidates(toolchain, project, request)
    )
    raise EvmError(f"build succeeded but executable was not found; checked:\n{candidates}")


def effective_sources(project: Project, target_name: str) -> tuple[str, ...]:
    chain = target_chain(project, target_name)
    result: list[str] = []
    for target in chain:
        for source in target.sources:
            if source not in result:
                result.append(source)
    return tuple(result)


def effective_root(project: Project, target_name: str) -> Root | None:
    result: Root | None = None
    for target in target_chain(project, target_name):
        if target.root is not None:
            result = target.root
    return result


def target_chain(project: Project, target_name: str) -> tuple[Target, ...]:
    _require_target(project, target_name)
    target = project.target(target_name)
    result: list[Target] = []
    if target.extends is not None:
        result.extend(target_chain(project, target.extends))
    result.append(target)
    return tuple(result)


def effective_conditions(
    project: Project, *, target: str, compiler: str | None, release: bool
) -> list[dict[str, Any]]:
    context = {
        "os": _normalized_os(),
        "architecture": platform.machine().lower(),
        "compiler": compiler,
        "mode": "release" if release else "dev",
    }
    result: list[dict[str, Any]] = []
    for condition in project.conditions:
        if condition.target != target:
            continue
        matches = all(context.get(key) == value for key, value in condition.when)
        result.append(
            {
                "when": dict(condition.when),
                "matched": matches,
                "sources": list(condition.sources),
                "external_objects": list(condition.external_objects),
            }
        )
    return result


def explain_data(
    project: Project,
    *,
    target: str,
    release: bool,
    compiler: str | None,
) -> dict[str, Any]:
    _require_target(project, target)
    selected: Toolchain | None
    try:
        selected = select_toolchain(project, compiler)
    except EvmError:
        selected = None
    root = effective_root(project, target)
    conditions = effective_conditions(
        project,
        target=target,
        compiler=selected.adapter if selected else compiler,
        release=release,
    )
    conditional_sources = [
        source
        for condition in conditions
        if condition["matched"]
        for source in condition["sources"]
    ]
    return {
        "project": {
            "name": project.name,
            "version": project.version,
            "type": project.kind,
            "uuid": project.uuid,
        },
        "target": target,
        "inheritance": [item.name for item in target_chain(project, target)],
        "mode": "release" if release else "dev",
        "platform": f"{_normalized_os()}-{platform.machine().lower()}",
        "root": None if root is None else f"{root.class_name}.{root.feature}",
        "sources": [*effective_sources(project, target), *conditional_sources],
        "requires": dict(project.requires),
        "conditions": conditions,
        "toolchain": None
        if selected is None
        else {
            "adapter": selected.adapter,
            "version": str(selected.version),
            "executable": str(selected.executable),
            "selection": selected.selection,
            "reason": selected.reason,
        },
        "compiler_arguments": project.compiler_arguments,
    }


def print_toolchain(toolchain: Toolchain, *, request: BuildRequest) -> None:
    lines = (
        f"Compiler: {toolchain.adapter}",
        f"Toolchain: {toolchain.display_name}",
        f"Selection: {toolchain.selection}",
        f"Reason: {toolchain.reason}",
        f"Target: {request.target}",
        f"Mode: {'release' if request.release else 'dev'}",
    )
    print("\n".join(lines), flush=True)


def import_ecf(source: Path, destination: Path) -> tuple[Project, str, list[str]]:
    source = source.resolve()
    tree = parse_ecf(source)
    root = tree.getroot()
    name = root.get("name")
    uuid_value = root.get("uuid")
    if not name or not uuid_value:
        raise EvmError("ECF system must define name and uuid")
    targets = root.xpath("./*[local-name()='target']")
    if not targets:
        raise EvmError("ECF contains no targets")
    warnings: list[str] = []
    unsupported_nodes: list[str] = []
    if len(targets) > 1:
        warnings.append(
            f"only the primary target is represented; {len(targets) - 1} additional target(s)"
        )
    allowed = {"root", "cluster"}
    for target in targets:
        for child in target:
            if isinstance(child.tag, str) and etree.QName(child).localname not in allowed:
                unsupported_nodes.append(etree.QName(child).localname)
    if unsupported_nodes:
        warnings.append("unrepresented ECF elements: " + ", ".join(sorted(set(unsupported_nodes))))
    primary = targets[0]
    root_nodes = primary.xpath("./*[local-name()='root']")
    application = bool(root_nodes and root_nodes[0].get("class"))
    root_class = root_nodes[0].get("class") if application else None
    root_feature = root_nodes[0].get("feature") if application else None
    if application and not root_feature:
        raise EvmError("application ECF root must define a creation feature")
    clusters = [
        item.get("location")
        for item in primary.xpath("./*[local-name()='cluster']")
        if item.get("location")
    ]
    if not clusters:
        warnings.append("primary target contains no source clusters")
        clusters = ["src"]
    level = "partial" if warnings else "lossless"
    destination = destination.resolve()
    manifest_path = destination / "Eiffel.toml"
    lock_path = destination / LOCK_NAME
    existing = next((path for path in (manifest_path, lock_path) if path.exists()), None)
    if existing is not None:
        raise EvmError(f"refusing to overwrite existing {existing}")
    imported_project = _ImportedProject(
        name=name,
        uuid=uuid_value,
        library=not application,
        root=Root(root_class, root_feature) if application else None,
        clusters=tuple(clusters),
        ecf=os.path.relpath(source, destination),
    )
    manifest = _imported_manifest(imported_project)
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write(manifest_path, manifest.encode())
    project = load_manifest(manifest_path)
    atomic_write(lock_path, serialize_lock(empty_lock(project)))
    return project, level, warnings


def _new_manifest(name: str, uuid_value: str, library: bool) -> str:
    kind = "library" if library else "application"
    result = (
        "[project]\n"
        f"name = {json.dumps(name)}\n"
        'version = "0.1.0"\n'
        f'type = "{kind}"\n'
        f'uuid = "{uuid_value}"\n'
        f'ecf = "{name}.ecf"\n'
        "ecf-managed = true\n"
    )
    if not library:
        result += '\n[root]\nclass = "APPLICATION"\nfeature = "make"\n'
    result += '\n[sources]\nclusters = ["src"]\n'
    return result


def _imported_manifest(project: _ImportedProject) -> str:
    kind = "library" if project.library else "application"
    values = ", ".join(json.dumps(item) for item in project.clusters)
    result = (
        "[project]\n"
        f"name = {json.dumps(project.name)}\n"
        'version = "0.0.0"\n'
        f'type = "{kind}"\n'
        f"uuid = {json.dumps(project.uuid)}\n"
        "ecf-managed = false\n"
        f"ecf = {json.dumps(project.ecf)}\n"
    )
    if project.root is not None:
        result += (
            f"\n[root]\nclass = {json.dumps(project.root.class_name)}\n"
            f"feature = {json.dumps(project.root.feature)}\n"
        )
    result += f"\n[sources]\nclusters = [{values}]\n"
    return result


def _application_source() -> str:
    return (
        "class\n"
        "    APPLICATION\n\n"
        "create\n"
        "    make\n\n"
        "feature {NONE} -- Initialization\n\n"
        "    make\n"
        "            -- Run the application.\n"
        "        do\n"
        '            print ("Hello from EVM!%N")\n'
        "        end\n\n"
        "end\n"
    )


def _library_source(name: str) -> str:
    class_name = _eiffel_class_name(name)
    return f"class\n    {class_name}\n\nend\n"


def _eiffel_class_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _normalized_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not result or not result[0].isalpha():
        result = f"project_{result}"
    return result


def _require_target(project: Project, target: str) -> None:
    try:
        project.target(target)
    except KeyError as error:
        names = ", ".join(item.name for item in project.targets)
        raise EvmError(f"unknown target {target!r}; available targets: {names}") from error


def _unresolved_variables(value: str) -> list[str]:
    variables = re.findall(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", value)
    return sorted(
        name
        for pair in variables
        for name in pair
        if name and name not in os.environ and name != "ECF_CONFIG_PATH"
    )


def _normalized_os() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    return "unix"
