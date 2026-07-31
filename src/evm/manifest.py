"""Parsing and validation for Eiffel.toml."""

from __future__ import annotations

import dataclasses
import re
import shlex
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from evm.dependency_validation import ensure_dependency_is_not_implicit_runtime
from evm.errors import EvmError
from evm.model import (
    CompilerRequirement,
    Condition,
    Dependency,
    Project,
    Root,
    Target,
    Task,
    TaskStep,
    TestConfiguration,
)
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
    "dependencies",
    "dev-dependencies",
    "patch",
    "ecf",
    "test",
    "scripts",
    "workspace",
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
    test = _parse_test(document.get("test"), targets)
    if test is None and (path.parent / "tests").is_dir():
        has_test_sources = any((path.parent / "tests").rglob("*.e"))
        if has_test_sources and not any(target.name == "test" for target in targets):
            targets.append(
                Target(
                    "test",
                    Root("TEST_APPLICATION", "make"),
                    ("tests",),
                    "default",
                )
            )
            test = TestConfiguration("test")
    _validate_target_graph(targets)

    compilers = _parse_compilers(document.get("compatibility"))
    requires = _parse_requires(document.get("requires"))
    conditions = _parse_conditions(document.get("conditions"), {target.name for target in targets})
    compiler_arguments = _parse_compiler_arguments(document.get("compiler"))
    dependencies = _parse_dependencies(document.get("dependencies"), development=False)
    dependencies += _parse_dependencies(document.get("dev-dependencies"), development=True)
    dependency_names = [dependency.name for dependency in dependencies]
    if len(dependency_names) != len(set(dependency_names)):
        raise EvmError("a dependency cannot appear in both dependencies and dev-dependencies")
    dependencies = _apply_patches(dependencies, document.get("patch"))
    ecf_includes = _parse_ecf_includes(document.get("ecf"), path)
    tasks = _parse_tasks(document.get("scripts"))
    _validate_task_references(tasks)
    _parse_workspace_members(document.get("workspace"), path)
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
        dependencies=dependencies,
        ecf_includes=ecf_includes,
        test=test,
        tasks=tasks,
    )


def _parse_test(value: Any, targets: list[Target]) -> TestConfiguration | None:
    target_names = {target.name for target in targets}
    if value is None:
        return TestConfiguration("test") if "test" in target_names else None
    if not isinstance(value, Mapping):
        raise EvmError("test must be a table")
    _reject_unknown(value, {"target"}, "test")
    target = _required_string(value, "target", "test.target")
    if target not in target_names:
        raise EvmError(f"test.target refers to unknown target {target!r}")
    return TestConfiguration(target)


