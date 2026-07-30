"""Deterministic Eiffel.lock parsing and serialization."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import tomlkit

from evm.errors import EvmError
from evm.model import Dependency, Project

LOCK_NAME = "Eiffel.lock"
LOCK_FORMAT_VERSION = 1


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    source: str
    dependencies: tuple[str, ...] = ()
    checksum: str | None = None
    requested: str | None = None
    revision: str | None = None
    tree: str | None = None
    manifest: str | None = None
    ecf: str | None = None
    subdir: str | None = None
    path: str | None = None
    path_kind: str | None = None
    distribution: str | None = None
    library: str | None = None
    archive: str | None = None
    development: bool = False
    patched: bool = False

    @property
    def materialized_name(self) -> str:
        identity = self.revision[:8] if self.revision else self.version
        source_hash = hashlib.sha256(self.source.encode()).hexdigest()[:6]
        safe_identity = "".join(character if character.isalnum() else "-" for character in identity)
        return f"{self.name}-{safe_identity}-{source_hash}"


@dataclass(frozen=True)
class LockFile:
    manifest_fingerprint: str
    packages: tuple[LockedPackage, ...]
    format_version: int = LOCK_FORMAT_VERSION

    def package(self, name: str) -> LockedPackage:
        for package in self.packages:
            if package.name == name:
                return package
        raise KeyError(name)


def manifest_fingerprint(project: Project) -> str:
    dependencies = [_dependency_identity(item) for item in project.dependencies]
    encoded = json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def empty_lock(project: Project) -> LockFile:
    return LockFile(manifest_fingerprint(project), ())


def load_lock(path: Path) -> LockFile:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvmError(f"lock file not found: {path}; run `evm update`") from error
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise EvmError(f"cannot parse {path}: {error}") from error
    if document.get("format-version") != LOCK_FORMAT_VERSION:
        raise EvmError(
            f"unsupported lock format {document.get('format-version')!r}; "
            f"expected {LOCK_FORMAT_VERSION}"
        )
    fingerprint = document.get("manifest-fingerprint", "")
    if not isinstance(fingerprint, str):
        raise EvmError("manifest-fingerprint in Eiffel.lock must be a string")
    raw_packages = document.get("package", [])
    if not isinstance(raw_packages, list):
        raise EvmError("package in Eiffel.lock must be an array of tables")
    packages = tuple(_parse_package(raw, index) for index, raw in enumerate(raw_packages))
    names = [package.name for package in packages]
    if len(names) != len(set(names)):
        raise EvmError("Eiffel.lock contains duplicate package names")
    return LockFile(fingerprint, packages)


def ensure_lock_matches(project: Project, lock: LockFile) -> None:
    expected = manifest_fingerprint(project)
    if lock.manifest_fingerprint != expected:
        raise EvmError(
            "Eiffel.toml and Eiffel.lock are inconsistent; "
            "run `evm update` or the corresponding `evm add`/`evm remove`"
        )


def serialize_lock(lock: LockFile) -> bytes:
    document = tomlkit.document()
    document.add("format-version", lock.format_version)
    document.add("manifest-fingerprint", lock.manifest_fingerprint)
    package_array = tomlkit.aot()
    for package in sorted(lock.packages, key=lambda item: item.name):
        table = tomlkit.table()
        for field in fields(package):
            value = getattr(package, field.name)
            if value in (None, (), False):
                continue
            rendered = list(value) if isinstance(value, tuple) else value
            table.add(field.name.replace("_", "-"), rendered)
        package_array.append(table)
    document.add("package", package_array)
    return tomlkit.dumps(document).encode()


def _parse_package(raw: Any, index: int) -> LockedPackage:
    if not isinstance(raw, dict):
        raise EvmError(f"package[{index}] in Eiffel.lock must be a table")
    known = {field.name.replace("_", "-") for field in fields(LockedPackage)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise EvmError(f"unknown Eiffel.lock field: package[{index}].{unknown[0]}")
    required: dict[str, str] = {}
    for name in ("name", "version", "source"):
        value = raw.get(name)
        if not isinstance(value, str) or not value:
            raise EvmError(f"package[{index}].{name} must be a non-empty string")
        required[name] = value
    dependencies = raw.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and item for item in dependencies
    ):
        raise EvmError(f"package[{index}].dependencies must be an array of strings")
    optional: dict[str, Any] = {}
    for field in fields(LockedPackage):
        key = field.name.replace("_", "-")
        if field.name in required or field.name == "dependencies" or key not in raw:
            continue
        value = raw[key]
        if field.name in {"development", "patched"}:
            if not isinstance(value, bool):
                raise EvmError(f"package[{index}].{key} must be a boolean")
        elif not isinstance(value, str) or not value:
            raise EvmError(f"package[{index}].{key} must be a non-empty string")
        optional[field.name] = value
    return LockedPackage(
        **required,
        dependencies=tuple(dependencies),
        **optional,
    )


def _dependency_identity(dependency: Dependency) -> dict[str, object]:
    return {
        field.name: getattr(dependency, field.name)
        for field in fields(dependency)
        if getattr(dependency, field.name) is not None
    }
