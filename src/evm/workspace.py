"""Workspace discovery and package ordering."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from evm.errors import EvmError
from evm.manifest import MANIFEST_NAME, find_manifest, load_manifest
from evm.model import Project


@dataclass(frozen=True)
class Workspace:
    root: Path
    manifest_path: Path
    packages: tuple[Project, ...]

    def package(self, name: str) -> Project:
        for package in self.packages:
            if package.name == name:
                return package
        raise EvmError(f"unknown workspace package {name!r}")

    def ordered_packages(self, selected: str | None = None) -> tuple[Project, ...]:
        packages = {package.name: package for package in self.packages}
        dependencies = {
            package.name: _workspace_dependencies(package, packages) for package in self.packages
        }
        required = (
            set(packages) if selected is None else _dependency_closure(selected, dependencies)
        )
        ordered: list[Project] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                cycle = " -> ".join((*visiting, name))
                raise EvmError(f"workspace dependency cycle detected: {cycle}")
            if name in visited or name not in required:
                return
            visiting.append(name)
            for dependency in dependencies[name]:
                visit(dependency)
            visiting.pop()
            visited.add(name)
            ordered.append(packages[name])

        if selected is not None and selected not in packages:
            raise EvmError(f"unknown workspace package {selected!r}")
        for package in self.packages:
            visit(package.name)
        return tuple(ordered)


@dataclass(frozen=True)
class ProjectContext:
    project: Project | None
    workspace: Workspace | None


@dataclass
class _WorkspaceTree:
    lines: list[str]
    dependencies: dict[str, tuple[str, ...]]


def load_project_context(start: Path | None = None) -> ProjectContext:
    manifest_path = find_manifest(start)
    workspace = load_workspace(manifest_path)
    if workspace is not None:
        project = next(
            (
                package
                for package in workspace.packages
                if package.directory == manifest_path.parent.resolve()
            ),
            None,
        )
        return ProjectContext(project, workspace)
    return ProjectContext(load_manifest(manifest_path), None)


def load_workspace(path: Path) -> Workspace | None:
    document = _load_toml(path)
    raw_workspace = document.get("workspace")
    if raw_workspace is None:
        return _find_parent_workspace(path.parent)
    if not isinstance(raw_workspace, dict):
        raise EvmError("workspace must be a table")
    unknown = sorted(set(raw_workspace) - {"members"})
    if unknown:
        raise EvmError(f"unknown manifest field: workspace.{unknown[0]}")
    members = raw_workspace.get("members")
    if not isinstance(members, list) or not members:
        raise EvmError("workspace.members must be a non-empty array of strings")
    root = path.parent.resolve()
    projects: list[Project] = []
    for index, member in enumerate(members):
        if not isinstance(member, str) or not member:
            raise EvmError(f"workspace.members[{index}] must be a non-empty string")
        member_directory = (root / member).resolve()
        try:
            member_directory.relative_to(root)
        except ValueError as error:
            raise EvmError(f"workspace.members[{index}] escapes the workspace root") from error
        projects.append(
            replace(
                load_manifest(member_directory / MANIFEST_NAME),
                workspace_root=root,
            )
        )
    names = [project.name for project in projects]
    if len(names) != len(set(names)):
        raise EvmError("workspace package names must be unique")
    return Workspace(root, path.resolve(), tuple(projects))


def workspace_tree_lines(workspace: Workspace) -> list[str]:
    packages = {package.name: package for package in workspace.packages}
    dependencies = {
        package.name: _workspace_dependencies(package, packages) for package in workspace.packages
    }
    depended_on = {name for values in dependencies.values() for name in values}
    roots = [name for name in packages if name not in depended_on]
    tree = _WorkspaceTree([], dependencies)
    for index, name in enumerate(roots):
        _append_package_tree(tree, name, "", index == len(roots) - 1, root=True)
    return tree.lines


def is_workspace_member_dependency(project: Project, dependency_path: str) -> bool:
    if project.workspace_root is None:
        return False
    candidate = (project.directory / dependency_path).resolve()
    manifest = candidate / MANIFEST_NAME
    if not manifest.is_file():
        return False
    workspace = load_workspace(project.workspace_root / MANIFEST_NAME)
    return workspace is not None and any(
        package.directory == candidate for package in workspace.packages
    )


def _find_parent_workspace(directory: Path) -> Workspace | None:
    for parent in directory.parents:
        candidate = parent / MANIFEST_NAME
        if not candidate.is_file():
            continue
        document = _load_toml(candidate)
        if "workspace" in document:
            workspace = load_workspace(candidate)
            if workspace is not None and any(
                package.directory == directory.resolve() for package in workspace.packages
            ):
                return workspace
    return None


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvmError(f"manifest not found: {path}") from error
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise EvmError(f"cannot parse {path}: {error}") from error


def _workspace_dependencies(
    project: Project,
    packages: dict[str, Project],
) -> tuple[str, ...]:
    directories = {package.directory: package.name for package in packages.values()}
    result: list[str] = []
    for dependency in project.dependencies:
        if dependency.source != "path" or dependency.path is None:
            continue
        name = directories.get((project.directory / dependency.path).resolve())
        if name is not None:
            if dependency.name != name:
                raise EvmError(
                    f"workspace dependency {dependency.name!r} points to package {name!r}"
                )
            result.append(name)
    return tuple(result)


def _dependency_closure(
    selected: str,
    dependencies: dict[str, tuple[str, ...]],
) -> set[str]:
    if selected not in dependencies:
        raise EvmError(f"unknown workspace package {selected!r}")
    result = {selected}
    pending = [selected]
    while pending:
        name = pending.pop()
        for dependency in dependencies[name]:
            if dependency not in result:
                result.add(dependency)
                pending.append(dependency)
    return result


def _append_package_tree(
    tree: _WorkspaceTree,
    name: str,
    prefix: str,
    last: bool,
    *,
    root: bool = False,
) -> None:
    tree.lines.append(name if root else f"{prefix}{'└── ' if last else '├── '}{name}")
    children = tree.dependencies[name]
    child_prefix = prefix if root else prefix + ("    " if last else "│   ")
    for index, child in enumerate(children):
        _append_package_tree(
            tree,
            child,
            child_prefix,
            index == len(children) - 1,
        )
