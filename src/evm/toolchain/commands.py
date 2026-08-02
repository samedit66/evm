"""Transactional project operations for configured toolchains."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import tomlkit
from tomlkit.exceptions import TOMLKitError

from evm.errors import EvmError
from evm.filesystem import atomic_write, atomic_write_many
from evm.lockfile import (
    LOCK_NAME,
    LockedToolchain,
    LockFile,
    empty_lock,
    load_lock,
    manifest_fingerprint,
    serialize_lock,
)
from evm.manifest import parse_manifest
from evm.model import Project
from evm.toolchain.installation import resolve_artifact
from evm.toolchain.types import (
    ToolchainArtifact,
    ToolchainInstallation,
    ToolchainSelector,
    current_toolchain_platform,
)


def configure_project_toolchains(
    project: Project,
    selectors: tuple[ToolchainSelector, ...],
    client: httpx.Client | None = None,
) -> tuple[Project, LockFile]:
    if not selectors:
        raise EvmError("at least one toolchain selector is required")
    artifacts = tuple(resolve_artifact(selector, client) for selector in selectors)
    exact_selectors = tuple(
        f"{item.provider}@{item.revision if item.provider == 'serpent' else item.version}"
        for item in artifacts
    )
    if len(exact_selectors) != len(set(exact_selectors)):
        raise EvmError("toolchain matrix resolves to duplicate releases")
    document = _load_manifest_document(project.manifest_path)
    table = tomlkit.table()
    table.add("default", exact_selectors[0])
    table.add("matrix", list(exact_selectors))
    document["toolchain"] = table
    manifest = tomlkit.dumps(document).encode()
    proposed = parse_manifest(manifest.decode(), project.manifest_path)
    previous = _load_or_create_lock(project)
    locked_toolchains = tuple(_locked_toolchain(item) for item in artifacts)
    lock = LockFile(manifest_fingerprint(proposed), previous.packages, locked_toolchains)
    atomic_write_many(
        {
            proposed.manifest_path: manifest,
            proposed.directory / LOCK_NAME: serialize_lock(lock),
        }
    )
    return proposed, lock


def project_toolchain_selectors(project: Project) -> tuple[ToolchainSelector, ...]:
    if project.toolchain is None:
        raise EvmError(
            "project does not configure a toolchain matrix; run `evm toolchain use PROVIDER`"
        )
    return tuple(ToolchainSelector.parse(item) for item in project.toolchain.matrix)


def locked_toolchains_for_current_platform(lock: LockFile) -> tuple[LockedToolchain, ...]:
    platform = current_toolchain_platform()
    return tuple(
        item
        for item in lock.toolchains
        if item.platform == platform.operating_system and item.architecture == platform.architecture
    )


def record_installed_toolchains(
    project: Project,
    installations: tuple[ToolchainInstallation, ...],
) -> LockFile:
    lock = load_lock(project.directory / LOCK_NAME)
    updated = tuple(_with_installed_checksum(item, installations) for item in lock.toolchains)
    result = replace(lock, toolchains=updated)
    atomic_write(project.directory / LOCK_NAME, serialize_lock(result))
    return result


def _with_installed_checksum(
    locked: LockedToolchain,
    installations: tuple[ToolchainInstallation, ...],
) -> LockedToolchain:
    installation = next(
        (
            item
            for item in installations
            if item.provider == locked.provider
            and item.revision == locked.revision
            and item.platform.operating_system == locked.platform
            and item.platform.architecture == locked.architecture
        ),
        None,
    )
    if installation is None or installation.checksum is None:
        return locked
    return replace(locked, checksum=installation.checksum)


def _locked_toolchain(artifact: ToolchainArtifact) -> LockedToolchain:
    return LockedToolchain(
        artifact.provider,
        artifact.version,
        artifact.revision,
        artifact.platform.operating_system,
        artifact.platform.architecture,
        artifact.url,
        artifact.checksum,
    )


def _load_or_create_lock(project: Project) -> LockFile:
    path = project.directory / LOCK_NAME
    if path.is_file():
        return load_lock(path)
    return empty_lock(project)


def _load_manifest_document(path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TOMLKitError) as error:
        raise EvmError(f"cannot edit {path}: {error}") from error
