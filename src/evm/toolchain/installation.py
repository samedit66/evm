"""Official catalog resolution and atomic toolchain installation."""

from __future__ import annotations

import hashlib
import os
import platform as system_platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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
    verify_installation,
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
_SERPENT_COMMIT_URL = "https://api.github.com/repos/samedit66/serpent/commits/main"
_SERPENT_ARCHIVE_URL = "https://github.com/samedit66/serpent/archive/{revision}.zip"
_SERPENT_VERSION = "0.1.0"
_LIBERTY_REPOSITORY = "https://git.savannah.gnu.org/git/liberty-eiffel.git"
_LIBERTY_VERSION = "0.0"
_EIFFEL_INSTALL_SCRIPT_URL = "https://www.eiffel.org/setup/install.sh"
_EIFFEL_CDN_URL = "https://www.eiffel.com/cdn/EiffelStudio"
_EIFFEL_ARCHIVE_URL = "https://ftp.eiffel.com/pub/download"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 60
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_MINIMUM_BISON_VERSION = (3, 7)
_SERPENT_PARSER_RESOURCE = "build/eiffelp"


@dataclass(frozen=True)
class InstallationProgress:
    stage: str
    message: str
    completed: int | None = None
    total: int | None = None


ProgressReporter = Callable[[InstallationProgress], None]


def _report(
    reporter: ProgressReporter | None,
    stage: str,
    message: str,
    completed: int | None = None,
    total: int | None = None,
) -> None:
    if reporter is not None:
        reporter(InstallationProgress(stage, message, completed, total))


def available_artifacts(
    provider: str,
    client: httpx.Client | None = None,
    platform: ToolchainPlatform | None = None,
    progress: ProgressReporter | None = None,
) -> tuple[ToolchainArtifact, ...]:
    current_platform = platform or current_toolchain_platform()
    _report(progress, "catalog", f"Checking {provider} releases", 0)
    if provider == "liberty":
        artifacts = (_liberty_artifact("latest", current_platform),)
        _report(progress, "catalog", f"Checked {provider} releases", 1)
        return artifacts
    with _catalog_client(client) as catalog:
        if provider == "gobo":
            artifacts = _gobo_artifacts(catalog, current_platform)
        elif provider == "ise":
            artifacts = _eiffel_artifacts(catalog, current_platform)
        elif provider == "serpent":
            artifacts = (_serpent_artifact(catalog, current_platform),)
        else:
            raise EvmError(
                f"unknown toolchain provider {provider!r}; "
                "known providers: ise, gobo, serpent, liberty"
            )
        available = (
            artifacts
            if provider == "ise"
            else tuple(
                artifact
                for artifact in artifacts
                if _artifact_url_is_available(catalog, artifact.url)
            )
        )
    _report(progress, "catalog", f"Checked {provider} releases", 1)
    return available


def resolve_artifact(
    selector: ToolchainSelector,
    client: httpx.Client | None = None,
    platform: ToolchainPlatform | None = None,
) -> ToolchainArtifact:
    if selector.provider == "liberty":
        return _liberty_artifact(selector.requested_version, platform)
    if selector.provider == "serpent" and selector.requested_version != "latest":
        return _serpent_revision_artifact(selector.requested_version, platform)
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
    progress: ProgressReporter | None = None,
) -> ToolchainInstallation:
    _report(progress, "resolve", f"Resolving {selector}")
    if selector.provider == "liberty":
        return _install_liberty(selector, offline, platform, progress)
    if selector.provider == "serpent":
        _report(progress, "prerequisites", "Checking Serpent build prerequisites")
        _ensure_serpent_build_prerequisites()
    artifact = _resolve_install_artifact(selector, offline, client, platform)
    destination = managed_installation_directory(
        artifact.provider, artifact.revision, artifact.platform
    )
    lock = FileLock(str(destination) + ".lock")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        existing = _verified_existing_installation(destination, progress)
        if existing is not None:
            return existing
        archive, checksum = _obtain_archive(artifact, offline, client, progress)
        return _extract_installation(artifact, archive, checksum, destination, progress)


