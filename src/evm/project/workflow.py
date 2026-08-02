"""Project creation, validation, import, and execution orchestration."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any

import tomlkit
from lxml import etree

from evm.dependencies.resolution import install_dependencies
from evm.errors import EvmError
from evm.filesystem import atomic_write_many
from evm.formats.ecf import (
    ensure_managed_ecf,
    parse_ecf,
    prepare_legacy_ecf,
    validate_ecf,
)
from evm.formats.iron import (
    IRON_PACKAGE_NAME,
    iron_package_diagnostics,
    load_iron_package,
    select_iron_project,
)
from evm.lockfile import LOCK_NAME, empty_lock, load_lock, serialize_lock
from evm.manifest import parse_manifest
from evm.model import BuildRequest, PackageMetadata, Project, Root, Target, TestConfiguration
from evm.toolchain.compilers import compiler_adapter
from evm.toolchain.selection import (
    Toolchain,
    artifact_candidates,
    compiler_command,
    prepare_build_directory,
    run_compiler,
    select_toolchain,
)
from evm.workspace import is_workspace_member_dependency


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
    test: TestConfiguration | None


@dataclass(frozen=True)
class _EcfImportAnalysis:
    project: _ImportedProject
    level: str
    warnings: tuple[str, ...]


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
    iron_path = project.directory / IRON_PACKAGE_NAME
    if iron_path.is_file():
        diagnostics.extend(iron_package_diagnostics(load_iron_package(iron_path), project))
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
        if (
            dependency.source == "path"
            and dependency.path is not None
            and not is_workspace_member_dependency(project, dependency.path)
        ):
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
    *,
    announce: bool = True,
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
    compilation_project = prepare_compilation_project(project, toolchain)
    command = compiler_command(toolchain, compilation_project, request, check_only)
    if announce:
        print_toolchain(toolchain, request=request)
    run_compiler(command, build_directory, capture_output=not announce)
    return toolchain


def _legacy_ecf_variables(toolchain: Toolchain) -> dict[str, Path]:
    return dict(compiler_adapter(toolchain.adapter).legacy_ecf_variables(toolchain))


def prepare_compilation_project(project: Project, toolchain: Toolchain) -> Project:
    if project.ecf_managed:
        return project
    return replace(
        project,
        ecf_path=prepare_legacy_ecf(project, _legacy_ecf_variables(toolchain)),
    )


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
    return _write_import(analysis, destination)


def import_iron(
    source: Path,
    destination: Path,
    project_name: str | None = None,
) -> tuple[Project, str, list[str]]:
    source = source.resolve()
    destination = destination.resolve()
    package = load_iron_package(source)
    ecf = select_iron_project(package, project_name, source.parent)
    analysis = _analyze_ecf_import(ecf, destination)
    warnings = list(analysis.warnings)
    if package.name != analysis.project.name:
        warnings.append(
            f"IRON package name {package.name!r} differs from ECF system "
            f"name {analysis.project.name!r}; using the IRON package name"
        )
        analysis = replace(analysis, project=replace(analysis.project, name=package.name))
    if package.has_setup:
        warnings.append("IRON setup declarations were not imported or executed")
    if package.unknown_notes:
        names = ", ".join(package.unknown_notes)
        warnings.append(f"unsupported IRON notes were not imported: {names}")
    imported = _write_import(analysis, destination, package.metadata)
    level = "partial" if package.has_setup or package.unknown_notes else imported[1]
    return imported[0], level, [*warnings]


def _write_import(
    analysis: _EcfImportAnalysis,
    destination: Path,
    metadata: PackageMetadata | None = None,
) -> tuple[Project, str, list[str]]:
    manifest_path = destination / "Eiffel.toml"
    manifest = _imported_manifest(analysis.project)
    if metadata is not None:
        manifest = _add_package_metadata(manifest, metadata)
    try:
        parsed_project = parse_manifest(manifest, manifest_path)
    except EvmError as error:
        raise EvmError(f"Import level: unsupported\n{error}") from error
    files = {
        manifest_path: manifest.encode(),
        destination / LOCK_NAME: serialize_lock(empty_lock(parsed_project)),
    }
    existing = next((path for path in files if path.exists()), None)
    if existing is not None:
        raise EvmError(f"refusing to overwrite existing {existing}")
    atomic_write_many(files)
    return parsed_project, analysis.level, list(analysis.warnings)


def _add_package_metadata(manifest: str, metadata: PackageMetadata) -> str:
    document = tomlkit.parse(manifest)
    package = tomlkit.table()
    for name, value in (
        ("title", metadata.title),
        ("description", metadata.description),
        ("license", metadata.license),
        ("copyright", metadata.copyright),
    ):
        if value is not None:
            package.add(name, value)
    if metadata.tags:
        package.add("tags", list(metadata.tags))
    if metadata.links:
        links = tomlkit.table()
        for link in metadata.links:
            value = tomlkit.inline_table()
            if link.title is not None:
                value.add("title", link.title)
            value.add("url", link.url)
            links.add(link.category, value)
        package.add("links", links)
    if metadata.iron_maps:
        iron = tomlkit.table()
        iron.add("maps", list(metadata.iron_maps))
        package.add("iron", iron)
    if package:
        document.add("package", package)
    return tomlkit.dumps(document)


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
    library_target = root.get("library_target")
    primary_element = _primary_import_target(targets, library_target)
    if primary_element.get("name") != "default":
        warnings.append(
            f"ECF has no 'default' target; {primary_element.get('name')!r} is the manifest default"
        )
    imported_targets = tuple(
        _import_target(target, source.parent, destination, warnings) for target in targets
    )
    primary_index = targets.index(primary_element)
    primary = imported_targets[primary_index]
    library = library_target is not None or _is_library_target(primary_element)
    application = not library
    if application and primary.root is None and primary.extends is None:
        warnings.append("primary application target has no root or parent target")
        inherited_root = next(target.root for target in imported_targets if target.root is not None)
        primary = replace(primary, root=inherited_root)
    if not primary.clusters:
        warnings.append("primary target contains no source clusters")
        primary = replace(primary, clusters=("src",))

    manifest_targets = tuple(
        _manifest_import_target(target, primary.name, application, warnings)
        for index, target in enumerate(imported_targets)
        if index != primary_index
    )
    test = _import_test_configuration(root, targets, warnings)
    return _EcfImportAnalysis(
        project=_ImportedProject(
            name=name,
            uuid=uuid_value,
            library=library,
            primary=primary,
            targets=manifest_targets,
            ecf=os.path.relpath(source, destination),
            test=test,
        ),
        level="lossless",
        warnings=tuple(warnings),
    )


def _primary_import_target(
    targets: list[etree._Element],
    library_target: str | None,
) -> etree._Element:
    requested_name = library_target or "default"
    selected = next((target for target in targets if target.get("name") == requested_name), None)
    if selected is not None:
        return selected
    if library_target is not None:
        raise EvmError(
            f"Import level: unsupported\nECF library_target {library_target!r} does not exist"
        )
    return targets[0]


def _is_library_target(target: etree._Element) -> bool:
    roots = target.xpath("./*[local-name()='root']")
    return not roots or roots[0].get("all_classes") == "true"


def _import_test_configuration(
    system: etree._Element,
    targets: list[etree._Element],
    warnings: list[str],
) -> TestConfiguration | None:
    named = [target for target in targets if target.get("name", "").casefold() in {"test", "tests"}]
    candidates = named or [
        target for target in targets if _target_inherits_library(system, target, "testing")
    ]
    if len(candidates) != 1:
        if len(candidates) > 1:
            names = ", ".join(repr(target.get("name")) for target in candidates)
            warnings.append(f"multiple test targets detected; configure [test].target: {names}")
        return None
    target = candidates[0]
    target_name = target.get("name")
    if target_name is None:
        return None
    runner = "autotest" if _target_inherits_library(system, target, "testing") else "target"
    warnings.append(f"test target {target_name!r} detected with {runner!r} runner")
    return TestConfiguration(target_name, runner)


def _target_inherits_library(
    system: etree._Element,
    target: etree._Element,
    library: str,
) -> bool:
    current: etree._Element | None = target
    while current is not None:
        if current.xpath("./*[local-name()='library'][@name=$name]", name=library):
            return True
        parent_name = current.get("extends")
        if parent_name is None:
            return False
        parents = system.xpath(
            "./*[local-name()='target'][@name=$name]",
            name=parent_name,
        )
        current = parents[0] if len(parents) == 1 else None
    return False


def _import_path_warnings(root: etree._Element) -> list[str]:
    warnings: list[str] = []
    library_count = len(root.xpath("./*[local-name()='target']/*[local-name()='library']"))
    if library_count:
        noun = "library" if library_count == 1 else "libraries"
        verb = "remains" if library_count == 1 else "remain"
        warnings.append(f"{library_count} {noun} {verb} defined by the source legacy ECF")
    absolute_locations = [
        location
        for node in root.xpath(
            "./*[local-name()='target']/*[local-name()='cluster' or local-name()='library']"
        )
        if (location := node.get("location")) and _is_absolute_ecf_path(location)
    ]
    if absolute_locations:
        warnings.append(
            f"{len(absolute_locations)} cluster or library path(s) are absolute and non-portable"
        )
    return warnings


def _manifest_import_target(
    target: _ImportedTarget,
    primary_name: str,
    application: bool,
    warnings: list[str],
) -> _ImportedTarget:
    extends = target.extends
    if application and target.root is None and extends is None:
        warnings.append(
            f"application target {target.name!r} has no root or parent; "
            f"the manifest representation inherits {primary_name!r}"
        )
        extends = primary_name
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
    if _is_absolute_ecf_path(location) or "$" in location:
        return location
    portable_location = location.replace("\\", "/")
    return Path(os.path.relpath(source_directory / portable_location, destination)).as_posix()


def _is_absolute_ecf_path(location: str) -> bool:
    return Path(location).is_absolute() or PureWindowsPath(location).is_absolute()


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
    if project.primary.name != "default":
        result += f"default-target = {json.dumps(project.primary.name)}\n"
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
    if project.test is not None:
        result += (
            f"\n[test]\ntarget = {json.dumps(project.test.target)}\n"
            f"runner = {json.dumps(project.test.runner)}\n"
        )
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
