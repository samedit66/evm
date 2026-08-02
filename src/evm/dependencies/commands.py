"""Transactional user operations for project dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from evm.dependencies.resolution import install_dependencies, resolve_dependencies
from evm.errors import EvmError
from evm.filesystem import atomic_write_many
from evm.formats.ecf import managed_ecf_bytes
from evm.lockfile import LOCK_NAME, LockFile, load_lock, serialize_lock
from evm.manifest import parse_manifest
from evm.model import Dependency, Project


def add_dependency(
    project: Project,
    dependency: Dependency,
    *,
    offline: bool = False,
) -> LockFile:
    section_name = "dev-dependencies" if dependency.development else "dependencies"
    document = _load_document(project.manifest_path)
    if _dependency_declared(document, dependency.name):
        raise EvmError(f"dependency {dependency.name!r} is already declared")
    section = document.get(section_name)
    if section is None:
        section = tomlkit.table()
        document.add(section_name, section)
    section.add(dependency.name, _dependency_value(dependency))
    return _apply_manifest_transaction(project, document, offline=offline)


def remove_dependency(project: Project, name: str, *, offline: bool = False) -> LockFile:
    document = _load_document(project.manifest_path)
    removed = False
    for section_name in ("dependencies", "dev-dependencies"):
        section = document.get(section_name)
        if section is not None and name in section:
            del section[name]
            removed = True
            if not section:
                del document[section_name]
    patch = document.get("patch")
    if patch is not None and name in patch:
        del patch[name]
        if not patch:
            del document["patch"]
    if not removed:
        raise EvmError(f"dependency {name!r} is not declared")
    return _apply_manifest_transaction(project, document, offline=offline)


def update_dependencies(
    project: Project,
    *,
    names: set[str] | None = None,
    precise: str | None = None,
    offline: bool = False,
) -> LockFile:
    if precise is not None:
        if names is None or len(names) != 1:
            raise EvmError("--precise requires exactly one dependency name")
        document = _load_document(project.manifest_path)
        _set_precise_revision(document, next(iter(names)), precise)
        return _apply_manifest_transaction(project, document, offline=offline)
    previous = _optional_lock(project)
    lock = resolve_dependencies(
        project,
        offline=offline,
        names=names,
        previous=previous,
    )
    install_dependencies(project, offline=offline, lock=lock)
    _write_resolved_state(project, project.manifest_path.read_bytes(), lock)
    return lock


def install_project(project: Project, *, offline: bool = False) -> LockFile:
    lock_path = project.directory / LOCK_NAME
    if lock_path.is_file():
        return install_dependencies(project, offline=offline)
    lock = resolve_dependencies(project, offline=offline)
    install_dependencies(project, offline=offline, lock=lock)
    _write_resolved_state(project, project.manifest_path.read_bytes(), lock)
    return lock


def _apply_manifest_transaction(
    current_project: Project,
    document: tomlkit.TOMLDocument,
    *,
    offline: bool,
) -> LockFile:
    manifest_bytes = tomlkit.dumps(document).encode()
    proposed = parse_manifest(manifest_bytes.decode(), current_project.manifest_path)
    lock = resolve_dependencies(
        proposed,
        offline=offline,
        previous=_optional_lock(current_project),
    )
    install_dependencies(proposed, offline=offline, lock=lock)
    _write_resolved_state(proposed, manifest_bytes, lock)
    return lock


def _write_resolved_state(project: Project, manifest: bytes, lock: LockFile) -> None:
    if not project.ecf_managed:
        raise EvmError("dependency commands cannot update a legacy unmanaged ECF")
    ecf = managed_ecf_bytes(project, lock=lock)
    atomic_write_many(
        {
            project.manifest_path: manifest,
            project.directory / LOCK_NAME: serialize_lock(lock),
            project.ecf_path: ecf,
        }
    )


def _load_document(path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TOMLKitError) as error:
        raise EvmError(f"cannot edit {path}: {error}") from error


def _dependency_declared(document: tomlkit.TOMLDocument, name: str) -> bool:
    return any(
        section is not None and name in section
        for section in (
            document.get("dependencies"),
            document.get("dev-dependencies"),
        )
    )


def _dependency_value(dependency: Dependency) -> Any:
    if (
        dependency.source == "iron"
        and dependency.version is not None
        and all(
            value is None
            for value in (
                dependency.git,
                dependency.path,
                dependency.library,
                dependency.ecf,
                dependency.subdir,
            )
        )
    ):
        return dependency.version
    value = tomlkit.inline_table()
    if dependency.source == "git":
        value["git"] = dependency.git
        if dependency.requested_kind and dependency.requested_value:
            value[dependency.requested_kind] = dependency.requested_value
    elif dependency.source == "path":
        value["path"] = dependency.path
    else:
        value["source"] = dependency.source
    for key, item in (
        ("version", dependency.version),
        ("library", dependency.library),
        ("ecf", dependency.ecf),
        ("subdir", dependency.subdir),
    ):
        if item is not None:
            value[key] = item
    return value


def _set_precise_revision(
    document: tomlkit.TOMLDocument,
    name: str,
    revision: str,
) -> None:
    for section_name in ("dependencies", "dev-dependencies"):
        section = document.get(section_name)
        if section is None or name not in section:
            continue
        value = section[name]
        if not isinstance(value, dict) or "git" not in value:
            raise EvmError(f"--precise is only valid for a Git dependency: {name}")
        for key in ("tag", "branch", "rev"):
            value.pop(key, None)
        value["rev"] = revision
        return
    raise EvmError(f"dependency {name!r} is not declared")


def _optional_lock(project: Project) -> LockFile | None:
    path = project.directory / LOCK_NAME
    return load_lock(path) if path.is_file() else None