def install_locked_toolchain(
    locked: LockedToolchain,
    *,
    offline: bool = False,
    client: httpx.Client | None = None,
    progress: ProgressReporter | None = None,
) -> ToolchainInstallation:
    _report(progress, "resolve", f"Resolving locked {locked.provider}@{locked.revision}")
    if locked.provider == "liberty":
        return _install_liberty(
            ToolchainSelector("liberty", locked.revision), offline, None, progress
        )
    if locked.provider == "serpent":
        _report(progress, "prerequisites", "Checking Serpent build prerequisites")
        _ensure_serpent_build_prerequisites()
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
        existing = _verified_existing_installation(destination, progress)
        if existing is not None:
            return existing
        archive, checksum = _obtain_archive(artifact, offline, client, progress)
        return _extract_installation(artifact, archive, checksum, destination, progress)


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


def _serpent_artifact(
    client: httpx.Client,
    platform: ToolchainPlatform,
) -> ToolchainArtifact:
    payload = _get(client, _SERPENT_COMMIT_URL).json()
    revision = payload.get("sha") if isinstance(payload, Mapping) else None
    if not isinstance(revision, str):
        raise EvmError("Serpent commit catalog did not report a revision")
    return _serpent_revision_artifact(revision, platform)


def _serpent_revision_artifact(
    revision: str,
    platform: ToolchainPlatform | None,
) -> ToolchainArtifact:
    if re.fullmatch(r"[0-9a-f]{7,40}", revision) is None:
        raise EvmError("Serpent selectors must use latest or a 7-40 character Git commit")
    current_platform = platform or current_toolchain_platform()
    return ToolchainArtifact(
        "serpent",
        _SERPENT_VERSION,
        revision,
        current_platform,
        _SERPENT_ARCHIVE_URL.format(revision=revision),
        f"serpent-{revision}.zip",
    )


def _liberty_artifact(
    requested_revision: str,
    platform: ToolchainPlatform | None,
) -> ToolchainArtifact:
    revision = _resolve_liberty_revision() if requested_revision == "latest" else requested_revision
    if re.fullmatch(r"[0-9a-f]{7,40}", revision) is None:
        raise EvmError("Liberty selectors must use latest or a 7-40 character Git commit")
    return ToolchainArtifact(
        "liberty",
        _LIBERTY_VERSION,
        revision,
        platform or current_toolchain_platform(),
        f"{_LIBERTY_REPOSITORY}#{revision}",
        f"liberty-{revision}.git",
    )


def _resolve_liberty_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "ls-remote", _LIBERTY_REPOSITORY, "refs/heads/master"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise EvmError(f"cannot query Liberty repository: {error}") from error
    revision = completed.stdout.partition("\t")[0].strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        details = completed.stderr.strip() or "master revision was not reported"
        raise EvmError(f"cannot resolve Liberty latest revision: {details}")
    return revision


def _install_liberty(
    selector: ToolchainSelector,
    offline: bool,
    platform: ToolchainPlatform | None,
    progress: ProgressReporter | None,
) -> ToolchainInstallation:
    _report(progress, "prerequisites", "Checking Liberty build prerequisites")
    _ensure_liberty_store_is_supported(user_toolchain_root())
    artifact = _liberty_artifact(selector.requested_version, platform)
    destination = managed_installation_directory(
        artifact.provider, artifact.revision, artifact.platform
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination) + ".lock"):
        existing = _verified_existing_installation(destination, progress)
        if existing is not None:
            return existing
        if offline:
            raise EvmError("Liberty cannot be installed offline without an existing checkout")
        return _bootstrap_liberty(artifact, destination, progress)


def _ensure_liberty_store_is_supported(store: Path) -> None:
    if not any(character.isspace() for character in str(store)):
        return
    raise EvmError(
        f"Liberty cannot be installed under a path containing whitespace: {store}; "
        "set EVM_TOOLCHAIN_HOME to a whitespace-free directory, for example "
        '`export EVM_TOOLCHAIN_HOME="$HOME/.local/share/evm/toolchains"`'
    )


