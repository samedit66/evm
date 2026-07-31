from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from lxml import etree

import evm.dependencies as dependencies
from evm.errors import EvmError
from evm.lockfile import LockedPackage, LockFile, manifest_fingerprint
from evm.model import Dependency
from evm.project import create_project


def test_iron_metadata_is_normalized_to_archive_identity() -> None:
    output = """
    id: 12345678-abcd
    repository: https://iron.example/25.02/
    archive: revision=9
    """

    repository, archive, version = dependencies._iron_archive_metadata(output, "json")

    assert repository == "https://iron.example/25.02"
    assert archive == "https://iron.example/access/25.02/package/12345678-abcd/archive"
    assert version == "25.02.9"


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("repository: https://iron.example/25.02", "metadata for json is incomplete"),
        (
            "id: abcd\nrepository: https://iron.example\narchive: revision=1",
            "unsupported IRON repository URL",
        ),
    ],
)
def test_iron_metadata_rejects_incomplete_or_unsupported_output(
    output: str,
    message: str,
) -> None:
    with pytest.raises(EvmError, match=message):
        dependencies._iron_archive_metadata(output, "json")


def test_online_iron_resolution_downloads_and_locks_verified_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    archive = _write_archive(tmp_path / "json.tar.bz2", {"json.ecf": b"<system/>"})
    checksum = f"sha256:{dependencies._file_sha256(archive)}"
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: "/usr/bin/iron")
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            "id: abcd\nrepository: https://iron.example/1.2\narchive: revision=3",
            "",
        ),
    )
    monkeypatch.setattr(dependencies, "_download_archive", lambda *_: (archive, checksum))
    dependency = Dependency(name="json", source="iron", version="1.2", ecf="json.ecf")

    lock = dependencies.resolve_dependencies(replace(project, dependencies=(dependency,)))

    package = lock.package("json")
    assert package.version == "1.2.3"
    assert package.checksum == checksum
    assert package.requested == "version:1.2"


def test_online_iron_resolution_rejects_version_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: "/usr/bin/iron")
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            "id: abcd\nrepository: https://iron.example/2.0\narchive: revision=1",
            "",
        ),
    )
    dependency = Dependency(name="json", source="iron", version="1.2")

    with pytest.raises(EvmError, match=r"does not satisfy 1\.2"):
        dependencies.resolve_dependencies(replace(project, dependencies=(dependency,)))


def test_iron_resolution_reports_missing_client_and_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    dependency = Dependency(name="json", source="iron")
    parsed = replace(project, dependencies=(dependency,))
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: None)

    with pytest.raises(EvmError, match="iron is not installed"):
        dependencies.resolve_dependencies(parsed)

    monkeypatch.setattr(dependencies.shutil, "which", lambda _: "/usr/bin/iron")
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "not found"),
    )
    with pytest.raises(EvmError, match=r"IRON did not provide metadata.*not found"):
        dependencies.resolve_dependencies(parsed)


def test_offline_iron_resolution_uses_previous_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    archive = _write_archive(tmp_path / "json.tar.bz2", {"json.ecf": b"<system/>"})
    digest = dependencies._file_sha256(archive)
    cached = project.state_directory / "sources" / "archives" / f"{digest}.tar.bz2"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(archive.read_bytes())
    package = LockedPackage(
        name="json",
        version="1.2.3",
        source="iron+https://iron.example/1.2",
        checksum=f"sha256:{digest}",
        ecf="json.ecf",
        archive="https://iron.example/json.tar.bz2",
    )
    dependency = Dependency(name="json", source="iron", version="1.2", ecf="json.ecf")
    parsed = replace(project, dependencies=(dependency,))
    previous = LockFile(manifest_fingerprint(parsed), (package,))
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: "/usr/bin/iron")

    lock = dependencies.resolve_dependencies(parsed, offline=True, previous=previous)

    assert lock.package("json") == package


@pytest.mark.parametrize("previous", [None, LockFile("fingerprint", ())])
def test_offline_iron_resolution_requires_previous_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous: LockFile | None,
) -> None:
    project = create_project(tmp_path / "app")
    dependency = Dependency(name="json", source="iron")
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: "/usr/bin/iron")

    with pytest.raises(EvmError, match="unavailable offline"):
        dependencies.resolve_dependencies(
            replace(project, dependencies=(dependency,)),
            offline=True,
            previous=previous,
        )


