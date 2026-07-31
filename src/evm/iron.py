"""IRON package interoperability."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from evm.errors import EvmError
from evm.model import PackageLink, PackageMetadata, Project

IRON_PACKAGE_NAME = "package.iron"
_PROJECT_ENTRY = re.compile(r'^([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]+)"\s*$')
_NOTE_ENTRY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*(?:\[[^]]+\])?)\s*:\s*(.*)$")
_LINK_NAME = re.compile(r"^link\[([^]]+)\]$")
_KNOWN_NOTES = {"title", "description", "tags", "license", "copyright", "maps"}


@dataclass(frozen=True)
class IronProject:
    name: str
    ecf: str


@dataclass(frozen=True)
class IronPackage:
    name: str
    projects: tuple[IronProject, ...]
    metadata: PackageMetadata
    has_setup: bool = False
    unknown_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedNote:
    name: str
    value: str


def load_iron_package(path: Path) -> IronPackage:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise EvmError(f"IRON package file not found: {path}") from error
    except (OSError, UnicodeError) as error:
        raise EvmError(f"cannot read {path}: {error}") from error
    return parse_iron_package(content, path)


def parse_iron_package(content: str, path: Path) -> IronPackage:
    lines = content.splitlines()
    package_name: str | None = None
    projects: list[IronProject] = []
    notes: list[_ParsedNote] = []
    section: str | None = None
    has_setup = False
    index = 0
    while index < len(lines):
        line_number = index + 1
        stripped = lines[index].strip()
        index += 1
        if not stripped or stripped.startswith("--"):
            continue
        if stripped == "end":
            section = "end"
            continue
        if stripped in {"project", "note", "setup"}:
            section = stripped
            has_setup = has_setup or stripped == "setup"
            continue
        if stripped.startswith("package ") and package_name is None:
            package_name = stripped.removeprefix("package ").strip()
            _validate_package_name(package_name, path, line_number)
            continue
        if section == "project":
            projects.append(_parse_project_entry(stripped, path, line_number))
            continue
        if section == "note":
            note, index = _parse_note(lines, stripped, index, path, line_number)
            notes.append(note)
            continue
        if section == "setup":
            continue
        raise _syntax_error(path, line_number, f"unexpected content {stripped!r}")
    if package_name is None:
        raise EvmError(f"cannot parse {path}: missing package declaration")
    return _iron_package(package_name, projects, notes, has_setup)


def _validate_package_name(name: str, path: Path, line_number: int) -> None:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) is None:
        raise _syntax_error(path, line_number, f"invalid package name {name!r}")


def _parse_project_entry(line: str, path: Path, line_number: int) -> IronProject:
    match = _PROJECT_ENTRY.fullmatch(line)
    if match is None:
        raise _syntax_error(path, line_number, "invalid project declaration")
    return IronProject(match.group(1), match.group(2))


def _parse_note(
    lines: list[str],
    line: str,
    next_index: int,
    path: Path,
    line_number: int,
) -> tuple[_ParsedNote, int]:
    match = _NOTE_ENTRY.fullmatch(line)
    if match is None:
        raise _syntax_error(path, line_number, "invalid note declaration")
    name = match.group(1)
    raw_value = match.group(2).strip()
    if raw_value.startswith('"['):
        values = [raw_value[2:]]
        while next_index < len(lines):
            continuation = lines[next_index]
            next_index += 1
            if continuation.rstrip().endswith(']"'):
                values.append(continuation.rstrip()[:-2])
                return _ParsedNote(name, "\n".join(values).strip()), next_index
            values.append(continuation)
        raise _syntax_error(path, line_number, "unterminated multiline note")
    return _ParsedNote(name, raw_value), next_index


def _iron_package(
    name: str,
    projects: list[IronProject],
    notes: list[_ParsedNote],
    has_setup: bool,
) -> IronPackage:
    scalar_notes: dict[str, str] = {}
    links: list[PackageLink] = []
    unknown: list[str] = []
    for note in notes:
        link_match = _LINK_NAME.fullmatch(note.name)
        if link_match is not None:
            links.append(_parse_link(link_match.group(1), note.value))
        elif note.name in _KNOWN_NOTES:
            scalar_notes[note.name] = _parse_scalar(note.value)
        else:
            unknown.append(note.name)
    metadata = PackageMetadata(
        title=scalar_notes.get("title"),
        description=scalar_notes.get("description"),
        license=scalar_notes.get("license"),
        copyright=scalar_notes.get("copyright"),
        tags=_comma_separated(scalar_notes.get("tags")),
        links=tuple(links),
        iron_maps=_comma_separated(scalar_notes.get("maps")),
    )
    return IronPackage(name, tuple(projects), metadata, has_setup, tuple(unknown))


def _parse_scalar(value: str) -> str:
    if not value.startswith('"'):
        return value.strip()
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        return value.strip('"')
    return " ".join(parts)


def _parse_link(category: str, value: str) -> PackageLink:
    try:
        parts = shlex.split(value, posix=True)
    except ValueError as error:
        raise EvmError(f"invalid IRON link[{category}]: {error}") from error
    if len(parts) == 1:
        return PackageLink(category, parts[0])
    if len(parts) == 2:
        return PackageLink(category, parts[1], parts[0])
    raise EvmError(f"invalid IRON link[{category}]")


def _comma_separated(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def serialize_iron_package(project: Project) -> bytes:
    lines = [
        f"package {project.name}",
        "",
        "project",
        f'\t{project.name} = "{project.ecf_path.name}"',
    ]
    metadata = project.package
    if metadata is not None and _has_metadata(metadata):
        lines.extend(("", "note"))
        _append_note(lines, "title", metadata.title)
        _append_note(lines, "description", metadata.description)
        if metadata.tags:
            lines.append(f"\ttags: {','.join(metadata.tags)}")
        _append_note(lines, "license", metadata.license)
        _append_note(lines, "copyright", metadata.copyright)
        for link in metadata.links:
            title = f"{json.dumps(link.title)} " if link.title is not None else ""
            lines.append(f"\tlink[{link.category}]: {title}{json.dumps(link.url)}")
        if metadata.iron_maps:
            lines.append(f"\tmaps: {','.join(metadata.iron_maps)}")
    lines.extend(("", "end", ""))
    return "\n".join(lines).encode()


def _has_metadata(metadata: PackageMetadata) -> bool:
    return any(
        (
            metadata.title,
            metadata.description,
            metadata.license,
            metadata.copyright,
            metadata.tags,
            metadata.links,
            metadata.iron_maps,
        )
    )


def _append_note(lines: list[str], name: str, value: str | None) -> None:
    if value is None:
        return
    if "\n" in value:
        lines.extend((f'\t{name}: "[{value}', '\t\t\t\t]"'))
    else:
        lines.append(f"\t{name}: {json.dumps(value)}")


def iron_package_diagnostics(package: IronPackage, project: Project) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if package.name != project.name:
        diagnostics.append(
            f"{IRON_PACKAGE_NAME}: package name {package.name!r} does not match {project.name!r}"
        )
    aliases: set[str] = set()
    locations: set[Path] = set()
    for declared in package.projects:
        if declared.name in aliases:
            diagnostics.append(f"{IRON_PACKAGE_NAME}: duplicate project name {declared.name!r}")
        aliases.add(declared.name)
        location = _safe_ecf_path(project.directory, declared.ecf)
        if location is None:
            diagnostics.append(
                f"{IRON_PACKAGE_NAME}: project {declared.name!r} escapes the package root"
            )
            continue
        locations.add(location)
        if not location.is_file():
            diagnostics.append(f"{IRON_PACKAGE_NAME}: project ECF not found: {declared.ecf}")
    if project.ecf_path.resolve() not in locations:
        diagnostics.append(
            f"{IRON_PACKAGE_NAME}: current ECF is not declared: {project.ecf_path.name}"
        )
    return tuple(diagnostics)


def select_iron_project(package: IronPackage, name: str | None, root: Path) -> Path:
    if not package.projects:
        raise EvmError("package.iron declares no ECF projects")
    if name is None and len(package.projects) != 1:
        choices = ", ".join(project.name for project in package.projects)
        raise EvmError(
            f"package.iron declares multiple projects; use --project with one of: {choices}"
        )
    selected = next(
        (project for project in package.projects if project.name == name),
        package.projects[0] if name is None else None,
    )
    if selected is None:
        raise EvmError(f"unknown package.iron project {name!r}")
    path = _safe_ecf_path(root, selected.ecf)
    if path is None:
        raise EvmError(f"package.iron project {selected.name!r} escapes the package root")
    if not path.is_file():
        raise EvmError(f"package.iron project ECF not found: {selected.ecf}")
    return path


def _safe_ecf_path(root: Path, value: str) -> Path | None:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _syntax_error(path: Path, line_number: int, message: str) -> EvmError:
    return EvmError(f"cannot parse {path}:{line_number}: {message}")