def _bootstrap_liberty(
    artifact: ToolchainArtifact,
    destination: Path,
    progress: ProgressReporter | None,
) -> ToolchainInstallation:
    missing_tools = [name for name in ("git", "bash", "gcc", "g++") if shutil.which(name) is None]
    if missing_tools:
        raise EvmError(f"Liberty build tools were not found: {', '.join(missing_tools)}")
    root = destination / "root"
    try:
        destination.mkdir()
        root.mkdir()
        _report(progress, "fetch", "Fetching Liberty source")
        _run_install_command(["git", "init", str(root)])
        _run_install_command(
            ["git", "-C", str(root), "remote", "add", "origin", _LIBERTY_REPOSITORY]
        )
        _run_install_command(
            ["git", "-C", str(root), "fetch", "--depth", "1", "origin", artifact.revision],
            progress=progress,
            progress_message="Fetching Liberty revision",
        )
        _run_install_command(["git", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"])
        home = root / ".home"
        home.mkdir()
        environment = os.environ.copy()
        environment.update({"HOME": str(home), "CC": "gcc", "CXX": "g++"})
        _run_install_command(
            ["bash", str(root / "install.sh"), "-plain", "-bootstrap"],
            working_directory=root,
            environment=environment,
            progress=progress,
            progress_message="Bootstrapping Liberty; this can take several minutes",
        )
        installation = ToolchainInstallation(
            artifact.provider,
            artifact.version,
            artifact.revision,
            artifact.platform,
            root,
            root / "target" / "bin" / executable_name("liberty"),
            InstallationKind.MANAGED,
            source=artifact.url,
        )
        _report(progress, "verify", "Verifying the Liberty compiler")
        _verify_new_installation(installation, environment)
        save_managed_installation(installation)
        _report(progress, "complete", "Liberty installation verified")
        return installation
    except (EvmError, OSError) as error:
        diagnostic_log = _preserve_liberty_log(root, destination)
        shutil.rmtree(destination, ignore_errors=True)
        if diagnostic_log is not None:
            raise EvmError(f"{error}\nFull log: {diagnostic_log}") from error
        raise


def _preserve_liberty_log(root: Path, destination: Path) -> Path | None:
    logs = tuple((root / "target" / "log").glob("install-*.log"))
    if not logs:
        return None
    latest = max(logs, key=lambda path: path.stat().st_mtime)
    preserved = destination.parent / f"{destination.name}-install.log"
    try:
        shutil.copy2(latest, preserved)
    except OSError:
        return None
    return preserved


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
        resolved_version = _gobo_release_version(version, revision, channel)
        digest = raw.get("digest")
        checksum = digest if isinstance(digest, str) and digest.startswith("sha256:") else None
        return ToolchainArtifact(
            "gobo", resolved_version, revision, platform, url, filename, checksum, channel
        )
    return None


def _gobo_release_version(version: str, revision: str, channel: str) -> str:
    if _VERSION_RE.fullmatch(version) is not None:
        return version
    if channel == "nightly" and _VERSION_RE.fullmatch(revision) is not None:
        return ".".join(revision.split(".")[:2])
    raise EvmError(
        f"Gobo {channel} artifact does not resolve to a numeric version: "
        f"version={version!r}, revision={revision!r}"
    )


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
        filename = _eiffel_archive_filename(version, build, platform)
        if channel == "stable":
            urls = (
                f"{_EIFFEL_CDN_URL}/{version}/{build}/{filename}",
                f"{_EIFFEL_ARCHIVE_URL}/{version}/{filename}",
            )
        elif channel == "beta":
            urls = (f"https://ftp.eiffel.com/pub/beta/{version}/{filename}",)
        else:
            urls = (f"https://ftp.eiffel.com/pub/beta/nightly/{filename}",)
        url = next(
            (candidate for candidate in urls if _artifact_url_is_available(client, candidate)), None
        )
        if url is None:
            continue
        artifact = ToolchainArtifact(
            "ise", version, revision, platform, url, filename, channel=channel
        )
        if artifact not in artifacts:
            artifacts.append(artifact)
    return tuple(artifacts)


def _eiffel_archive_filename(
    version: str,
    build: str,
    platform: ToolchainPlatform,
) -> str:
    extension = ".7z" if platform.operating_system == "windows" else ".tar.bz2"
    return f"Eiffel_{version}_rev_{build}-{platform.ise_platform}{extension}"


def _artifact_url_is_available(client: httpx.Client, url: str) -> bool:
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            next(response.iter_bytes(1), b"")
        return True
    except httpx.HTTPError:
        return False


def _script_value(script: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=([^\s#]+)", script, re.MULTILINE)
    if match is None:
        raise EvmError(f"EiffelStudio catalog does not define {name}")
    return match.group(1)


def _artifact_matches(artifact: ToolchainArtifact, requested: str) -> bool:
    if requested in {"latest", "stable"}:
        return artifact.channel == "stable"
    if requested in {"beta", "nightly"}:
        return artifact.channel == requested
    return requested in {artifact.version, artifact.revision}


def _obtain_archive(
    artifact: ToolchainArtifact,
    offline: bool,
    client: httpx.Client | None,
    progress: ProgressReporter | None,
) -> tuple[Path, str]:
    cache = _download_cache()
    archive = cache / artifact.filename
    checksum_file = archive.with_suffix(archive.suffix + ".sha256")
    if archive.is_file() and checksum_file.is_file():
        checksum = checksum_file.read_text(encoding="ascii").strip()
        if _sha256(archive) == checksum and _checksum_matches(artifact, checksum):
            _report(progress, "download", "Using verified cached archive")
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
            total = _response_size(response)
            downloaded = 0
            _report(progress, "download", f"Downloading {artifact.filename}", 0, total)
            digest = hashlib.sha256()
            with temporary.open("wb") as output:
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_SIZE):
                    digest.update(chunk)
                    output.write(chunk)
                    downloaded += len(chunk)
                    _report(
                        progress,
                        "download",
                        f"Downloading {artifact.filename}",
                        downloaded,
                        total,
                    )
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
    progress: ProgressReporter | None,
) -> ToolchainInstallation:
    staging_parent = destination.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=staging_parent))
    try:
        extracted = staging / "extracted"
        extracted.mkdir()
        _report(progress, "extract", f"Extracting {artifact.filename}")
        _extract_archive(archive, extracted)
        if artifact.provider == "serpent":
            return _install_serpent_distribution(
                artifact, extracted, checksum, destination, progress
            )
        distribution_root = _distribution_root(artifact.provider, extracted, artifact.platform)
        destination.mkdir()
        installed_root = destination / "root"
        os.replace(distribution_root, installed_root)
        executable = _compiler_path(artifact.provider, installed_root, artifact.platform)
        _ensure_executable(executable)
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
        _report(progress, "verify", f"Verifying {artifact.provider} compiler")
        diagnostics = verify_installation(installation)
        if diagnostics:
            raise EvmError("toolchain verification failed: " + "; ".join(diagnostics))
        save_managed_installation(installation)
        _report(progress, "complete", f"Installed {artifact.identity}")
        return installation
    except EvmError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile, py7zr.Bad7zFile) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise EvmError(f"cannot install {artifact.identity}: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _install_serpent_distribution(
    artifact: ToolchainArtifact,
    extracted: Path,
    checksum: str,
    destination: Path,
    progress: ProgressReporter | None,
) -> ToolchainInstallation:
    source_roots = [path.parent for path in extracted.glob("*/pyproject.toml")]
    if len(source_roots) != 1:
        raise EvmError(
            f"Serpent archive must contain one Python project; found {len(source_roots)}"
        )
    python = _find_serpent_python()
    if python is None:
        raise EvmError("Serpent requires Python 3.13 or newer; no compatible Python was found")
    bison = _ensure_serpent_build_prerequisites()
    environment = _serpent_build_environment(bison)
    destination.mkdir()
    installed_root = destination / "root"
    _report(progress, "bootstrap", "Creating the Serpent Python environment")
    _run_install_command([python, "-m", "venv", str(installed_root)])
    executable = _venv_python(installed_root)
    _run_install_command(
        [str(executable), "-m", "pip", "install", str(source_roots[0])],
        environment=environment,
        progress=progress,
        progress_message="Building and installing Serpent",
    )
    _report(progress, "bootstrap", "Installing the Serpent native parser")
    _install_serpent_parser(source_roots[0], installed_root)
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
    _report(progress, "verify", "Verifying the Serpent Python environment")
    _verify_new_installation(installation)
    save_managed_installation(installation)
    _report(progress, "complete", "Serpent installation verified")
    return installation