def test_ensure_iron_archive_downloads_missing_cache_and_checks_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    archive = tmp_path / "archive.tar.bz2"
    archive.write_bytes(b"archive")
    package = LockedPackage(
        name="json",
        version="1.0.0",
        source="iron+repo",
        checksum="sha256:expected",
        archive="https://example.invalid/archive",
    )
    monkeypatch.setattr(
        dependencies,
        "_download_archive",
        lambda *_: (archive, "sha256:expected"),
    )

    assert dependencies._ensure_iron_archive(project, package, offline=False) == archive

    archive.write_bytes(b"archive")
    monkeypatch.setattr(
        dependencies,
        "_download_archive",
        lambda *_: (archive, "sha256:wrong"),
    )
    with pytest.raises(EvmError, match="archive checksum verification failed"):
        dependencies._ensure_iron_archive(project, package, offline=False)
    assert not archive.exists()


def test_ensure_iron_archive_requires_locked_archive_identity(tmp_path: Path) -> None:
    project = create_project(tmp_path / "app")
    package = LockedPackage(name="json", version="1.0.0", source="iron+repo")

    with pytest.raises(EvmError, match="no locked archive identity"):
        dependencies._ensure_iron_archive(project, package, offline=False)

    with pytest.raises(EvmError, match="no SHA-256 archive checksum"):
        dependencies._ensure_iron_archive(project, package, offline=True)


def test_extract_archive_reports_corruption_and_cleans_directory(tmp_path: Path) -> None:
    project = create_project(tmp_path / "app")
    archive = tmp_path / "broken.tar.bz2"
    archive.write_bytes(b"broken")

    with pytest.raises(EvmError, match="cannot inspect IRON archive for json"):
        dependencies._extract_archive_for_resolution(project, "json", archive)

    assert list((project.state_directory / "tmp").iterdir()) == []


def test_iron_package_ecf_is_read_from_package_file(tmp_path: Path) -> None:
    (tmp_path / "package.iron").write_text('json = "library/json.ecf"')

    assert dependencies._iron_package_ecf(tmp_path, "json") == "library/json.ecf"

    with pytest.raises(EvmError, match="no project named xml"):
        dependencies._iron_package_ecf(tmp_path, "xml")
    (tmp_path / "package.iron").unlink()
    with pytest.raises(EvmError, match=r"has no package\.iron"):
        dependencies._iron_package_ecf(tmp_path, "json")


def test_ecf_iron_dependencies_are_discovered_and_normalized(tmp_path: Path) -> None:
    ecf = tmp_path / "json.ecf"
    ecf.write_text(
        '<system><target><library location="plain.ecf"/>'
        '<library location="iron:base:base.ecf"/>'
        '<library location="iron:json:self.ecf"/>'
        '<library location="iron:base:other.ecf"/></target></system>'
    )
    owner = Dependency(name="json", source="iron", version="1", development=True)

    discovered = dependencies._ecf_iron_dependencies(tmp_path, "json.ecf", owner)

    assert discovered == (
        Dependency(
            name="base",
            source="iron",
            version="1",
            ecf="other.ecf",
            development=True,
        ),
    )


@pytest.mark.parametrize("location", ["iron:", "iron::base.ecf", "iron:base:"])
def test_ecf_iron_dependency_rejects_invalid_uri(tmp_path: Path, location: str) -> None:
    (tmp_path / "json.ecf").write_text(f'<system><library location="{location}"/></system>')
    owner = Dependency(name="json", source="iron")

    with pytest.raises(EvmError, match="invalid IRON URI"):
        dependencies._ecf_iron_dependencies(tmp_path, "json.ecf", owner)


def test_ecf_iron_dependency_reports_invalid_xml(tmp_path: Path) -> None:
    (tmp_path / "json.ecf").write_text("<broken>")

    with pytest.raises(EvmError, match="cannot inspect IRON ECF"):
        dependencies._ecf_iron_dependencies(
            tmp_path,
            "json.ecf",
            Dependency(name="json", source="iron"),
        )


def test_materialization_rewrites_known_iron_locations(tmp_path: Path) -> None:
    project = create_project(tmp_path / "app")
    root = tmp_path / "content"
    root.mkdir()
    ecf = root / "json.ecf"
    ecf.write_text(
        '<system><library location="iron:base:base.ecf"/>'
        '<library location="iron:missing:missing.ecf"/>'
        '<library location="plain.ecf"/></system>'
    )
    package = LockedPackage(name="json", version="1", source="iron+repo", ecf="json.ecf")
    base = LockedPackage(name="base", version="1", source="iron+repo", ecf="base.ecf")
    lock = LockFile(manifest_fingerprint(project), (base, package))

    dependencies._rewrite_iron_ecf_locations(project, lock, package, root)

    locations = etree.parse(str(ecf)).xpath("//@location")
    assert locations[0].endswith(f"/{base.materialized_name}/base.ecf")
    assert locations[1:] == ["iron:missing:missing.ecf", "plain.ecf"]