def _parse_tasks(value: Any) -> tuple[Task, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise EvmError("scripts must be a table")
    tasks: list[Task] = []
    for name, raw in value.items():
        if _NAME_RE.fullmatch(name) is None:
            raise EvmError(f"scripts.{name} is not a valid task name")
        if isinstance(raw, str):
            arguments = _short_task_arguments(raw, f"scripts.{name}")
            tasks.append(Task(name, (TaskStep(command=arguments[0], arguments=arguments[1:]),)))
            continue
        if not isinstance(raw, Mapping):
            raise EvmError(f"scripts.{name} must be a command string or table")
        _reject_unknown(raw, {"steps"}, f"scripts.{name}")
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps:
            raise EvmError(f"scripts.{name}.steps must be a non-empty array")
        tasks.append(
            Task(
                name,
                tuple(
                    _parse_task_step(step, f"scripts.{name}.steps[{index}]")
                    for index, step in enumerate(steps)
                ),
            )
        )
    return tuple(tasks)


def _short_task_arguments(value: str, path: str) -> tuple[str, ...]:
    try:
        arguments = tuple(shlex.split(value, posix=True))
    except ValueError as error:
        raise EvmError(f"{path} is not a valid command: {error}") from error
    if not arguments:
        raise EvmError(f"{path} must contain one EVM command")
    if any(token in value for token in ("&&", "||", "|", ">", "<", ";", "$(", "`")) or re.search(
        r"\$[A-Za-z_{]", value
    ):
        raise EvmError(f"{path} must contain one EVM command without shell operators")
    return arguments


def _parse_task_step(value: Any, path: str) -> TaskStep:
    if not isinstance(value, Mapping):
        raise EvmError(f"{path} must be a table")
    command = value.get("command")
    shell = value.get("shell")
    if (command is None) == (shell is None):
        raise EvmError(f"{path} requires exactly one of command or shell")
    if shell is not None:
        _reject_unknown(value, {"shell"}, path)
        return TaskStep(shell=_non_empty_string(shell, f"{path}.shell"))
    allowed_options = {
        "command",
        "release",
        "compiler",
        "target",
        "offline",
        "configuration-only",
        "package",
    }
    _reject_unknown(value, allowed_options, path)
    arguments: list[str] = []
    for key, option in value.items():
        if key == "command":
            continue
        option_name = f"--{key}"
        if isinstance(option, bool):
            if option:
                arguments.append(option_name)
        elif isinstance(option, str) and option:
            arguments.extend((option_name, option))
        else:
            raise EvmError(f"{path}.{key} must be a string or boolean")
    return TaskStep(
        command=_non_empty_string(command, f"{path}.command"),
        arguments=tuple(arguments),
    )


def _validate_task_references(tasks: tuple[Task, ...]) -> None:
    names = {task.name for task in tasks}
    for task in tasks:
        for step in task.steps:
            if step.command == "task" and (not step.arguments or step.arguments[0] not in names):
                raise EvmError(f"task {task.name!r} refers to an unknown task")


def _parse_workspace_members(value: Any, path: Path) -> tuple[Path, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise EvmError("workspace must be a table")
    _reject_unknown(value, {"members"}, "workspace")
    members = _string_list(value.get("members"), "workspace.members", required=True)
    resolved: list[Path] = []
    for index, member in enumerate(members):
        member_path = _safe_project_path(path.parent, member, f"workspace.members[{index}]")
        if not (member_path / MANIFEST_NAME).is_file():
            raise EvmError(f"workspace member has no {MANIFEST_NAME}: {member}")
        resolved.append(member_path)
    if len(resolved) != len(set(resolved)):
        raise EvmError("workspace.members contains duplicate paths")
    return tuple(resolved)


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
        if name == "default":
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


def _parse_ecf_includes(value: Any, manifest_path: Path) -> tuple[Path, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise EvmError("ecf must be a table")
    _reject_unknown(value, {"include"}, "ecf")
    includes = _string_list(value.get("include"), "ecf.include", required=True)
    return tuple(
        _safe_project_path(manifest_path.parent, include, f"ecf.include[{index}]")
        for index, include in enumerate(includes)
    )


def _parse_dependencies(value: Any, *, development: bool) -> tuple[Dependency, ...]:
    if value is None:
        return ()
    section = "dev-dependencies" if development else "dependencies"
    if not isinstance(value, Mapping):
        raise EvmError(f"{section} must be a table")
    result: list[Dependency] = []
    for name, raw in value.items():
        if _NAME_RE.fullmatch(name) is None:
            raise EvmError(f"{section}.{name} has an invalid dependency name")
        result.append(_parse_dependency(name, raw, section, development))
    return tuple(result)


def _parse_dependency(
    name: str,
    raw: Any,
    section: str,
    development: bool,
) -> Dependency:
    path = f"{section}.{name}"
    if isinstance(raw, str):
        return Dependency(name=name, source="iron", version=raw, development=development)
    if not isinstance(raw, Mapping):
        raise EvmError(f"{path} must be a version string or inline table")
    allowed = {
        "source",
        "version",
        "git",
        "tag",
        "rev",
        "branch",
        "path",
        "library",
        "ecf",
        "subdir",
    }
    _reject_unknown(raw, allowed, path)
    source = _dependency_source(raw, path)
    revision_fields = [key for key in ("tag", "rev", "branch") if key in raw]
    if source == "git" and len(revision_fields) != 1:
        raise EvmError(f"{path} must define exactly one of tag, rev, or branch")
    if source != "git" and revision_fields:
        raise EvmError(f"{path} revision constraints are only valid for Git dependencies")
    requested_kind = revision_fields[0] if revision_fields else None
    requested_value = (
        _non_empty_string(raw[requested_kind], f"{path}.{requested_kind}")
        if requested_kind
        else None
    )
    version = _optional_string(raw.get("version"), f"{path}.version")
    git = _optional_string(raw.get("git"), f"{path}.git")
    dependency_path = _optional_string(raw.get("path"), f"{path}.path")
    library = _optional_string(raw.get("library"), f"{path}.library")
    ecf = _safe_relative_dependency_path(raw.get("ecf"), f"{path}.ecf")
    subdir = _safe_relative_dependency_path(raw.get("subdir"), f"{path}.subdir")
    if source == "git" and git is None:
        raise EvmError(f"{path}.git is required")
    if source == "path" and dependency_path is None:
        raise EvmError(f"{path}.path is required")
    if dependency_path is not None and Path(dependency_path).is_absolute():
        raise EvmError(f"{path}.path must be relative to Eiffel.toml")
    if source == "gobo" and library is None:
        raise EvmError(f"{path}.library is required")
    if source == "iron" and version is None:
        raise EvmError(f"{path}.version is required")
    dependency = Dependency(
        name=name,
        source=source,
        version=version,
        git=git,
        requested_kind=requested_kind,
        requested_value=requested_value,
        path=dependency_path,
        library=library,
        ecf=ecf,
        subdir=subdir,
        development=development,
    )
    ensure_dependency_is_not_implicit_runtime(dependency)
    return dependency


def _dependency_source(raw: Mapping[str, Any], path: str) -> str:
    inferred = [name for name in ("git", "path") if name in raw]
    explicit = raw.get("source")
    if explicit is not None:
        explicit = _non_empty_string(explicit, f"{path}.source")
    if len(inferred) > 1 or (explicit is not None and inferred and explicit != inferred[0]):
        raise EvmError(f"{path} defines conflicting dependency sources")
    source = explicit or (inferred[0] if inferred else "iron")
    if source not in {"ise", "gobo", "iron", "git", "path"}:
        raise EvmError(f"{path}.source must be one of: ise, gobo, iron, git, path")
    return source


def _apply_patches(
    dependencies: tuple[Dependency, ...],
    value: Any,
) -> tuple[Dependency, ...]:
    if value is None:
        return dependencies
    if not isinstance(value, Mapping):
        raise EvmError("patch must be a table")
    by_name = {dependency.name: dependency for dependency in dependencies}
    for name, raw in value.items():
        if name not in by_name:
            raise EvmError(f"patch.{name} does not match a declared dependency")
        if not isinstance(raw, Mapping):
            raise EvmError(f"patch.{name} must be an inline table")
        _reject_unknown(raw, {"path"}, f"patch.{name}")
        patched_path = _non_empty_string(raw.get("path"), f"patch.{name}.path")
        if Path(patched_path).is_absolute():
            raise EvmError(f"patch.{name}.path must be relative to Eiffel.toml")
        by_name[name] = dataclasses.replace(by_name[name], patched_path=patched_path)
    return tuple(by_name[dependency.name] for dependency in dependencies)


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, path)


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvmError(f"{path} must be a non-empty string")
    return value


def _safe_relative_dependency_path(value: Any, path: str) -> str | None:
    raw = _optional_string(value, path)
    if raw is None:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvmError(f"{path} must stay inside the dependency")
    return candidate.as_posix()


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