def _install_serpent_parser(source_root: Path, installed_root: Path) -> None:
    built_parser = source_root / "serpent" / "resources" / _SERPENT_PARSER_RESOURCE
    if not built_parser.is_file():
        raise EvmError(f"Serpent build did not produce its native parser: {built_parser}")
    resources = _installed_serpent_resources(installed_root)
    installed_parser = resources / _SERPENT_PARSER_RESOURCE
    installed_parser.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built_parser, installed_parser)
    _ensure_executable(installed_parser)


def _installed_serpent_resources(installed_root: Path) -> Path:
    candidates = [installed_root / "Lib" / "site-packages" / "serpent" / "resources"]
    candidates.extend((installed_root / "lib").glob("python*/site-packages/serpent/resources"))
    existing = [path for path in candidates if path.is_dir()]
    if len(existing) != 1:
        raise EvmError(
            "Serpent installation must contain one package resource directory; "
            f"found {len(existing)}"
        )
    return existing[0]


def _serpent_build_environment(bison: str) -> dict[str, str]:
    environment = os.environ.copy()
    path_entries = [str(Path(bison).parent)]
    existing_path = environment.get("PATH")
    if existing_path:
        path_entries.append(existing_path)
    environment["PATH"] = os.pathsep.join(path_entries)
    return environment