def test_materialization_reports_invalid_ecf_during_rewrite(tmp_path: Path) -> None:
    project = create_project(tmp_path / "app")
    root = tmp_path / "content"
    root.mkdir()
    (root / "broken.ecf").write_text("<broken>")
    package = LockedPackage(name="json", version="1", source="iron+repo")
    lock = LockFile(manifest_fingerprint(project), (package,))

    with pytest.raises(EvmError, match="cannot rewrite IRON ECF"):
        dependencies._rewrite_iron_ecf_locations(project, lock, package, root)


def test_package_tree_validation_rejects_missing_ecf_and_escaping_symlink(
    tmp_path: Path,
) -> None:
    package = LockedPackage(name="json", version="1", source="iron+repo", ecf="json.ecf")

    with pytest.raises(EvmError, match=r"materialized package json is missing json\.ecf"):
        dependencies._validate_package_tree(tmp_path, package)

    (tmp_path / "json.ecf").touch()
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(EvmError, match="json symlink escapes the dependency root"):
        dependencies._validate_package_tree(tmp_path, package)


def test_distribution_checksum_is_verified_before_use(tmp_path: Path) -> None:
    (tmp_path / "base.ecf").write_text("<system/>")
    good = LockedPackage(
        name="base",
        version="1",
        source="eiffelstudio",
        ecf="base.ecf",
        checksum=f"sha256:{dependencies._directory_hash(tmp_path)}",
    )

    dependencies._validate_package_tree(tmp_path, good)

    with pytest.raises(EvmError, match="checksum verification failed for base"):
        dependencies._validate_package_tree(tmp_path, replace(good, checksum="sha256:bad"))


def test_distribution_roots_and_library_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ise = tmp_path / "ise"
    gobo = tmp_path / "gobo"
    (ise / "base").mkdir(parents=True)
    (ise / "base" / "base.ecf").touch()
    (gobo / "library" / "xml").mkdir(parents=True)
    (gobo / "library" / "xml" / "custom.ecf").touch()
    monkeypatch.setenv("ISE_LIBRARY", str(ise))
    monkeypatch.setenv("GOBO", str(gobo))

    assert dependencies._distribution_root("ise") == ise
    assert dependencies._distribution_root("gobo") == gobo / "library"
    assert dependencies._find_distribution_library(ise, "base", None) == (
        ise / "base",
        "base.ecf",
    )
    assert dependencies._find_distribution_library(gobo / "library", "xml", "custom.ecf") == (
        gobo / "library" / "xml",
        "custom.ecf",
    )


def test_distribution_library_selection_reports_ambiguous_or_missing_ecf(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "one.ecf").touch()
    (base / "two.ecf").touch()

    with pytest.raises(EvmError, match="cannot identify ECF"):
        dependencies._find_distribution_library(tmp_path, "base", None)
    with pytest.raises(EvmError, match="distribution library ECF does not exist"):
        dependencies._find_distribution_library(tmp_path, "base", "missing.ecf")


def test_git_operation_errors_include_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    error = subprocess.CalledProcessError(1, ["git"], stderr="fatal: unavailable")
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(EvmError, match=r"Git operation failed.*fatal: unavailable"):
        dependencies._run_git("status")


def test_download_archive_streams_content_and_reuses_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    response = _StreamResponse([b"one", b"two"])
    monkeypatch.setattr(dependencies.httpx, "stream", lambda *args, **kwargs: response)

    archive, checksum = dependencies._download_archive(project, "https://example.invalid/archive")
    repeated, repeated_checksum = dependencies._download_archive(
        project,
        "https://example.invalid/archive",
    )

    assert archive.read_bytes() == b"onetwo"
    assert checksum == f"sha256:{hashlib.sha256(b'onetwo').hexdigest()}"
    assert (repeated, repeated_checksum) == (archive, checksum)


def test_download_archive_removes_partial_file_on_http_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(tmp_path / "app")
    error = httpx.HTTPError("network failed")
    monkeypatch.setattr(
        dependencies.httpx,
        "stream",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(EvmError, match="cannot download IRON archive"):
        dependencies._download_archive(project, "https://example.invalid/archive")

    assert list((project.state_directory / "tmp").iterdir()) == []


class _StreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __enter__(self) -> _StreamResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self) -> list[bytes]:
        return self.chunks


def _write_archive(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, mode="w:bz2") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path
