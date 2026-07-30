"""Project creation, validation, import, and execution orchestration."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lxml import etree

from evm.dependencies import install_dependencies
from evm.ecf import ensure_managed_ecf, generate_ecf, parse_ecf, validate_ecf
from evm.errors import EvmError
from evm.filesystem import atomic_write, atomic_write_many
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
class _ImportedTarget:
    name: str
    root: Root | None
    clusters: tuple[str, ...]
    extends: str | None


@dataclass(frozen=True)
class _ImportedProject:
    name: str
    uuid: str
    library: bool
    primary: _ImportedTarget
    targets: tuple[_ImportedTarget, ...]
    ecf: str
    overlay: str | None


@dataclass(frozen=True)
class _EcfImportAnalysis:
    project: _ImportedProject
    level: str
    warnings: tuple[str, ...]
    overlay: bytes | None


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
    destination = destination.resolve()
    analysis = _analyze_ecf_import(source, destination)
    manifest_path = destination / "Eiffel.toml"
    manifest = _imported_manifest(analysis.project)
    try:
        parsed_project = parse_manifest(manifest, manifest_path)
    except EvmError as error:
        raise EvmError(f"Import level: unsupported\n{error}") from error
    files = {
        manifest_path: manifest.encode(),
        destination / LOCK_NAME: serialize_lock(empty_lock(parsed_project)),
    }
    if analysis.overlay is not None:
        files[destination / "config" / "imported.ecf"] = analysis.overlay
    existing = next((path for path in files if path.exists()), None)
    if existing is not None:
        raise EvmError(f"refusing to overwrite existing {existing}")
    atomic_write_many(files)
    return parsed_project, analysis.level, list(analysis.warnings)


def _analyze_ecf_import(source: Path, destination: Path) -> _EcfImportAnalysis:
    try:
        tree = parse_ecf(source)
    except EvmError as error:
        raise EvmError(f"Import level: unsupported\n{error}") from error
    root = tree.getroot()
    name = root.get("name")
    uuid_value = root.get("uuid")
    if not name or not uuid_value:
        raise EvmError("Import level: unsupported\nECF system must define name and uuid")
    targets = root.xpath("./*[local-name()='target']")
    if not targets:
        raise EvmError("Import level: unsupported\nECF contains no targets")
    warnings: list[str] = []
    warnings.extend(_import_path_warnings(root))
    primary_element = next((target for target in targets if target.get("name") == "default"), None)
    if primary_element is None:
        primary_element = targets[0]
        warnings.append(
            f"ECF has no 'default' target; {primary_element.get('name')!r} is the manifest default"
        )
    imported_targets = tuple(
        _import_target(target, source.parent, destination, warnings) for target in targets
    )
    primary_index = targets.index(primary_element)
    primary = imported_targets[primary_index]
    application = any(target.root is not None for target in imported_targets)
    if application and primary.root is None and primary.extends is None:
        warnings.append("primary application target has no root or parent target")
        inherited_root = next(target.root for target in imported_targets if target.root is not None)
        primary = replace(primary, root=inherited_root)
    if not primary.clusters:
        warnings.append("primary target contains no source clusters")
        primary = replace(primary, clusters=("src",))

    has_unsafe_doctype = bool(tree.docinfo.doctype)
    overlay_needed = _requires_import_overlay(root)
    if has_unsafe_doctype:
        warnings.append("document type declarations cannot be copied into a safe ECF overlay")
    manifest_targets = tuple(
        _manifest_import_target(target, primary.name, application, warnings)
        for index, target in enumerate(imported_targets)
        if index != primary_index
    )
    preserve_overlay = overlay_needed and not has_unsafe_doctype
    return _EcfImportAnalysis(
        project=_ImportedProject(
            name=name,
            uuid=uuid_value,
            library=not application,
            primary=primary,
            targets=manifest_targets,
            ecf=os.path.relpath(source, destination),
            overlay="config/imported.ecf" if preserve_overlay else None,
        ),
        level=_import_level(has_unsafe_doctype, overlay_needed),
        warnings=tuple(warnings),
        overlay=(
            etree.tostring(root, encoding="UTF-8", xml_declaration=True)
            if preserve_overlay
            else None
        ),
    )


def _import_path_warnings(root: etree._Element) -> list[str]:
    warnings: list[str] = []
    library_count = len(root.xpath("./*[local-name()='target']/*[local-name()='library']"))
    if library_count:
        noun = "library" if library_count == 1 else "libraries"
        warnings.append(f"{library_count} {noun} retained as opaque ECF overlay entries")
    absolute_locations = [
        location
        for node in root.xpath(
            "./*[local-name()='target']/*[local-name()='cluster' or local-name()='library']"
        )
        if (location := node.get("location")) and Path(location).is_absolute()
    ]
    if absolute_locations:
        warnings.append(
            f"{len(absolute_locations)} cluster or library path(s) are absolute and non-portable"
        )
    return warnings


def _import_level(has_unsafe_doctype: bool, overlay_needed: bool) -> str:
    if has_unsafe_doctype:
        return "partial"
    if overlay_needed:
        return "lossless-with-overlay"
    return "lossless"


def _manifest_import_target(
    target: _ImportedTarget,
    primary_name: str,
    application: bool,
    warnings: list[str],
) -> _ImportedTarget:
    extends = "default" if target.extends == primary_name else target.extends
    if application and target.root is None and extends is None:
        warnings.append(
            f"application target {target.name!r} has no root or parent; "
            "the manifest representation inherits the default target"
        )
        extends = "default"
    return replace(target, extends=extends)


def _import_target(
    target: etree._Element,
    source_directory: Path,
    destination: Path,
    warnings: list[str],
) -> _ImportedTarget:
    name = target.get("name")
    if not name:
        raise EvmError("Import level: unsupported\nall ECF targets must define a name")
    root_nodes = target.xpath("./*[local-name()='root']")
    imported_root: Root | None = None
    if root_nodes and root_nodes[0].get("class"):
        class_name = root_nodes[0].get("class")
        feature = root_nodes[0].get("feature")
        if not feature:
            raise EvmError(
                f"Import level: unsupported\napplication ECF target {name!r} "
                "root must define a creation feature"
            )
        imported_root = Root(class_name, feature)
    clusters = tuple(
        _imported_cluster_location(location, source_directory, destination)
        for item in target.xpath("./*[local-name()='cluster']")
        if (location := item.get("location"))
    )
    missing_locations = len(target.xpath("./*[local-name()='cluster']")) - len(clusters)
    if missing_locations:
        warnings.append(f"target {name!r} has {missing_locations} cluster(s) without a location")
    return _ImportedTarget(name, imported_root, clusters, target.get("extends"))


def _imported_cluster_location(
    location: str,
    source_directory: Path,
    destination: Path,
) -> str:
    if Path(location).is_absolute() or "$" in location:
        return location
    return os.path.relpath(source_directory / location, destination)


def _requires_import_overlay(root: etree._Element) -> bool:
    system_attributes = {etree.QName(attribute).localname for attribute in root.attrib} - {
        "name",
        "uuid",
        "library_target",
        "schemaLocation",
    }
    if system_attributes:
        return True
    if any(
        isinstance(child.tag, str) and etree.QName(child).localname != "target" for child in root
    ):
        return True
    for target in root.xpath("./*[local-name()='target']"):
        if set(target.attrib) - {"name", "extends"}:
            return True
        for child in target:
            if not isinstance(child.tag, str):
                continue
            local_name = etree.QName(child).localname
            if local_name not in {"root", "cluster"}:
                return True
            if local_name == "cluster":
                if set(child.attrib) - {"name", "location", "recursive"} or len(child):
                    return True
            elif set(child.attrib) - {"class", "feature", "all_classes"} or len(child):
                return True
    return False


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
    values = ", ".join(json.dumps(item) for item in project.primary.clusters)
    result = (
        "[project]\n"
        f"name = {json.dumps(project.name)}\n"
        'version = "0.0.0"\n'
        f'type = "{kind}"\n'
        f"uuid = {json.dumps(project.uuid)}\n"
        "ecf-managed = false\n"
        f"ecf = {json.dumps(project.ecf)}\n"
    )
    if project.primary.root is not None:
        result += (
            f"\n[root]\nclass = {json.dumps(project.primary.root.class_name)}\n"
            f"feature = {json.dumps(project.primary.root.feature)}\n"
        )
    result += f"\n[sources]\nclusters = [{values}]\n"
    for target in project.targets:
        result += f"\n[targets.{json.dumps(target.name)}]\n"
        if target.root is not None:
            result += f"root = {json.dumps(f'{target.root.class_name}.{target.root.feature}')}\n"
        if target.clusters:
            target_sources = ", ".join(json.dumps(item) for item in target.clusters)
            result += f"sources = [{target_sources}]\n"
        if target.extends is not None:
            result += f"extends = {json.dumps(target.extends)}\n"
    if project.overlay is not None:
        result += f"\n[ecf]\ninclude = [{json.dumps(project.overlay)}]\n"
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