def _response_size(response: httpx.Response) -> int | None:
    raw_size = response.headers.get("Content-Length")
    if raw_size is None or not raw_size.isdigit():
        return None
    return int(raw_size)


def _ensure_executable(executable: Path) -> None:
    if os.name == "nt":
        return
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _verify_new_installation(
    installation: ToolchainInstallation,
    environment: Mapping[str, str] | None = None,
) -> None:
    if installation.provider == "serpent":
        _run_install_command(
            [
                str(installation.executable),
                "-c",
                (
                    "import os; from serpent.resources import get_resource_path; "
                    f"parser = get_resource_path('{_SERPENT_PARSER_RESOURCE}'); "
                    "assert parser.is_file() and os.access(parser, os.X_OK)"
                ),
            ]
        )
        return
    if installation.provider == "liberty":
        _run_install_command(
            [str(installation.executable), "-version"],
            working_directory=installation.root,
            environment=environment,
        )


def _run_install_command(
    command: list[str],
    *,
    working_directory: Path | None = None,
    environment: Mapping[str, str] | None = None,
    progress: ProgressReporter | None = None,
    progress_message: str | None = None,
) -> None:
    if progress_message is not None:
        _run_install_command_with_heartbeat(
            command, working_directory, environment, progress, progress_message
        )
        return
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise EvmError(f"cannot run {' '.join(command)}: {error}") from error
    if completed.returncode != 0:
        details = _command_failure_details(completed.stdout, completed.stderr)
        raise EvmError(f"toolchain installation command failed: {' '.join(command)}\n{details}")


