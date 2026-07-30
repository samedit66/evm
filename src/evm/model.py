"""Normalized project model used by manifests, ECF, and toolchains."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Root:
    class_name: str
    feature: str


@dataclass(frozen=True)
class Target:
    name: str
    root: Root | None
    sources: tuple[str, ...]
    extends: str | None = None


@dataclass(frozen=True)
class Condition:
    target: str
    when: tuple[tuple[str, str], ...]
    sources: tuple[str, ...] = ()
    external_objects: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompilerRequirement:
    adapter: str
    constraint: str | None


@dataclass(frozen=True)
class Dependency:
    name: str
    source: str
    version: str | None = None
    git: str | None = None
    requested_kind: str | None = None
    requested_value: str | None = None
    path: str | None = None
    library: str | None = None
    ecf: str | None = None
    subdir: str | None = None
    development: bool = False
    patched_path: str | None = None


@dataclass(frozen=True)
class BuildRequest:
    compiler: str | None = None
    target: str = "default"
    release: bool = False
    regenerate_ecf: bool = False
    offline: bool = False


@dataclass(frozen=True)
class Project:
    manifest_path: Path
    name: str
    version: str
    kind: str
    uuid: str
    ecf_path: Path
    ecf_managed: bool
    targets: tuple[Target, ...]
    conditions: tuple[Condition, ...] = ()
    compilers: tuple[CompilerRequirement, ...] = ()
    requires: tuple[tuple[str, str], ...] = ()
    compiler_arguments: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dependencies: tuple[Dependency, ...] = ()

    @property
    def directory(self) -> Path:
        return self.manifest_path.parent

    def target(self, name: str) -> Target:
        for target in self.targets:
            if target.name == name:
                return target
        raise KeyError(name)
