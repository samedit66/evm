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
class TestConfiguration:
    target: str


@dataclass(frozen=True)
class TaskStep:
    command: str | None = None
    shell: str | None = None
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    name: str
    steps: tuple[TaskStep, ...]


@dataclass(frozen=True)
class PackageLink:
    category: str
    url: str
    title: str | None = None


@dataclass(frozen=True)
class PackageMetadata:
    title: str | None = None
    description: str | None = None
    license: str | None = None
    copyright: str | None = None
    tags: tuple[str, ...] = ()
    links: tuple[PackageLink, ...] = ()
    iron_maps: tuple[str, ...] = ()


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
    ecf_includes: tuple[Path, ...] = ()
    test: TestConfiguration | None = None
    tasks: tuple[Task, ...] = ()
    package: PackageMetadata | None = None
    workspace_root: Path | None = None
    build_root: Path | None = None
    configuration_root: Path | None = None
    state_root: Path | None = None

    @property
    def directory(self) -> Path:
        return self.manifest_path.parent

    @property
    def configuration_directory(self) -> Path:
        return self.configuration_root or self.directory

    @property
    def state_directory(self) -> Path:
        return self.state_root or (self.workspace_root or self.directory) / ".evm"

    def target(self, name: str) -> Target:
        for target in self.targets:
            if target.name == name:
                return target
        raise KeyError(name)
