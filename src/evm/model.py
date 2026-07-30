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
class BuildRequest:
    compiler: str | None = None
    target: str = "default"
    release: bool = False
    regenerate_ecf: bool = False


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

    @property
    def directory(self) -> Path:
        return self.manifest_path.parent

    def target(self, name: str) -> Target:
        for target in self.targets:
            if target.name == name:
                return target
        raise KeyError(name)
