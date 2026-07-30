"""Dependency resolution, verification, and project-local materialization."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import tomlkit
from filelock import FileLock
from lxml import etree

from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.lockfile import (
    LOCK_NAME,
    LockedPackage,
    LockFile,
    ensure_lock_matches,
    load_lock,
    manifest_fingerprint,
)
from evm.manifest import load_manifest
from evm.model import Dependency, Project
from evm.toolchains import detect

_GIT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class _ResolutionState:
    project: Project
    resolved: dict[str, LockedPackage]
    resolving: list[str]
    offline: bool
    previous: LockFile | None


@dataclass(frozen=True)
class _GitFetchRequest:
    requested_kind: str | None = None
    requested_value: str | None = None
    locked_revision: str | None = None


@dataclass(frozen=True)
class _TreeRenderer:
    lines: list[str]
    packages: dict[str, LockedPackage]


def resolve_dependencies(
    project: Project,
    *,
    offline: bool = False,
    names: set[str] | None = None,
    previous: LockFile | None = None,
) -> LockFile:
    resolved: dict[str, LockedPackage] = {}
    resolving: list[str] = []
    state = _ResolutionState(project, resolved, resolving, offline, previous)
    selected = [
        dependency
        for dependency in project.dependencies
        if names is None or dependency.name in names
    ]
    if names is not None:
        missing = names - {dependency.name for dependency in selected}
        if missing:
            raise EvmError(f"unknown dependency: {sorted(missing)[0]}")
    for dependency in selected:
        _resolve_dependency(dependency, state)
    if names is not None and previous is not None:
        selected_names = {dependency.name for dependency in selected}
        for dependency in project.dependencies:
            if dependency.name in selected_names:
                continue
            try:
                package = previous.package(dependency.name)
            except KeyError:
                _resolve_dependency(dependency, state)
            else:
                _copy_locked_closure(package.name, previous, resolved)
    return LockFile(manifest_fingerprint(project), tuple(sorted(resolved.values(), key=_by_name)))


def install_dependencies(
    project: Project,
    *,
    offline: bool = False,
    lock: LockFile | None = None,
) -> LockFile:
    selected_lock = lock or load_lock(project.directory / LOCK_NAME)
    ensure_lock_matches(project, selected_lock)
    evm_directory = project.directory / ".evm"
    lock_directory = evm_directory / "locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_directory / "install.lock"):
        for package in selected_lock.packages:
            _install_package(project, selected_lock, package, offline=offline)
        _write_state(project, selected_lock)
    return selected_lock


def dependency_library_locations(
    project: Project,
    lock: LockFile,
    *,
    include_development: bool,
) -> tuple[tuple[str, str], ...]:
    locations: list[tuple[str, str]] = []
    for package in lock.packages:
        if package.development and not include_development:
            continue
        if package.ecf is None:
            continue
        if package.path is not None:
            package_root = (project.directory / package.path).resolve()
            location = os.path.relpath(package_root / package.ecf, project.directory)
        else:
            location = f".evm/deps/{package.materialized_name}/{package.ecf}"
        locations.append((package.name, Path(location).as_posix()))
    return tuple(locations)


def clean_unused(project: Project, lock: LockFile) -> tuple[int, int]:
    deps_directory = project.directory / ".evm" / "deps"
    sources_directory = project.directory / ".evm" / "sources" / "git"
    used_deps = {package.materialized_name for package in lock.packages if package.path is None}
    used_sources = {
        _source_key(package.source.removeprefix("git+"))
        for package in lock.packages
        if package.source.startswith("git+")
    }
    removed_deps = _remove_unlisted_directories(deps_directory, used_deps)
    removed_sources = _remove_unlisted_directories(sources_directory, used_sources)
    archives_directory = project.directory / ".evm" / "sources" / "archives"
    used_archives = {
        f"{package.checksum.removeprefix('sha256:')}.tar.bz2"
        for package in lock.packages
        if package.archive is not None and package.checksum is not None
    }
    removed_sources += _remove_unlisted_files(archives_directory, used_archives)
    return removed_deps, removed_sources


def dependency_tree_lines(
    project: Project,
    lock: LockFile,
    *,
    focus: str | None = None,
) -> list[str]:
    packages = {package.name: package for package in lock.packages}
    direct = [dependency.name for dependency in project.dependencies]
    if focus is not None:
        if focus not in packages:
            raise EvmError(f"dependency {focus!r} is not present in Eiffel.lock")
        paths = _dependency_paths(direct, focus, packages)
        lines = [f"{project.name} {project.version}"]
        for path in paths:
            chain = " -> ".join(path)
            package = packages[focus]
            reason = package.requested or package.version
            lines.append(f"{chain} ({reason})")
        return lines
    lines = [f"{project.name} {project.version}"]
    renderer = _TreeRenderer(lines, packages)
    for index, name in enumerate(direct):
        _append_tree(renderer, name, "", index == len(direct) - 1, set())
    return lines


def _resolve_dependency(
    dependency: Dependency,
    state: _ResolutionState,
) -> None:
    if dependency.name in state.resolving:
        cycle = " -> ".join((*state.resolving, dependency.name))
        raise EvmError(f"dependency cycle detected: {cycle}")
    state.resolving.append(dependency.name)
    package, nested_project, discovered = _resolve_one(
        state.project,
        dependency,
        offline=state.offline,
        previous=state.previous,
    )
    existing = state.resolved.get(package.name)
    if existing is not None and _package_identity(existing) != _package_identity(package):
        raise EvmError(f"dependency conflict for {package.name}: incompatible sources or versions")
    if existing is not None:
        package = replace(
            package,
            development=existing.development and package.development,
        )
    nested_dependencies = list(discovered)
    if nested_project is not None:
        nested_dependencies.extend(nested_project.dependencies)
    nested_names: list[str] = []
    owner = nested_project or state.project
    for nested_dependency in nested_dependencies:
        _resolve_dependency(nested_dependency, replace(state, project=owner))
        nested_names.append(nested_dependency.name)
    state.resolved[package.name] = replace(
        package,
        dependencies=tuple(sorted(nested_names)),
    )
    state.resolving.pop()


def _resolve_one(
    project: Project,
    dependency: Dependency,
    *,
    offline: bool,
    previous: LockFile | None,
) -> tuple[LockedPackage, Project | None, tuple[Dependency, ...]]:
    if dependency.patched_path is not None:
        package, nested, discovered = _resolve_path(
            project,
            dependency,
            dependency.patched_path,
        )
        return (
            replace(
                package,
                source=_declared_source(dependency),
                patched=True,
            ),
            nested,
            discovered,
        )
    if dependency.source == "path":
        if dependency.path is None:
            raise AssertionError("validated path dependency has no path")
        return _resolve_path(project, dependency, dependency.path)
    if dependency.source == "git":
        return _resolve_git(
            project,
            dependency,
            offline=offline,
            previous=previous,
        )
    if dependency.source == "ise":
        package, nested = _resolve_distribution(project, dependency, "ise")
        return package, nested, ()
    if dependency.source == "gobo":
        package, nested = _resolve_distribution(project, dependency, "gobo")
        return package, nested, ()
    return _resolve_iron(project, dependency, offline=offline, previous=previous)


def _resolve_path(
    project: Project,
    dependency: Dependency,
    raw_path: str,
) -> tuple[LockedPackage, Project | None, tuple[Dependency, ...]]:
    root = (project.directory / raw_path).resolve()
    if not root.is_dir():
        raise EvmError(f"path dependency {dependency.name} does not exist: {raw_path}")
    nested = _load_nested_manifest(root)
    ecf = _dependency_ecf(root, dependency.ecf, nested)
    version = nested.version if nested is not None else dependency.version or "0.0.0"
    package = LockedPackage(
        name=dependency.name,
        version=version,
        source=f"path+{raw_path}",
        checksum=f"sha256:{_directory_hash(root)}",
        manifest="Eiffel.toml" if nested is not None else None,
        ecf=ecf,
        path=raw_path,
        path_kind="external",
        development=dependency.development,
    )
    discovered = _ecf_iron_dependencies(root, ecf, dependency)
    if discovered:
        raise EvmError(
            f"path dependency {dependency.name} uses IRON URIs in {ecf}; "
            "replace them with local ECF paths for live path mode"
        )
    return package, nested, discovered


def _resolve_git(
    project: Project,
    dependency: Dependency,
    *,
    offline: bool,
    previous: LockFile | None,
) -> tuple[LockedPackage, Project | None, tuple[Dependency, ...]]:
    if dependency.git is None or dependency.requested_kind is None:
        raise AssertionError("validated Git dependency is incomplete")
    repository = _git_repository(
        project,
        dependency.git,
        offline=offline,
        request=_GitFetchRequest(
            requested_kind=dependency.requested_kind,
            requested_value=dependency.requested_value,
        ),
    )
    requested = f"{dependency.requested_kind}:{dependency.requested_value}"
    revision = _git_revision(repository, dependency)
    if previous is not None and dependency.requested_kind in {"tag", "rev"}:
        try:
            locked = previous.package(dependency.name)
        except KeyError:
            pass
        else:
            if locked.requested == requested:
                revision = locked.revision or revision
    checkout = _git_checkout(repository, revision)
    package_root = _inside(checkout, dependency.subdir or ".", f"{dependency.name}.subdir")
    nested = _load_nested_manifest(package_root)
    ecf = _dependency_ecf(package_root, dependency.ecf, nested)
    version = nested.version if nested is not None else dependency.version or "0.0.0"
    tree_path = f"{revision}:{dependency.subdir}" if dependency.subdir else f"{revision}^{{tree}}"
    tree = _git_output(repository, "rev-parse", tree_path)
    package = LockedPackage(
        name=dependency.name,
        version=version,
        source=f"git+{dependency.git}",
        requested=requested,
        revision=revision,
        tree=tree,
        manifest="Eiffel.toml" if nested is not None else None,
        ecf=ecf,
        subdir=dependency.subdir,
        development=dependency.development,
    )
    discovered = _ecf_iron_dependencies(package_root, ecf, dependency)
    return package, nested, discovered


def _resolve_distribution(
    project: Project,
    dependency: Dependency,
    adapter: str,
) -> tuple[LockedPackage, None]:
    detected = detect(adapter)
    if detected.version is None:
        raise EvmError(f"cannot resolve {dependency.name}: {detected.error}")
    root = _distribution_root(adapter)
    library = dependency.library or dependency.name
    package_root, ecf = _find_distribution_library(root, library, dependency.ecf)
    source = "eiffelstudio" if adapter == "ise" else "gobo-distribution"
    locked_ecf = ecf if adapter == "ise" else f"library/{library}/{ecf}"
    checksum_root = package_root if adapter == "ise" else root
    return (
        LockedPackage(
            name=dependency.name,
            version=str(detected.version),
            source=source,
            ecf=locked_ecf,
            path_kind=f"{adapter}-library",
            distribution="gobo" if adapter == "gobo" else None,
            library=library,
            development=dependency.development,
            checksum=f"sha256:{_directory_hash(checksum_root)}",
        ),
        None,
    )


def _resolve_iron(
    project: Project,
    dependency: Dependency,
    *,
    offline: bool,
    previous: LockFile | None,
) -> tuple[LockedPackage, Project | None, tuple[Dependency, ...]]:
    executable = shutil.which("iron")
    if executable is None:
        raise EvmError(f"cannot resolve IRON dependency {dependency.name}: iron is not installed")
    if offline:
        locked = _previous_package(previous, dependency.name)
        archive = _cached_archive(project, locked)
        root = _extract_archive_for_resolution(project, dependency.name, archive)
        nested = _load_nested_manifest(root)
        requested_ecf = dependency.ecf or _iron_package_ecf(root, dependency.name)
        ecf = _dependency_ecf(root, requested_ecf, nested)
        discovered = _ecf_iron_dependencies(root, ecf, dependency)
        return (
            replace(locked, ecf=ecf, development=dependency.development),
            nested,
            discovered,
        )
    completed = subprocess.run(
        [executable, "info", dependency.name],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise EvmError(
            f"IRON did not provide metadata for {dependency.name}: "
            f"{completed.stderr.strip() or 'package unavailable'}"
        )
    repository, archive_url, resolved_version = _iron_archive_metadata(
        completed.stdout,
        dependency.name,
    )
    requested_version = dependency.version
    if requested_version is not None and not (
        resolved_version == requested_version
        or resolved_version.startswith(f"{requested_version}.")
    ):
        raise EvmError(
            f"IRON package {dependency.name} resolved to {resolved_version}, "
            f"which does not satisfy {requested_version}"
        )
    archive, checksum = _download_archive(project, archive_url)
    root = _extract_archive_for_resolution(project, dependency.name, archive)
    nested = _load_nested_manifest(root)
    requested_ecf = dependency.ecf or _iron_package_ecf(root, dependency.name)
    ecf = _dependency_ecf(root, requested_ecf, nested)
    discovered = _ecf_iron_dependencies(root, ecf, dependency)
    return (
        LockedPackage(
            name=dependency.name,
            version=resolved_version,
            source=f"iron+{repository}",
            checksum=checksum,
            requested=(f"version:{dependency.version}" if dependency.version is not None else None),
            ecf=ecf,
            manifest="Eiffel.toml" if nested is not None else None,
            archive=archive_url,
            development=dependency.development,
        ),
        nested,
        discovered,
    )


def _install_package(
    project: Project,
    lock: LockFile,
    package: LockedPackage,
    *,
    offline: bool,
) -> None:
    if package.path is not None:
        root = (project.directory / package.path).resolve()
        if not root.is_dir():
            raise EvmError(f"path dependency {package.name} does not exist: {package.path}")
        return
    destination = project.directory / ".evm" / "deps" / package.materialized_name
    if destination.is_dir() and _installed_package_valid(destination, package):
        return
    if offline and not _source_available(project, package):
        identity = package.revision or package.version
        raise EvmError(
            f"dependency {package.name} ({package.source} {identity}) is missing locally; "
            "run `evm install` with network access"
        )
    temporary_root = project.directory / ".evm" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{package.name}-", dir=temporary_root))
    try:
        content = temporary / "content"
        if package.source.startswith("git+"):
            _install_git(project, package, content, offline=offline)
        elif package.source in {"eiffelstudio", "gobo-distribution"}:
            _install_distribution(package, content)
        elif package.source.startswith("iron+"):
            _install_iron(project, package, content, offline=offline)
        else:
            raise EvmError(f"unsupported locked source for {package.name}: {package.source}")
        _rewrite_iron_ecf_locations(project, lock, package, content)
        _validate_package_tree(content, package)
        atomic_write(content / ".evm-package", _package_marker(package, content).encode())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        content.replace(destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _install_git(
    project: Project,
    package: LockedPackage,
    destination: Path,
    *,
    offline: bool,
) -> None:
    url = package.source.removeprefix("git+")
    repository = _git_repository(
        project,
        url,
        offline=offline,
        request=_GitFetchRequest(locked_revision=package.revision),
    )
    if package.revision is None:
        raise EvmError(f"Git package {package.name} has no locked revision")
    treeish = (
        f"{package.revision}:{package.subdir}" if package.subdir else f"{package.revision}^{{tree}}"
    )
    actual_tree = _git_output(repository, "rev-parse", treeish)
    if actual_tree != package.tree:
        raise EvmError(f"Git tree verification failed for {package.name}")
    archive = destination.with_suffix(".tar")
    _run_git(
        "-C",
        str(repository),
        "archive",
        "--format=tar",
        f"--output={archive}",
        treeish,
    )
    destination.mkdir(parents=True)
    try:
        with tarfile.open(archive, mode="r:") as tar:
            tar.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise EvmError(f"cannot extract Git tree for {package.name}: {error}") from error
    finally:
        archive.unlink(missing_ok=True)


def _install_distribution(package: LockedPackage, destination: Path) -> None:
    adapter = "ise" if package.source == "eiffelstudio" else "gobo"
    root = _distribution_root(adapter)
    if adapter == "gobo":
        shutil.copytree(root, destination / "library", symlinks=True)
        return
    package_root, _ = _find_distribution_library(root, package.library or package.name, package.ecf)
    shutil.copytree(package_root, destination, symlinks=True)


def _install_iron(
    project: Project,
    package: LockedPackage,
    destination: Path,
    *,
    offline: bool,
) -> None:
    archive = _ensure_iron_archive(project, package, offline=offline)
    destination.mkdir(parents=True)
    try:
        with tarfile.open(archive, mode="r:bz2") as tar:
            tar.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise EvmError(f"cannot extract IRON archive for {package.name}: {error}") from error


def _git_repository(
    project: Project,
    url: str,
    *,
    offline: bool,
    request: _GitFetchRequest,
) -> Path:
    directory = project.directory / ".evm" / "sources" / "git" / _source_key(url)
    repository = directory / "repository.git"
    if not repository.is_dir():
        if offline:
            raise EvmError(f"Git source is unavailable offline: {url}")
        directory.mkdir(parents=True, exist_ok=True)
        _run_git("init", "--bare", str(repository))
        _run_git("-C", str(repository), "remote", "add", "origin", url)
    if not offline:
        _fetch_git_identity(
            repository,
            requested_kind=request.requested_kind,
            requested_value=request.requested_value,
            locked_revision=request.locked_revision,
        )
    return repository


def _fetch_git_identity(
    repository: Path,
    *,
    requested_kind: str | None,
    requested_value: str | None,
    locked_revision: str | None,
) -> None:
    if locked_revision is not None:
        try:
            _git_output(repository, "cat-file", "-e", f"{locked_revision}^{{commit}}")
        except EvmError:
            _run_git("-C", str(repository), "fetch", "--depth=1", "origin", locked_revision)
        _run_git(
            "-C",
            str(repository),
            "update-ref",
            f"refs/evm/locked/{locked_revision}",
            locked_revision,
        )
        return
    if requested_kind == "branch" and requested_value:
        ref = f"refs/heads/{requested_value}"
        _run_git(
            "-C",
            str(repository),
            "fetch",
            "--depth=1",
            "--force",
            "origin",
            f"{ref}:{ref}",
        )
        return
    if requested_kind == "tag" and requested_value:
        ref = f"refs/tags/{requested_value}"
        _run_git(
            "-C",
            str(repository),
            "fetch",
            "--depth=1",
            "--force",
            "origin",
            f"{ref}:{ref}",
        )
        return
    if requested_kind == "rev" and requested_value:
        if len(requested_value) == 40:
            _run_git("-C", str(repository), "fetch", "--depth=1", "origin", requested_value)
            return
        _run_git(
            "-C",
            str(repository),
            "fetch",
            "--filter=blob:none",
            "origin",
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
        )
        return
    raise EvmError("Git resolution requires a branch, tag, revision, or locked commit")


def _git_revision(repository: Path, dependency: Dependency) -> str:
    value = dependency.requested_value
    if value is None:
        raise AssertionError("validated Git dependency has no revision value")
    if dependency.requested_kind == "tag":
        reference = f"refs/tags/{value}^{{commit}}"
    elif dependency.requested_kind == "branch":
        reference = f"refs/heads/{value}^{{commit}}"
    else:
        reference = f"{value}^{{commit}}"
    revision = _git_output(repository, "rev-parse", "--verify", reference)
    if len(revision) != 40:
        raise EvmError(f"Git did not resolve {value!r} to a full commit hash")
    return revision


def _git_checkout(repository: Path, revision: str) -> Path:
    checkout = repository.parent / revision
    if checkout.is_dir():
        return checkout
    _run_git("-C", str(repository), "worktree", "add", "--detach", str(checkout), revision)
    return checkout


def _git_output(repository: Path, *arguments: str) -> str:
    completed = _run_git("-C", str(repository), *arguments)
    return completed.stdout.strip()


def _run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        details = ""
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            details = f": {error.stderr.strip()}"
        raise EvmError(f"Git operation failed ({' '.join(arguments)}){details}") from error


def _load_nested_manifest(root: Path) -> Project | None:
    manifest = root / "Eiffel.toml"
    return load_manifest(manifest) if manifest.is_file() else None


def _dependency_ecf(root: Path, requested: str | None, nested: Project | None) -> str:
    if requested is not None:
        candidate = _inside(root, requested, "dependency.ecf")
    elif nested is not None:
        candidate = _inside(
            root,
            os.path.relpath(nested.ecf_path, root),
            "dependency project.ecf",
        )
    else:
        matches = sorted(root.glob("*.ecf"))
        if len(matches) != 1:
            raise EvmError(
                "legacy dependency must specify ecf when it has not exactly one root ECF"
            )
        candidate = matches[0]
    if not candidate.is_file():
        raise EvmError(f"dependency ECF does not exist: {candidate}")
    return candidate.relative_to(root).as_posix()


def _distribution_root(adapter: str) -> Path:
    variable = "ISE_LIBRARY" if adapter == "ise" else "GOBO"
    value = os.environ.get(variable)
    if value:
        root = Path(value)
        if adapter == "gobo":
            root /= "library"
        return root.resolve()
    if adapter == "ise" and os.environ.get("ISE_EIFFEL"):
        return (Path(os.environ["ISE_EIFFEL"]) / "library").resolve()
    if adapter == "ise":
        detected = detect("ise")
        if detected.executable is not None:
            installed_library = detected.executable.resolve().parent.parent / "library"
            if installed_library.is_dir():
                return installed_library
    raise EvmError(f"{variable} is required to resolve {adapter} libraries")


def _find_distribution_library(
    distribution_root: Path,
    library: str,
    requested_ecf: str | None,
) -> tuple[Path, str]:
    base = distribution_root / library
    if requested_ecf:
        candidate = _inside(base, requested_ecf, f"{library}.ecf")
    else:
        conventional = base / f"{library}.ecf"
        matches = [conventional] if conventional.is_file() else sorted(base.glob("*.ecf"))
        if len(matches) != 1:
            raise EvmError(f"cannot identify ECF for distribution library {library}")
        candidate = matches[0]
    if not candidate.is_file():
        raise EvmError(f"distribution library ECF does not exist: {candidate}")
    return base, candidate.relative_to(base).as_posix()


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    excluded = {".git", ".evm", "build"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts):
            continue
        if relative.as_posix() == ".evm-package":
            continue
        digest.update(relative.as_posix().encode())
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
    return digest.hexdigest()


def _inside(root: Path, raw: str, field: str) -> Path:
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise EvmError(f"{field} escapes the dependency root") from error
    return candidate


def _validate_package_tree(root: Path, package: LockedPackage) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            _inside(root, os.path.relpath(path.resolve(), root), f"{package.name} symlink")
    if package.ecf is not None and not _inside(root, package.ecf, f"{package.name}.ecf").is_file():
        raise EvmError(f"materialized package {package.name} is missing {package.ecf}")
    if package.checksum and package.source in {"eiffelstudio", "gobo-distribution"}:
        checksum_root = root / "library" if package.source == "gobo-distribution" else root
        actual = f"sha256:{_directory_hash(checksum_root)}"
        if actual != package.checksum:
            raise EvmError(f"checksum verification failed for {package.name}")


def _installed_package_valid(root: Path, package: LockedPackage) -> bool:
    marker = root / ".evm-package"
    return marker.is_file() and marker.read_text() == _package_marker(package, root)


def _package_marker(package: LockedPackage, root: Path) -> str:
    identity = package.tree or package.checksum or package.version
    dependencies = ",".join(package.dependencies)
    return f"{package.source}\n{identity}\n{dependencies}\n{_directory_hash(root)}\n"


def _source_available(project: Project, package: LockedPackage) -> bool:
    if package.source.startswith("git+"):
        url = package.source.removeprefix("git+")
        repository = (
            project.directory / ".evm" / "sources" / "git" / _source_key(url) / "repository.git"
        )
        if not repository.is_dir() or package.revision is None:
            return False
        try:
            _git_output(repository, "cat-file", "-e", f"{package.revision}^{{commit}}")
        except EvmError:
            return False
        return True
    if package.source.startswith("iron+"):
        try:
            _cached_archive(project, package)
        except EvmError:
            return False
        return True
    return package.source in {"eiffelstudio", "gobo-distribution"}


def _write_state(project: Project, lock: LockFile) -> None:
    document = tomlkit.document()
    document.add("manifest-fingerprint", lock.manifest_fingerprint)
    document.add(
        "package",
        [
            {
                "name": package.name,
                "directory": package.materialized_name,
                "identity": package.tree or package.checksum or package.version,
            }
            for package in lock.packages
            if package.path is None
        ],
    )
    atomic_write(
        project.directory / ".evm" / "state.toml",
        tomlkit.dumps(document).encode(),
    )


def _declared_source(dependency: Dependency) -> str:
    if dependency.source == "git":
        return f"git+{dependency.git}"
    if dependency.source == "path":
        return f"path+{dependency.path}"
    return {
        "ise": "eiffelstudio",
        "gobo": "gobo-distribution",
        "iron": "iron",
    }[dependency.source]


def _package_identity(package: LockedPackage) -> tuple[str, str, str | None]:
    return package.source, package.version, package.revision


def _copy_locked_closure(
    name: str,
    previous: LockFile,
    resolved: dict[str, LockedPackage],
) -> None:
    if name in resolved:
        return
    try:
        package = previous.package(name)
    except KeyError as error:
        raise EvmError(f"Eiffel.lock is missing transitive dependency {name}") from error
    resolved[name] = package
    for child in package.dependencies:
        _copy_locked_closure(child, previous, resolved)


def _source_key(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def _by_name(package: LockedPackage) -> str:
    return package.name


def _remove_unlisted_directories(root: Path, used: set[str]) -> int:
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        if child.is_dir() and child.name not in used:
            shutil.rmtree(child)
            removed += 1
    return removed


def _remove_unlisted_files(root: Path, used_names: set[str]) -> int:
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        if child.is_file() and child.name not in used_names:
            child.unlink()
            removed += 1
    return removed


def _previous_package(previous: LockFile | None, name: str) -> LockedPackage:
    if previous is None:
        raise EvmError(f"IRON dependency {name} is unavailable offline; run `evm update` online")
    try:
        return previous.package(name)
    except KeyError as error:
        raise EvmError(
            f"IRON dependency {name} is unavailable offline; run `evm update` online"
        ) from error


def _iron_archive_metadata(output: str, name: str) -> tuple[str, str, str]:
    identifier_match = re.search(r"^\s*id:\s*([0-9A-Fa-f-]+)\s*$", output, re.MULTILINE)
    repository_match = re.search(r"^\s*repository:\s*(https?://\S+)\s*$", output, re.MULTILINE)
    revision_match = re.search(r"^\s*archive:\s+revision=(\d+)", output, re.MULTILINE)
    if identifier_match is None or repository_match is None or revision_match is None:
        raise EvmError(f"IRON metadata for {name} is incomplete")
    repository = repository_match.group(1).rstrip("/")
    scheme, separator, location = repository.partition("://")
    if not separator or "/" not in location:
        raise EvmError(f"unsupported IRON repository URL: {repository}")
    host, repository_path = location.split("/", 1)
    archive = (
        f"{scheme}://{host}/access/{repository_path}/package/{identifier_match.group(1)}/archive"
    )
    repository_version = repository.rsplit("/", 1)[-1]
    resolved_version = f"{repository_version}.{revision_match.group(1)}"
    return repository, archive, resolved_version


def _download_archive(project: Project, url: str) -> tuple[Path, str]:
    archives = project.directory / ".evm" / "sources" / "archives"
    temporary_root = project.directory / ".evm" / "tmp"
    archives.mkdir(parents=True, exist_ok=True)
    temporary_root.mkdir(parents=True, exist_ok=True)
    temporary = temporary_root / f"archive-{_source_key(url)}.download"
    digest = hashlib.sha256()
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=_GIT_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    stream.write(chunk)
    except (OSError, httpx.HTTPError) as error:
        temporary.unlink(missing_ok=True)
        raise EvmError(f"cannot download IRON archive {url}: {error}") from error
    checksum_digest = digest.hexdigest()
    destination = archives / f"{checksum_digest}.tar.bz2"
    if destination.exists():
        temporary.unlink()
    else:
        temporary.replace(destination)
    return destination, f"sha256:{checksum_digest}"


def _cached_archive(project: Project, package: LockedPackage) -> Path:
    if package.checksum is None or not package.checksum.startswith("sha256:"):
        raise EvmError(f"IRON package {package.name} has no SHA-256 archive checksum")
    digest = package.checksum.removeprefix("sha256:")
    archive = project.directory / ".evm" / "sources" / "archives" / f"{digest}.tar.bz2"
    if not archive.is_file() or _file_sha256(archive) != digest:
        raise EvmError(
            f"IRON archive for {package.name} is missing or corrupted; "
            "run `evm install` with network access"
        )
    return archive


def _ensure_iron_archive(
    project: Project,
    package: LockedPackage,
    *,
    offline: bool,
) -> Path:
    try:
        return _cached_archive(project, package)
    except EvmError:
        if offline:
            raise
    if package.archive is None or package.checksum is None:
        raise EvmError(f"IRON package {package.name} has no locked archive identity")
    archive, checksum = _download_archive(project, package.archive)
    if checksum != package.checksum:
        archive.unlink(missing_ok=True)
        raise EvmError(f"IRON archive checksum verification failed for {package.name}")
    return archive


def _extract_archive_for_resolution(project: Project, name: str, archive: Path) -> Path:
    temporary_root = project.directory / ".evm" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"resolve-{name}-", dir=temporary_root))
    try:
        with tarfile.open(archive, mode="r:bz2") as tar:
            tar.extractall(root, filter="data")
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(root, ignore_errors=True)
        raise EvmError(f"cannot inspect IRON archive for {name}: {error}") from error
    return root


def _iron_package_ecf(root: Path, name: str) -> str:
    package_file = root / "package.iron"
    if not package_file.is_file():
        raise EvmError(f"IRON package {name} has no package.iron; specify ecf explicitly")
    content = package_file.read_text(encoding="utf-8")
    match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]+\.ecf)"', content, re.MULTILINE)
    if match is None:
        raise EvmError(f"IRON package {name} has no project named {name}; specify ecf explicitly")
    return match.group(1)


def _ecf_iron_dependencies(
    root: Path,
    ecf: str,
    owner: Dependency,
) -> tuple[Dependency, ...]:
    ecf_path = _inside(root, ecf, f"{owner.name}.ecf")
    try:
        tree = etree.parse(
            str(ecf_path),
            parser=etree.XMLParser(resolve_entities=False, no_network=True),
        )
    except (OSError, etree.XMLSyntaxError) as error:
        raise EvmError(f"cannot inspect IRON ECF {ecf}: {error}") from error
    dependencies: dict[str, Dependency] = {}
    for location in tree.xpath("//@location"):
        if not isinstance(location, str) or not location.startswith("iron:"):
            continue
        parts = location.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise EvmError(f"invalid IRON URI in {ecf}: {location}")
        name, dependency_ecf = parts[1:]
        if name == owner.name:
            continue
        dependencies[name] = Dependency(
            name=name,
            source="iron",
            version=owner.version,
            ecf=dependency_ecf,
            development=owner.development,
        )
    return tuple(dependencies[name] for name in sorted(dependencies))


def _rewrite_iron_ecf_locations(
    project: Project,
    lock: LockFile,
    package: LockedPackage,
    root: Path,
) -> None:
    package_destination = project.directory / ".evm" / "deps" / package.materialized_name
    for ecf_path in root.rglob("*.ecf"):
        try:
            tree = etree.parse(
                str(ecf_path),
                parser=etree.XMLParser(resolve_entities=False, no_network=True),
            )
        except (OSError, etree.XMLSyntaxError) as error:
            raise EvmError(f"cannot rewrite IRON ECF {ecf_path}: {error}") from error
        changed = False
        final_ecf_parent = package_destination / ecf_path.relative_to(root).parent
        for element in tree.xpath("//*[@location]"):
            location = element.get("location")
            if location is None or not location.startswith("iron:"):
                continue
            parts = location.split(":", 2)
            if len(parts) != 3:
                continue
            try:
                dependency = lock.package(parts[1])
            except KeyError:
                continue
            dependency_ecf = (
                project.directory / ".evm" / "deps" / dependency.materialized_name / parts[2]
            )
            element.set(
                "location",
                Path(os.path.relpath(dependency_ecf, final_ecf_parent)).as_posix(),
            )
            changed = True
        if changed:
            ecf_path.write_bytes(
                etree.tostring(
                    tree,
                    encoding="UTF-8",
                    xml_declaration=True,
                    pretty_print=True,
                )
            )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_tree(
    renderer: _TreeRenderer,
    name: str,
    prefix: str,
    last: bool,
    ancestors: set[str],
) -> None:
    package = renderer.packages.get(name)
    branch = "└── " if last else "├── "
    if package is None:
        renderer.lines.append(f"{prefix}{branch}{name} [missing from lock]")
        return
    renderer.lines.append(f"{prefix}{branch}{_package_label(package)}")
    if name in ancestors:
        return
    children = package.dependencies
    child_prefix = prefix + ("    " if last else "│   ")
    for index, child in enumerate(children):
        _append_tree(
            renderer,
            child,
            child_prefix,
            index == len(children) - 1,
            {*ancestors, name},
        )


def _package_label(package: LockedPackage) -> str:
    if package.source.startswith("git+"):
        requested = package.requested or "revision"
        revision = (package.revision or "")[:8]
        source = f"git {requested} @ {revision}"
    elif package.source == "eiffelstudio":
        source = "ISE"
    elif package.source == "gobo-distribution":
        source = "Gobo"
    elif package.source.startswith("iron+"):
        source = "IRON"
    else:
        source = "path"
    patch = " patched" if package.patched else ""
    return f"{package.name} {package.version} [{source}{patch}]"


def _dependency_paths(
    direct: list[str],
    focus: str,
    packages: dict[str, LockedPackage],
) -> list[list[str]]:
    result: list[list[str]] = []

    def visit(name: str, path: list[str]) -> None:
        if name in path:
            return
        current = [*path, name]
        if name == focus:
            result.append(current)
            return
        package = packages.get(name)
        if package is not None:
            for child in package.dependencies:
                visit(child, current)

    for name in direct:
        visit(name, [])
    return result
