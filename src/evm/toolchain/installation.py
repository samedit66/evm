"""Official catalog resolution and atomic toolchain installation."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

import httpx
import py7zr
from filelock import FileLock

from evm.errors import EvmError
from evm.lockfile import LockedToolchain
from evm.toolchain.store import (
    list_installations,
    managed_installation_directory,
    save_managed_installation,
    user_toolchain_root,
)
from evm.toolchain.types import (
    InstallationKind,
    ToolchainArtifact,
    ToolchainInstallation,
    ToolchainPlatform,
    ToolchainSelector,
    current_toolchain_platform,
    executable_name,
)

_GOBO_RELEASES_URL = "https://api.github.com/repos/gobo-eiffel/gobo/releases"
_EIFFEL_INSTALL_SCRIPT_URL = "https://www.eiffel.org/setup/install.sh"
_EIFFEL_CDN_URL = "https://www.eiffel.com/cdn/EiffelStudio"
_EIFFEL_ARCHIVE_URL = "https://ftp.eiffel.com/pub/download"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 60
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def available_artifacts(
    provider: str,
    client: httpx.Client | None = None,
    platform: ToolchainPlatform | None = None,
) -> tuple[ToolchainArtifact, ...]:
    current_platform = platform or current_toolchain_platform()
    with _catalog_client(client) as catalog:
        if provider == "gobo":
            return _gobo_artifacts(catalog, current_platform)
        if provider == "ise":
            return _eiffel_artifacts(catalog, current_platform)
    raise EvmError(f"unknown toolchain provider {provider!r}; known providers: ise, gobo")


def resolve_artifact(
    selector: ToolchainSelector,
    client: httpx.Client | None = None,
    platform: ToolchainPlatform | None = None,
) -> ToolchainArtifact:
    artifacts = available_artifacts(selector.provider, client, platform)
    requested = selector.requested_version
    candidates = [artifact for artifact in artifacts if _artifact_matches(artifact, requested)]
    if not candidates:
        raise EvmError(
            f"no {selector.provider} artifact matches {requested!r} for "
            f"{(platform or current_toolchain_platform()).identifier}"
        )
    return candidates[0]


def install_toolchain(
    selector: ToolchainSelector,
    *,
    offline: bool = False,
    client: httpx.Client | None = None,
    platform: ToolchainPlatform | None = None,
) -> ToolchainInstallation:
    artifact = _resolve_install_artifact(selector, offline, client, platform)
    destination = managed_installation_directory(
        artifact.provider, artifact.revision, artifact.platform
    )
    lock = FileLock(str(destination) + ".lock")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        existing = _existing_installation(destination)
        if existing is not None:
            return existing
        archive, checksum = _obtain_archive(artifact, offline, client)
        return _extract_installation(artifact, archive, checksum, destination)


def install_locked_toolchain(
    locked: LockedToolchain,
    *,
    offline: bool = False,
    client: httpx.Client | None = None,
) -> ToolchainInstallation:
    platform = current_toolchain_platform()
    if (locked.platform, locked.architecture) != (
        platform.operating_system,
        platform.architecture,
    ):
        raise EvmError(
            f"locked toolchain {locked.provider}@{locked.revision} targets "
            f"{locked.platform}-{locked.architecture}, not {platform.identifier}"
        )
    artifact = ToolchainArtifact(
        locked.provider,
        locked.version,
        locked.revision,
        platform,
        locked.source,
        Path(httpx.URL(locked.source).path).name,
        locked.checksum,
    )
    destination = managed_installation_directory(
        artifact.provider, artifact.revision, artifact.platform
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination) + ".lock"):
        existing = _existing_installation(destination)
        if existing is not None:
            return existing
        archive, checksum = _obtain_archive(artifact, offline, client)
        return _extract_installation(artifact, archive, checksum, destination)


def _resolve_install_artifact(
    selector: ToolchainSelector,
    offline: bool,
    client: httpx.Client | None,
    platform: ToolchainPlatform | None,
) -> ToolchainArtifact:
    if not offline:
        return resolve_artifact(selector, client, platform)
    cached = _cached_artifacts(selector, platform or current_toolchain_platform())
    if not cached:
        raise EvmError(f"toolchain {selector} is not available in the offline download cache")
    return cached[0]


def _gobo_artifacts(
    client: httpx.Client,
    platform: ToolchainPlatform,
) -> tuple[ToolchainArtifact, ...]:
    response = _get(client, _GOBO_RELEASES_URL)
    releases = response.json()
    if not isinstance(releases, list):
        raise EvmError("Gobo release catalog returned an invalid response")
    artifacts: list[ToolchainArtifact] = []
    for release in releases:
        if not isinstance(release, Mapping) or release.get("draft") is True:
            continue
        tag = release.get("tag_name")
        assets = release.get("assets")
        if not isinstance(tag, str) or not isinstance(assets, list):
            continue
        channel = "nightly" if release.get("prerelease") is True else "stable"
        version = tag.removeprefix("gobo-")
        artifact = _gobo_release_artifact(assets, version, channel, platform)
        if artifact is not None:
            artifacts.append(artifact)
    return tuple(artifacts)


def _gobo_release_artifact(
    assets: list[object],
    version: str,
    channel: str,
    platform: ToolchainPlatform,
) -> ToolchainArtifact | None:
    prefix = f"gobo-{platform.archive_platform}-{platform.architecture}-"
    for raw in assets:
        if not isinstance(raw, Mapping):
            continue
        filename = raw.get("name")
        url = raw.get("browser_download_url")
        if not isinstance(filename, str) or not isinstance(url, str):
            continue
        if not filename.startswith(prefix) or not _supported_archive(filename):
            continue
        reported_versions = _VERSION_RE.findall(filename)
        revision = reported_versions[-1] if reported_versions else version
        digest = raw.get("digest")
        checksum = digest if isinstance(digest, str) and digest.startswith("sha256:") else None
        return ToolchainArtifact(
            "gobo", version, revision, platform, url, filename, checksum, channel
        )
    return None


def _eiffel_artifacts(
    client: httpx.Client,
    platform: ToolchainPlatform,
) -> tuple[ToolchainArtifact, ...]:
    script = _get(client, _EIFFEL_INSTALL_SCRIPT_URL).text
    artifacts: list[ToolchainArtifact] = []
    for channel, suffix in (("stable", "LATEST"), ("beta", "BETA"), ("nightly", "NIGHTLY")):
        version = _script_value(script, f"ISE_MAJOR_MINOR_{suffix}")
        build = _script_value(script, f"ISE_BUILD_{suffix}")
        revision = f"{version}.{build}"
        filename = f"Eiffel_{version}_rev_{build}-{platform.ise_platform}.tar.bz2"
        if channel == "stable":
            url = f"{_EIFFEL_CDN_URL}/{version}/{build}/{filename}"
        elif channel == "beta":
            url = f"https://ftp.eiffel.com/pub/beta/{version}/{filename}"
        else:
            url = f"https://ftp.eiffel.com/pub/beta/nightly/{filename}"
        artifact = ToolchainArtifact(
            "ise", version, revision, platform, url, filename, channel=channel
        )
        if artifact not in artifacts:
            artifacts.append(artifact)
    return tuple(artifacts)


def _script_value(script: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=([^\s#]+)", script, re.MULTILINE)
    if match is None:
        raise EvmError(f"EiffelStudio catalog does not define {name}")
    return match.group(1)


def _artifact_matches(artifact: ToolchainArtifact, requested: str) -> bool:
    if requested == "latest":
        return artifact.channel == "stable"
    if requested in {"beta", "nightly"}:
        return artifact.channel == requested
    return requested in {artifact.version, artifact.revision}


def _obtain_archive(
    artifact: ToolchainArtifact,
    offline: bool,
    client: httpx.Client | None,
) -> tuple[Path, str]:
    cache = _download_cache()
    archive = cache / artifact.filename
    checksum_file = archive.with_suffix(archive.suffix + ".sha256")
    if archive.is_file() and checksum_file.is_file():
        checksum = checksum_file.read_text(encoding="ascii").strip()
        if _sha256(archive) == checksum and _checksum_matches(artifact, checksum):
            return archive, checksum
    if offline:
        raise EvmError(f"cached archive is missing or invalid: {artifact.filename}")
    cache.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".part")
    try:
        with (
            _catalog_client(client) as catalog,
            catalog.stream("GET", artifact.url) as response,
        ):
            response.raise_for_status()
            digest = hashlib.sha256()
            with temporary.open("wb") as output:
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_SIZE):
                    digest.update(chunk)
                    output.write(chunk)
        checksum = digest.hexdigest()
        if not _checksum_matches(artifact, checksum):
            raise EvmError(f"checksum mismatch for {artifact.filename}")
        os.replace(temporary, archive)
        checksum_file.write_text(checksum + "\n", encoding="ascii")
        return archive, checksum
    except (OSError, httpx.HTTPError) as error:
        raise EvmError(f"cannot download {artifact.url}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _extract_installation(
    artifact: ToolchainArtifact,
    archive: Path,
    checksum: str,
    destination: Path,
) -> ToolchainInstallation:
    staging_parent = destination.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=staging_parent))
    try:
        extracted = staging / "extracted"
        extracted.mkdir()
        _extract_archive(archive, extracted)
        distribution_root = _distribution_root(artifact.provider, extracted, artifact.platform)
        destination.mkdir()
        installed_root = destination / "root"
        os.replace(distribution_root, installed_root)
        executable = _compiler_path(artifact.provider, installed_root, artifact.platform)
        installation = ToolchainInstallation(
            artifact.provider,
            artifact.version,
            artifact.revision,
            artifact.platform,
            installed_root,
            executable,
            InstallationKind.MANAGED,
            f"sha256:{checksum}",
            artifact.url,
        )
        save_managed_installation(installation)
        return installation
    except (OSError, tarfile.TarError, zipfile.BadZipFile, py7zr.Bad7zFile) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise EvmError(f"cannot install {artifact.identity}: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_archive(archive: Path, destination: Path) -> None:
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as bundle:
            _validate_archive_names(member.name for member in bundle.getmembers())
            bundle.extractall(destination, filter="data")
        return
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            _validate_archive_names(bundle.namelist())
            bundle.extractall(destination)
        return
    if archive.name.lower().endswith(".7z"):
        with py7zr.SevenZipFile(archive) as bundle:
            _validate_archive_names(bundle.getnames())
            bundle.extractall(destination)
        return
    raise EvmError(f"unsupported toolchain archive format: {archive.name}")


def _validate_archive_names(names: Iterable[str]) -> None:
    for raw_name in names:
        name = PurePosixPath(str(raw_name).replace("\\", "/"))
        if name.is_absolute() or ".." in name.parts:
            raise EvmError(f"unsafe path in toolchain archive: {raw_name}")


def _distribution_root(
    provider: str,
    extracted: Path,
    platform: ToolchainPlatform,
) -> Path:
    executable = executable_name(provider)
    patterns = (
        f"*/bin/{executable}" if provider == "gobo" else "",
        (f"*/studio/spec/{platform.ise_platform}/bin/{executable}" if provider == "ise" else ""),
    )
    matches = [path for pattern in patterns if pattern for path in extracted.glob(pattern)]
    if len(matches) != 1:
        raise EvmError(
            f"archive must contain exactly one {executable} installation; found {len(matches)}"
        )
    executable_path = matches[0]
    return executable_path.parents[1] if provider == "gobo" else executable_path.parents[4]


def _compiler_path(
    provider: str,
    root: Path,
    platform: ToolchainPlatform,
) -> Path:
    if provider == "gobo":
        return root / "bin" / executable_name(provider)
    return root / "studio" / "spec" / platform.ise_platform / "bin" / executable_name(provider)


def _existing_installation(destination: Path) -> ToolchainInstallation | None:
    metadata = destination / "installation.toml"
    if not metadata.is_file():
        return None
    return next(
        (item for item in list_installations() if item.root.parent == destination),
        None,
    )


def _download_cache() -> Path:
    override = os.environ.get("EVM_TOOLCHAIN_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    return user_toolchain_root().parent / "downloads"


def _cached_artifacts(
    selector: ToolchainSelector,
    platform: ToolchainPlatform,
) -> tuple[ToolchainArtifact, ...]:
    cache = _download_cache()
    if not cache.is_dir():
        return ()
    artifacts: list[ToolchainArtifact] = []
    provider_prefix = "Eiffel_" if selector.provider == "ise" else "gobo-"
    for archive in sorted(cache.iterdir(), reverse=True):
        if not archive.is_file() or not archive.name.startswith(provider_prefix):
            continue
        if not _supported_archive(archive.name):
            continue
        versions = _VERSION_RE.findall(archive.name)
        if not versions:
            continue
        revision = versions[-1]
        version = ".".join(revision.split(".")[:2])
        artifact = ToolchainArtifact(
            selector.provider,
            version,
            revision,
            platform,
            archive.as_uri(),
            archive.name,
        )
        if _artifact_matches(artifact, selector.requested_version):
            artifacts.append(artifact)
    return tuple(artifacts)


def _supported_archive(filename: str) -> bool:
    return filename.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".7z"))


def _checksum_matches(artifact: ToolchainArtifact, checksum: str) -> bool:
    return artifact.checksum in {None, f"sha256:{checksum}"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _get(client: httpx.Client, url: str) -> httpx.Response:
    try:
        response = client.get(url)
        response.raise_for_status()
        return response
    except httpx.HTTPError as error:
        raise EvmError(f"cannot read toolchain catalog {url}: {error}") from error


class _CatalogClient:
    def __init__(self, client: httpx.Client | None) -> None:
        self.client = client or httpx.Client(
            timeout=_HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "evm"},
        )
        self.owned = client is None

    def __enter__(self) -> httpx.Client:
        return self.client

    def __exit__(self, *_: object) -> None:
        if self.owned:
            self.client.close()


def _catalog_client(client: httpx.Client | None) -> _CatalogClient:
    return _CatalogClient(client)
