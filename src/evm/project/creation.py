"""Create new Eiffel projects from interchangeable scaffold templates."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from evm.ecf import generate_ecf
from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.lockfile import empty_lock, serialize_lock
from evm.manifest import load_manifest, parse_manifest
from evm.model import Project, Root
from evm.project.templates import APPLICATION_TEMPLATE, ProjectSource, ProjectTemplate


@dataclass(frozen=True)
class ProjectCreationRequest:
    """Collect the inputs required to create a new project scaffold."""

    directory: Path
    template: ProjectTemplate = APPLICATION_TEMPLATE
    initialize: bool = False
    scoop: bool = False


@dataclass(frozen=True)
class _ProjectFiles:
    manifest: Path
    lock: Path
    ecf: Path
    gitignore: Path
    source: Path


def create_project(request: ProjectCreationRequest) -> Project:
    """Create and load a project described by a scaffold template."""
    directory = request.directory.resolve()
    if directory.exists() and not directory.is_dir():
        raise EvmError(f"project path is not a directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    name = _normalized_project_name(directory.name)
    source = request.template.source(name)
    files = _project_files(directory, name, source)
    _ensure_scaffold_available(files, request.initialize)
    manifest = _new_manifest(
        name,
        str(uuid.uuid4()),
        request.template.kind,
        request.template.root,
        request.scoop,
    )
    project = parse_manifest(manifest, files.manifest)
    lock = empty_lock(project)
    ecf = generate_ecf(project, lock)
    _write_project_files(files, manifest, serialize_lock(lock), ecf, source.content)
    return load_manifest(files.manifest)


def _project_files(
    directory: Path,
    name: str,
    source: ProjectSource,
) -> _ProjectFiles:
    source_path = _scaffold_source_path(directory, source.relative_path)
    return _ProjectFiles(
        manifest=directory / "Eiffel.toml",
        lock=directory / "Eiffel.lock",
        ecf=directory / f"{name}.ecf",
        gitignore=directory / ".gitignore",
        source=source_path,
    )


def _scaffold_source_path(directory: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise EvmError(
            f"project template source path must stay inside the project: {relative_path}"
        )
    return directory / relative_path


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


def _new_manifest(
    name: str,
    uuid_value: str,
    kind: str,
    root: Root | None,
    scoop: bool,
) -> str:
    result = (
        "[project]\n"
        f"name = {json.dumps(name)}\n"
        'version = "0.1.0"\n'
        f"type = {json.dumps(kind)}\n"
        f"uuid = {json.dumps(uuid_value)}\n"
        f"ecf = {json.dumps(f'{name}.ecf')}\n"
        "ecf-managed = true\n"
    )
    if root is not None:
        result += (
            f"\n[root]\nclass = {json.dumps(root.class_name)}\n"
            f"feature = {json.dumps(root.feature)}\n"
        )
    result += '\n[sources]\nclusters = ["src"]\n'
    if scoop:
        result += '\n[requires]\nconcurrency = "scoop"\n'
    return result


def _write_project_files(
    files: _ProjectFiles,
    manifest: str,
    lock: bytes,
    ecf: bytes,
    source: str,
) -> None:
    files.source.parent.mkdir(parents=True, exist_ok=True)
    (files.manifest.parent / "tests").mkdir(exist_ok=True)
    atomic_write(files.manifest, manifest.encode())
    atomic_write(files.lock, lock)
    atomic_write(files.ecf, ecf)
    atomic_write(files.source, source.encode())
    if not files.gitignore.exists():
        atomic_write(files.gitignore, b".evm/\nbuild/\n")


def _normalized_project_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not result or not result[0].isalpha():
        result = f"project_{result}"
    return result