def _run_install_command_with_heartbeat(
    command: list[str],
    working_directory: Path | None,
    environment: Mapping[str, str] | None,
    progress: ProgressReporter | None,
    message: str,
) -> None:
    started = time.monotonic()
    _report(progress, "bootstrap", message, 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        raise EvmError(f"cannot run {' '.join(command)}: {error}") from error
    while True:
        try:
            stdout, stderr = process.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            elapsed = int(time.monotonic() - started)
            _report(progress, "bootstrap", f"{message} · {elapsed}s elapsed", elapsed)
    if process.returncode != 0:
        details = _command_failure_details(stdout, stderr)
        raise EvmError(f"toolchain installation command failed: {' '.join(command)}\n{details}")


def _command_failure_details(stdout: str, stderr: str) -> str:
    output = stderr.strip() or stdout.strip() or "no diagnostic output"
    lines = output.splitlines()
    if len(lines) <= 40:
        return output
    return "output truncated; last 40 lines:\n" + "\n".join(lines[-40:])


def _find_serpent_python() -> str | None:
    for name in ("python3.15", "python3.14", "python3.13", "python3"):
        executable = shutil.which(name)
        if executable is not None and _python_is_compatible(executable):
            return executable
    uv = shutil.which("uv")
    if uv is None:
        return None
    completed = subprocess.run(
        [uv, "python", "find", "3.13", "--system", "--no-python-downloads"],
        check=False,
        capture_output=True,
        text=True,
    )
    executable = completed.stdout.strip()
    if completed.returncode == 0 and executable and _python_is_compatible(executable):
        return executable
    return None


def find_serpent_bison() -> str | None:
    executable = shutil.which("bison")
    if executable is not None and _bison_is_compatible(executable):
        return executable
    homebrew_bison = _homebrew_bison()
    if homebrew_bison is not None and _bison_is_compatible(homebrew_bison):
        return homebrew_bison
    return None


def homebrew_can_install_bison() -> bool:
    return system_platform.system() == "Darwin" and shutil.which("brew") is not None


def install_bison_with_homebrew(
    progress: ProgressReporter | None = None,
) -> str:
    brew = shutil.which("brew")
    if system_platform.system() != "Darwin" or brew is None:
        raise EvmError("GNU Bison 3.7 or newer is required to build Serpent")
    _run_install_command(
        [brew, "install", "bison"],
        progress=progress,
        progress_message="Installing GNU Bison with Homebrew",
    )
    executable = find_serpent_bison()
    if executable is None:
        raise EvmError("Homebrew installed Bison but EVM could not find GNU Bison 3.7 or newer")
    _report(progress, "bison", f"Using GNU Bison at {executable}")
    return executable


def serpent_bison_requirement_error() -> str:
    executable = shutil.which("bison")
    detected = _bison_version(executable) if executable is not None else None
    found = (
        f"found Bison {'.'.join(str(part) for part in detected)} at {executable}"
        if detected is not None
        else "no compatible Bison was found"
    )
    guidance = (
        "On macOS install it with `brew install bison`"
        if system_platform.system() == "Darwin"
        else "Install GNU Bison 3.7 or newer with the system package manager"
    )
    return f"Serpent requires GNU Bison 3.7 or newer; {found}. {guidance}"


def _ensure_serpent_build_prerequisites() -> str:
    missing_tools = [name for name in ("make", "gcc", "flex") if shutil.which(name) is None]
    if missing_tools:
        raise EvmError(f"Serpent build tools were not found: {', '.join(missing_tools)}")
    bison = find_serpent_bison()
    if bison is None:
        raise EvmError(serpent_bison_requirement_error())
    return bison


def _homebrew_bison() -> str | None:
    if system_platform.system() != "Darwin":
        return None
    brew = shutil.which("brew")
    if brew is None:
        return None
    try:
        completed = subprocess.run(
            [brew, "--prefix", "bison"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    prefix = completed.stdout.strip()
    return str(Path(prefix) / "bin" / "bison") if prefix else None


def _bison_is_compatible(executable: str) -> bool:
    version = _bison_version(executable)
    return version is not None and version >= _MINIMUM_BISON_VERSION


def _bison_version(executable: str) -> tuple[int, int] | None:
    try:
        completed = subprocess.run(
            [executable, "--version"], check=False, capture_output=True, text=True
        )
    except OSError:
        return None
    match = re.search(r"bison \(GNU Bison\) (\d+)\.(\d+)", completed.stdout)
    if completed.returncode != 0 or match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def find_serpent_python() -> str | None:
    return _find_serpent_python()


def uv_can_install_serpent_python() -> bool:
    return shutil.which("uv") is not None


def install_serpent_python_with_uv(
    progress: ProgressReporter | None = None,
) -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise EvmError("Serpent requires Python 3.13 or newer; install Python 3.13 and retry")
    _run_install_command(
        [uv, "python", "install", "3.13"],
        progress=progress,
        progress_message="Installing Python 3.13 with uv",
    )
    completed = subprocess.run(
        [uv, "python", "find", "3.13", "--system", "--no-python-downloads"],
        check=False,
        capture_output=True,
        text=True,
    )
    executable = completed.stdout.strip()
    if completed.returncode != 0 or not executable or not _python_is_compatible(executable):
        details = completed.stderr.strip() or "uv did not report a compatible interpreter"
        raise EvmError(f"Python 3.13 was installed but cannot be located: {details}")
    _report(progress, "python", f"Using Python at {executable}")
    return executable


def _python_is_compatible(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "--version"], check=False, capture_output=True, text=True
        )
    except OSError:
        return False
    match = re.search(r"Python (\d+)\.(\d+)", completed.stdout + completed.stderr)
    return match is not None and (int(match.group(1)), int(match.group(2))) >= (3, 13)


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


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


def _verified_existing_installation(
    destination: Path,
    progress: ProgressReporter | None,
) -> ToolchainInstallation | None:
    installation = _existing_installation(destination)
    if installation is None:
        return None
    _report(progress, "verify", "Verifying existing installation")
    diagnostics = verify_installation(installation)
    if diagnostics:
        raise EvmError(
            f"existing installation {installation.identity} is not usable: "
            + "; ".join(diagnostics)
            + f"; remove it with `evm toolchain remove {installation.selector}` and retry"
        )
    _report(progress, "complete", "Existing installation verified")
    return installation


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
    if selector.provider == "serpent":
        return _cached_serpent_artifacts(selector, platform, cache)
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


def _cached_serpent_artifacts(
    selector: ToolchainSelector,
    platform: ToolchainPlatform,
    cache: Path,
) -> tuple[ToolchainArtifact, ...]:
    artifacts = []
    for archive in sorted(cache.glob("serpent-*.zip"), reverse=True):
        revision = archive.stem.removeprefix("serpent-")
        artifact = _serpent_revision_artifact(revision, platform)
        if selector.requested_version in {"latest", revision}:
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
