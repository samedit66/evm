"""Deterministic Eiffel.lock parsing and serialization."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import tomlkit

from evm.errors import EvmError
from evm.model import Dependency, Project

LOCK_NAME = "Eiffel.lock"
LOCK_FORMAT_VERSION = 2
_SUPPORTED_LOCK_FORMATS = {1, LOCK_FORMAT_VERSION}


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
class LockedToolchain:
    provider: str
    version: str
    revision: str
    platform: str
    architecture: str
    source: str
    checksum: str | None = None


@dataclass(frozen=True)
class LockFile:
    manifest_fingerprint: str
    packages: tuple[LockedPackage, ...]
    toolchains: tuple[LockedToolchain, ...] = ()
    format_version: int = LOCK_FORMAT_VERSION

    def package(self, name: str) -> LockedPackage:
        for package in self.packages:
            if package.name == name:
                return package
        raise KeyError(name)


def manifest_fingerprint(project: Project) -> str:
    dependencies = [_dependency_identity(item) for item in project.dependencies]
    toolchain = asdict(project.toolchain) if project.toolchain is not None else None
    return _fingerprint({"dependencies": dependencies, "toolchain": toolchain})


def _legacy_manifest_fingerprint(project: Project) -> str:
    dependencies = [_dependency_identity(item) for item in project.dependencies]
    return _fingerprint(dependencies)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
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
    source_format = document.get("format-version")
    if source_format not in _SUPPORTED_LOCK_FORMATS:
        raise EvmError(
            f"unsupported lock format {source_format!r}; expected 1 or {LOCK_FORMAT_VERSION}"
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
    raw_toolchains = document.get("toolchain", [])
    if not isinstance(raw_toolchains, list):
        raise EvmError("toolchain in Eiffel.lock must be an array of tables")
    toolchains = tuple(_parse_toolchain(raw, index) for index, raw in enumerate(raw_toolchains))
    identities = [
        (item.provider, item.revision, item.platform, item.architecture) for item in toolchains
    ]
    if len(identities) != len(set(identities)):
        raise EvmError("Eiffel.lock contains duplicate toolchain artifacts")
    return LockFile(fingerprint, packages, toolchains, source_format)


def ensure_lock_matches(project: Project, lock: LockFile) -> None:
    expected = (
        _legacy_manifest_fingerprint(project)
        if lock.format_version == 1 and project.toolchain is None
        else manifest_fingerprint(project)
    )
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
        for name, value in asdict(package).items():
            if value in (None, (), False):
                continue
            rendered = list(value) if isinstance(value, tuple) else value
            table.add(name.replace("_", "-"), rendered)
        package_array.append(table)
    document.add("package", package_array)
    toolchain_array = tomlkit.aot()
    for toolchain in sorted(
        lock.toolchains,
        key=lambda item: (item.provider, item.revision, item.platform, item.architecture),
    ):
        table = tomlkit.table()
        for name, value in asdict(toolchain).items():
            if value is not None:
                table.add(name.replace("_", "-"), value)
        toolchain_array.append(table)
    if toolchain_array:
        document.add("toolchain", toolchain_array)
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
    return {name: value for name, value in asdict(dependency).items() if value is not None}


def _parse_toolchain(raw: Any, index: int) -> LockedToolchain:
    if not isinstance(raw, dict):
        raise EvmError(f"toolchain[{index}] in Eiffel.lock must be a table")
    known = {field.name.replace("_", "-") for field in fields(LockedToolchain)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise EvmError(f"unknown Eiffel.lock field: toolchain[{index}].{unknown[0]}")
    values: dict[str, str | None] = {}
    for field in fields(LockedToolchain):
        key = field.name.replace("_", "-")
        value = raw.get(key)
        if value is None and field.name == "checksum":
            values[field.name] = None
            continue
        if not isinstance(value, str) or not value:
            raise EvmError(f"toolchain[{index}].{key} must be a non-empty string")
        values[field.name] = value
    return LockedToolchain(**values)
