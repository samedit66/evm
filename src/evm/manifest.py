"""Parsing and validation for Eiffel.toml."""

from __future__ import annotations

import re
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from evm.errors import EvmError
from evm.model import CompilerRequirement, Condition, Project, Root, Target
from evm.versioning import NumericVersion, validate_constraint

MANIFEST_NAME = "Eiffel.toml"
_TOP_LEVEL = {
    "project",
    "root",
    "sources",
    "compatibility",
    "requires",
    "targets",
    "conditions",
    "compiler",
}
_REQUIRES = {
    "standard": {"ecma", "ise"},
    "void-safety": {"none", "conformance", "initialization", "all"},
    "concurrency": {"none", "thread", "scoop"},
    "ise-semantics": None,
}
_CONDITION_WHEN_VALUES: dict[str, set[str]] = {
    "os": {"windows", "unix", "macos", "darwin"},
    "compiler": {"ise", "gobo"},
    "mode": {"dev", "release"},
}
_CONDITION_WHEN_FIELDS = {*_CONDITION_WHEN_VALUES, "architecture"}
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True)
class _ProjectMetadata:
    name: str
    version: str
    kind: str
    uuid: str
    ecf_path: Path
    ecf_managed: bool


def find_manifest(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / MANIFEST_NAME
        if candidate.is_file():
            return candidate
    raise EvmError(f"{MANIFEST_NAME} not found in {current} or its parents")


def load_manifest(path: Path) -> Project:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise EvmError(f"manifest not found: {path}") from error
    except (OSError, UnicodeError) as error:
        raise EvmError(f"cannot read {path}: {error}") from error
    return parse_manifest(content, path)


def parse_manifest(content: str, path: Path) -> Project:
    try:
        document = tomlkit.parse(content)
    except TOMLKitError as error:
        raise EvmError(f"cannot parse {path}: {error}") from error

    unknown = sorted(set(document) - _TOP_LEVEL)
    if unknown:
        raise EvmError(f"unknown manifest field: {unknown[0]}")

    metadata = _parse_project_metadata(_table(document, "project"), path)
    root = _parse_root(document.get("root"), metadata.kind)
    sources_table = _table(document, "sources")
    _reject_unknown(sources_table, {"clusters"}, "sources")
    sources = _string_list(sources_table.get("clusters"), "sources.clusters", required=True)
    targets = [Target("default", root, sources)]
    targets.extend(_parse_targets(document.get("targets"), metadata.kind))
    _validate_target_graph(targets)

    compilers = _parse_compilers(document.get("compatibility"))
    requires = _parse_requires(document.get("requires"))
    conditions = _parse_conditions(document.get("conditions"), {target.name for target in targets})
    compiler_arguments = _parse_compiler_arguments(document.get("compiler"))
    return Project(
        manifest_path=path.resolve(),
        name=metadata.name,
        version=metadata.version,
        kind=metadata.kind,
        uuid=metadata.uuid,
        ecf_path=metadata.ecf_path,
        ecf_managed=metadata.ecf_managed,
        targets=tuple(targets),
        conditions=conditions,
        compilers=compilers,
        requires=requires,
        compiler_arguments=compiler_arguments,
    )


def _parse_project_metadata(
    project_table: Mapping[str, Any],
    manifest_path: Path,
) -> _ProjectMetadata:
    _reject_unknown(
        project_table,
        {"name", "version", "type", "uuid", "ecf-managed", "ecf"},
        "project",
    )
    name = _required_string(project_table, "name", "project.name")
    if _NAME_RE.fullmatch(name) is None:
        raise EvmError("project.name must start with a letter and contain letters, digits, _ or -")
    version = _required_string(project_table, "version", "project.version")
    if _SEMVER_RE.fullmatch(version) is None:
        raise EvmError("project.version must be a semantic version such as 0.1.0")
    kind = _required_string(project_table, "type", "project.type")
    if kind not in {"application", "library"}:
        raise EvmError("project.type must be 'application' or 'library'")
    project_uuid = _parse_uuid(_required_string(project_table, "uuid", "project.uuid"))
    ecf_managed = project_table.get("ecf-managed", True)
    if not isinstance(ecf_managed, bool):
        raise EvmError("project.ecf-managed must be a boolean")
    ecf_name = project_table.get("ecf", f"{name}.ecf")
    if not isinstance(ecf_name, str) or not ecf_name:
        raise EvmError("project.ecf must be a non-empty string")
    ecf_path = (
        _safe_project_path(manifest_path.parent, ecf_name, "project.ecf")
        if ecf_managed
        else (manifest_path.parent / ecf_name).resolve()
    )
    return _ProjectMetadata(name, version, kind, project_uuid, ecf_path, ecf_managed)


def _parse_uuid(value: str) -> str:
    try:
        return str(uuid_module.UUID(value))
    except ValueError as error:
        raise EvmError("project.uuid must be a valid UUID") from error


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if not isinstance(value, Mapping):
        raise EvmError(f"missing or invalid [{name}] section")
    return value


def _required_string(table: Mapping[str, Any], key: str, path: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise EvmError(f"{path} must be a non-empty string")
    return value


def _string_list(value: Any, path: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise EvmError(f"{path} must be a non-empty array of strings")
    return tuple(value)


def _parse_root(value: Any, kind: str) -> Root | None:
    if value is None:
        if kind == "application":
            raise EvmError("application requires [root] with class and feature")
        return None
    if not isinstance(value, Mapping):
        raise EvmError("root must be a table")
    _reject_unknown(value, {"class", "feature"}, "root")
    return Root(
        _required_string(value, "class", "root.class"),
        _required_string(value, "feature", "root.feature"),
    )


def _parse_targets(value: Any, project_kind: str) -> list[Target]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        raise EvmError("targets must be a table")
    result: list[Target] = []
    for name, table in value.items():
        if name in {"default", "release"}:
            raise EvmError(f"targets.{name} is reserved")
        if not isinstance(table, Mapping):
            raise EvmError(f"targets.{name} must be a table")
        _reject_unknown(table, {"root", "sources", "extends"}, f"targets.{name}")
        root_value = table.get("root")
        root: Root | None = None
        if root_value is not None:
            if not isinstance(root_value, str) or "." not in root_value:
                raise EvmError(f"targets.{name}.root must have the form CLASS.feature")
            class_name, feature = root_value.rsplit(".", 1)
            root = Root(class_name, feature)
        sources = _string_list(table.get("sources"), f"targets.{name}.sources")
        extends = table.get("extends")
        if extends is not None and not isinstance(extends, str):
            raise EvmError(f"targets.{name}.extends must be a string")
        if project_kind == "application" and root is None and extends is None:
            raise EvmError(f"targets.{name} requires root or extends")
        result.append(Target(name, root, sources, extends))
    return result


def _validate_target_graph(targets: list[Target]) -> None:
    by_name = {target.name: target for target in targets}
    for target in targets:
        if target.extends is not None and target.extends not in by_name:
            raise EvmError(f"target {target.name!r} extends unknown target {target.extends!r}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise EvmError(f"target inheritance cycle involving {name!r}")
        if name in visited:
            return
        visiting.add(name)
        parent = by_name[name].extends
        if parent is not None:
            visit(parent)
        visiting.remove(name)
        visited.add(name)

    for name in by_name:
        visit(name)


def _parse_compilers(value: Any) -> tuple[CompilerRequirement, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise EvmError("compatibility must be a table")
    _reject_unknown(value, {"compilers"}, "compatibility")
    entries = _string_list(value.get("compilers"), "compatibility.compilers", required=True)
    result: list[CompilerRequirement] = []
    seen: set[str] = set()
    for entry in entries:
        parts = entry.split(maxsplit=1)
        adapter = parts[0]
        if adapter not in {"ise", "gobo"}:
            raise EvmError(f"unknown compiler adapter {adapter!r}; known adapters: ise, gobo")
        if adapter in seen:
            raise EvmError(f"duplicate compiler adapter in compatibility.compilers: {adapter}")
        constraint = parts[1] if len(parts) == 2 else None
        if constraint is not None:
            validate_constraint(constraint)
        result.append(CompilerRequirement(adapter, constraint))
        seen.add(adapter)
    return tuple(result)


def _parse_requires(value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise EvmError("requires must be a table")
    unknown = sorted(set(value) - set(_REQUIRES))
    if unknown:
        allowed = ", ".join(sorted(_REQUIRES))
        raise EvmError(f"unknown requires.{unknown[0]}; allowed keys: {allowed}")
    result: list[tuple[str, str]] = []
    for key, raw in value.items():
        if not isinstance(raw, str):
            raise EvmError(f"requires.{key} must be a string")
        choices = _REQUIRES[key]
        if choices is None:
            NumericVersion.parse(raw)
        elif raw not in choices:
            allowed = ", ".join(sorted(choices))
            raise EvmError(f"invalid requires.{key}: {raw!r}; allowed: {allowed}")
        result.append((key, raw))
    return tuple(sorted(result))


def _parse_conditions(value: Any, target_names: set[str]) -> tuple[Condition, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EvmError("conditions must use [[conditions]] array tables")
    return tuple(_parse_condition(item, index, target_names) for index, item in enumerate(value))


def _parse_condition(item: Any, index: int, target_names: set[str]) -> Condition:
    path = f"conditions[{index}]"
    if not isinstance(item, Mapping):
        raise EvmError(f"{path} must be a table")
    _reject_unknown(
        item,
        {"target", "when", "sources", "external-objects"},
        path,
    )
    target = item.get("target", "default")
    if not isinstance(target, str) or target not in target_names:
        raise EvmError(f"{path}.target refers to unknown target {target!r}")
    when = _parse_condition_predicate(item.get("when"), path)
    sources = _string_list(item.get("sources"), f"{path}.sources")
    external = _string_list(item.get("external-objects"), f"{path}.external-objects")
    if not sources and not external:
        raise EvmError(f"{path} must define sources or external-objects")
    return Condition(target, when, sources, external)


def _parse_condition_predicate(value: Any, path: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not value:
        raise EvmError(f"{path}.when must be a non-empty inline table")
    unknown = sorted(set(value) - _CONDITION_WHEN_FIELDS)
    if unknown:
        allowed = ", ".join(sorted(_CONDITION_WHEN_FIELDS))
        raise EvmError(f"unknown {path}.when.{unknown[0]}; allowed: {allowed}")
    pairs: list[tuple[str, str]] = []
    for key, raw in value.items():
        if not isinstance(raw, str) or not raw:
            raise EvmError(f"{path}.when.{key} must be a non-empty string")
        normalized = raw.lower()
        allowed_values = _CONDITION_WHEN_VALUES.get(key)
        if allowed_values is not None and normalized not in allowed_values:
            allowed = ", ".join(sorted(allowed_values))
            raise EvmError(f"invalid {path}.when.{key}: {raw!r}; allowed: {allowed}")
        pairs.append((key, normalized))
    return tuple(sorted(pairs))


def _parse_compiler_arguments(value: Any) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EvmError("compiler must be a table")
    unknown = sorted(set(value) - {"ise", "gobo"})
    if unknown:
        raise EvmError(f"unknown compiler adapter {unknown[0]!r}; known adapters: ise, gobo")
    result: dict[str, tuple[str, ...]] = {}
    for adapter, table in value.items():
        if not isinstance(table, Mapping):
            raise EvmError(f"compiler.{adapter} must be a table")
        _reject_unknown(table, {"arguments"}, f"compiler.{adapter}")
        result[adapter] = _string_list(table.get("arguments"), f"compiler.{adapter}.arguments")
    return result


def _safe_project_path(base: Path, raw: str, field: str) -> Path:
    path = (base / raw).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as error:
        raise EvmError(f"{field} must stay inside the project directory") from error
    return path


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise EvmError(f"unknown manifest field: {path}.{unknown[0]}")
