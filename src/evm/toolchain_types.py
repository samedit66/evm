"""Toolchain identities shared by catalog, storage, and compiler selection."""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from evm.errors import EvmError

KNOWN_PROVIDERS = ("ise", "gobo")
_SELECTOR_RE = re.compile(r"^(ise|gobo)(?:@([A-Za-z0-9][A-Za-z0-9._-]*))?$")


class InstallationKind(StrEnum):
    MANAGED = "managed"
    LINKED = "linked"


@dataclass(frozen=True)
class ToolchainSelector:
    provider: str
    version: str | None = None

    @classmethod
    def parse(cls, value: str) -> ToolchainSelector:
        match = _SELECTOR_RE.fullmatch(value.strip().lower())
        if match is None:
            raise EvmError(
                f"invalid toolchain selector {value!r}; known adapters: ise, gobo; "
                "expected provider or provider@version"
            )
        return cls(match.group(1), match.group(2))

    @property
    def requested_version(self) -> str:
        return self.version or "latest"

    def __str__(self) -> str:
        suffix = f"@{self.version}" if self.version else ""
        return f"{self.provider}{suffix}"


@dataclass(frozen=True)
class ToolchainPlatform:
    operating_system: str
    architecture: str
    archive_platform: str
    ise_platform: str

    @property
    def identifier(self) -> str:
        return f"{self.operating_system}-{self.architecture}"


@dataclass(frozen=True)
class ToolchainArtifact:
    provider: str
    version: str
    revision: str
    platform: ToolchainPlatform
    url: str
    filename: str
    checksum: str | None = None
    channel: str = "stable"

    @property
    def identity(self) -> str:
        return f"{self.provider}@{self.revision}"


@dataclass(frozen=True)
class ToolchainInstallation:
    provider: str
    version: str
    revision: str
    platform: ToolchainPlatform
    root: Path
    executable: Path
    kind: InstallationKind
    checksum: str | None = None
    source: str | None = None

    @property
    def selector(self) -> str:
        return f"{self.provider}@{self.version}"

    @property
    def identity(self) -> str:
        return f"{self.provider}@{self.revision}"

    def matches(self, selector: ToolchainSelector) -> bool:
        if self.provider != selector.provider:
            return False
        return selector.version in {None, "latest", self.version, self.revision}


def current_toolchain_platform(
    system: str | None = None,
    machine: str | None = None,
) -> ToolchainPlatform:
    system_name = (system or platform.system()).lower()
    machine_name = (machine or platform.machine()).lower()
    architecture = _normalized_architecture(machine_name)
    match (system_name, architecture):
        case ("linux", "x86_64"):
            return ToolchainPlatform("linux", architecture, "linux", "linux-x86-64")
        case ("linux", "arm64"):
            return ToolchainPlatform("linux", architecture, "linux", "linux-arm64")
        case ("darwin", "x86_64"):
            return ToolchainPlatform("macos", architecture, "macos", "macosx-x86-64")
        case ("darwin", "arm64"):
            return ToolchainPlatform("macos", architecture, "macos", "macosx-armv6")
        case ("windows", "x86_64"):
            return ToolchainPlatform("windows", architecture, "windows", "win64")
        case ("windows", "arm64"):
            return ToolchainPlatform("windows", architecture, "windows", "win64")
        case _:
            raise EvmError(
                f"unsupported toolchain platform: system={system_name}, architecture={machine_name}"
            )


def executable_name(provider: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    return ("ec" if provider == "ise" else "gec") + suffix


def _normalized_architecture(machine: str) -> str:
    if machine in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if machine in {"aarch64", "arm64", "armv8"}:
        return "arm64"
    raise EvmError(f"unsupported toolchain architecture: {machine}")
