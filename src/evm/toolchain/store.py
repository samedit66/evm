"""User-level storage and registration for Eiffel toolchains."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from evm.errors import EvmError
from evm.filesystem import atomic_write
from evm.toolchain.types import (
    InstallationKind,
    ToolchainInstallation,
    ToolchainPlatform,
    ToolchainSelector,
    current_toolchain_platform,
    executable_name,
)

_INSTALLATION_FILE = "installation.toml"
_LINKED_FILE = "linked.toml"
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_PROBE_TIMEOUT_SECONDS = 15
_GOBO_ALIAS_DIRECTORY = ".evm/toolchain-aliases"


def user_toolchain_root(environment: Mapping[str, str] | None = None) -> Path:
    current_environment = os.environ if environment is None else environment
    override = current_environment.get("EVM_TOOLCHAIN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = current_environment.get("LOCALAPPDATA")
        if not base:
            raise EvmError("LOCALAPPDATA is required to locate the EVM toolchain store")
        return Path(base) / "evm" / "toolchains"
    if _is_macos():
        return Path.home() / "Library" / "Application Support" / "evm" / "toolchains"
    data_home = current_environment.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "evm" / "toolchains"


def managed_installation_directory(
    provider: str,
    revision: str,
    platform: ToolchainPlatform,
    root: Path | None = None,
) -> Path:
    store = root or user_toolchain_root()
    return store / "managed" / provider / revision / platform.identifier


def save_managed_installation(installation: ToolchainInstallation) -> None:
    if installation.kind is not InstallationKind.MANAGED:
        raise EvmError("only managed installations can be stored beside their files")
    metadata = _installation_document(installation)
    atomic_write(installation.root.parent / _INSTALLATION_FILE, metadata)


def register_linked_installation(installation: ToolchainInstallation) -> None:
    if installation.kind is not InstallationKind.LINKED:
        raise EvmError("only linked installations can be registered")
    path = user_toolchain_root() / _LINKED_FILE
    document = _load_document(path)
    entries = document.get("toolchain")
    if entries is None:
        entries = tomlkit.aot()
        document.add("toolchain", entries)
    duplicate = next(
        (
            item
            for item in entries
            if isinstance(item, Mapping) and item.get("root") == str(installation.root)
        ),
        None,
    )
    if duplicate is not None:
        raise EvmError(f"toolchain is already linked: {installation.root}")
    entries.append(_installation_table(installation))
    atomic_write(path, tomlkit.dumps(document).encode())


def list_installations(root: Path | None = None) -> tuple[ToolchainInstallation, ...]:
    store = root or user_toolchain_root()
    installations: list[ToolchainInstallation] = []
    managed = store / "managed"
    if managed.is_dir():
        for metadata_path in sorted(managed.glob("*/*/*/installation.toml")):
            installations.append(_load_installation(metadata_path))
    installations.extend(_load_linked_installations(store / _LINKED_FILE))
    return tuple(sorted(installations, key=_installation_sort_key, reverse=True))


def select_installation(
    selector: ToolchainSelector,
    root: Path | None = None,
) -> ToolchainInstallation:
    platform = current_toolchain_platform()
    candidates = [
        item
        for item in list_installations(root)
        if item.matches(selector) and item.platform.identifier == platform.identifier
    ]
    healthy = [item for item in candidates if item.executable.is_file()]
    if healthy:
        return healthy[0]
    if candidates:
        raise EvmError(f"installed toolchain {selector} is incomplete; run `evm toolchain verify`")
    raise EvmError(f"toolchain {selector} is not installed; run `evm toolchain install {selector}`")


def probe_linked_installation(path: Path) -> ToolchainInstallation:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise EvmError(f"toolchain path is not a directory: {root}")
    platform = current_toolchain_platform()
    provider, executable = _find_toolchain_executable(root, platform)
    revision = _probe_version(provider, executable)
    version = ".".join(revision.split(".")[:2])
    return ToolchainInstallation(
        provider=provider,
        version=version,
        revision=revision,
        platform=platform,
        root=root,
        executable=executable,
        kind=InstallationKind.LINKED,
        source="linked",
    )


def remove_installation(installation: ToolchainInstallation) -> None:
    if installation.kind is InstallationKind.LINKED:
        _remove_linked_registration(installation)
        _remove_gobo_alias(installation)
        return
    managed_root = user_toolchain_root() / "managed"
    installation_directory = installation.root.parent.resolve()
    try:
        installation_directory.relative_to(managed_root.resolve())
    except ValueError as error:
        raise EvmError(f"refusing to remove unmanaged path: {installation_directory}") from error
    _remove_gobo_alias(installation)
    shutil.rmtree(installation_directory)


def toolchain_environment(
    installation: ToolchainInstallation,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    effective_root = gobo_shell_root(installation) if installation.provider == "gobo" else None
    path_entries = _toolchain_path_entries(installation, effective_root)
    existing_path = environment.get("PATH")
    if existing_path:
        path_entries.append(existing_path)
    environment["PATH"] = os.pathsep.join(path_entries)
    environment["EVM_TOOLCHAIN"] = installation.selector
    if installation.provider == "gobo":
        if effective_root is None:
            raise AssertionError("Gobo environment has no effective root")
        environment["GOBO"] = str(effective_root)
        environment["ISE_PLATFORM"] = installation.platform.ise_platform
    elif installation.provider == "ise":
        environment["ISE_EIFFEL"] = str(installation.root)
        environment["ISE_LIBRARY"] = str(installation.root)
        environment["ISE_PLATFORM"] = installation.platform.ise_platform
    return environment


def gobo_shell_root(installation: ToolchainInstallation) -> Path:
    if installation.provider != "gobo":
        raise EvmError("a shell-safe Gobo root requires a Gobo installation")
    root = installation.root.resolve()
    if os.name == "nt" or not _contains_whitespace(root):
        return root
    alias = _gobo_alias_path(installation)
    if _contains_whitespace(alias):
        raise EvmError(
            f"Gobo alias path contains whitespace: {alias}; "
            "set EVM_TOOLCHAIN_ALIAS_HOME to a path without whitespace"
        )
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink() and alias.resolve() == root:
        return alias
    if alias.is_symlink():
        alias.unlink()
    elif os.path.lexists(alias):
        raise EvmError(f"Gobo alias path is occupied by a non-symlink: {alias}")
    try:
        alias.symlink_to(root, target_is_directory=True)
    except FileExistsError:
        if not alias.is_symlink() or alias.resolve() != root:
            raise EvmError(f"Gobo alias could not be created safely: {alias}") from None
    return alias


def render_environment(environment: Mapping[str, str], shell: str) -> str:
    if shell == "dotenv":
        return "".join(f"{name}={_dotenv_quote(value)}\n" for name, value in environment.items())
    if shell == "powershell":
        return "".join(
            f"$env:{name}='{value.replace(chr(39), chr(39) * 2)}'\n"
            for name, value in environment.items()
        )
    if shell in {"bash", "zsh", "sh"}:
        return "".join(
            f"export {name}={shlex.quote(value)}\n" for name, value in environment.items()
        )
    raise EvmError(f"unsupported shell {shell!r}; expected bash, zsh, sh, powershell, or dotenv")


def verify_installation(installation: ToolchainInstallation) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if not installation.root.is_dir():
        diagnostics.append(f"installation root is missing: {installation.root}")
    if not installation.executable.is_file():
        diagnostics.append(f"compiler executable is missing: {installation.executable}")
        return tuple(diagnostics)
    if installation.provider == "gobo":
        try:
            gobo_shell_root(installation)
        except EvmError as error:
            diagnostics.append(str(error))
    if installation.provider == "serpent":
        return tuple(diagnostics)
    try:
        detected_version = _probe_version(installation.provider, installation.executable)
    except EvmError as error:
        diagnostics.append(str(error))
    else:
        if detected_version != installation.revision:
            diagnostics.append(
                f"compiler reports version {detected_version}, expected {installation.revision}"
            )
    return tuple(diagnostics)


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _installation_document(installation: ToolchainInstallation) -> bytes:
    document = tomlkit.document()
    document.add("toolchain", _installation_table(installation))
    return tomlkit.dumps(document).encode()


def _installation_table(installation: ToolchainInstallation) -> tomlkit.items.Table:
    table = tomlkit.table()
    values = {
        "provider": installation.provider,
        "version": installation.version,
        "revision": installation.revision,
        "platform": installation.platform.operating_system,
        "architecture": installation.platform.architecture,
        "archive-platform": installation.platform.archive_platform,
        "ise-platform": installation.platform.ise_platform,
        "root": str(installation.root),
        "executable": str(installation.executable),
        "kind": installation.kind.value,
        "checksum": installation.checksum,
        "source": installation.source,
    }
    for name, value in values.items():
        if value is not None:
            table.add(name, value)
    return table


def _load_installation(path: Path) -> ToolchainInstallation:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("toolchain")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise EvmError(f"cannot read toolchain metadata {path}: {error}") from error
    return _parse_installation(raw, path)


def _load_linked_installations(path: Path) -> list[ToolchainInstallation]:
    if not path.is_file():
        return []
    try:
        entries = tomllib.loads(path.read_text(encoding="utf-8")).get("toolchain", [])
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise EvmError(f"cannot read linked toolchain registry {path}: {error}") from error
    if not isinstance(entries, list):
        raise EvmError(f"toolchain in {path} must be an array of tables")
    return [_parse_installation(item, path) for item in entries]


def _parse_installation(raw: object, path: Path) -> ToolchainInstallation:
    if not isinstance(raw, dict):
        raise EvmError(f"toolchain metadata in {path} must be a table")
    required = (
        "provider",
        "version",
        "revision",
        "platform",
        "architecture",
        "archive-platform",
        "ise-platform",
        "root",
        "executable",
        "kind",
    )
    missing = next((name for name in required if not isinstance(raw.get(name), str)), None)
    if missing is not None:
        raise EvmError(f"toolchain metadata {path}: {missing} must be a string")
    platform = ToolchainPlatform(
        raw["platform"], raw["architecture"], raw["archive-platform"], raw["ise-platform"]
    )
    try:
        kind = InstallationKind(raw["kind"])
    except ValueError as error:
        raise EvmError(f"toolchain metadata {path}: unknown kind {raw['kind']!r}") from error
    return ToolchainInstallation(
        raw["provider"],
        raw["version"],
        raw["revision"],
        platform,
        Path(raw["root"]),
        Path(raw["executable"]),
        kind,
        _optional_string(raw, "checksum", path),
        _optional_string(raw, "source", path),
    )


def _optional_string(raw: dict[str, object], name: str, path: Path) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise EvmError(f"toolchain metadata {path}: {name} must be a string")


def _load_document(path: Path) -> tomlkit.TOMLDocument:
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TOMLKitError) as error:
        raise EvmError(f"cannot read linked toolchain registry {path}: {error}") from error


def _find_toolchain_executable(
    root: Path,
    platform: ToolchainPlatform,
) -> tuple[str, Path]:
    candidates = (
        ("gobo", root / "bin" / executable_name("gobo")),
        (
            "ise",
            root / "studio" / "spec" / platform.ise_platform / "bin" / executable_name("ise"),
        ),
        ("ise", root / "bin" / executable_name("ise")),
    )
    found = [(provider, executable) for provider, executable in candidates if executable.is_file()]
    if len(found) == 1:
        return found[0]
    if not found:
        raise EvmError(f"no supported Eiffel compiler found under {root}")
    raise EvmError(f"multiple Eiffel compilers found under {root}; link a more specific directory")


def _probe_version(provider: str, executable: Path) -> str:
    option = "-version" if provider == "ise" else "--version"
    try:
        completed = subprocess.run(
            [str(executable), option],
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvmError(f"cannot run {executable}: {error}") from error
    output = f"{completed.stdout}\n{completed.stderr}"
    match = _VERSION_RE.search(output)
    if match is None:
        raise EvmError(f"compiler did not report a numeric version: {executable}")
    return match.group(0)


def _toolchain_path_entries(
    installation: ToolchainInstallation,
    effective_root: Path | None = None,
) -> list[str]:
    if installation.provider == "gobo":
        return [str((effective_root or installation.root) / "bin")]
    if installation.provider == "serpent":
        return [str(installation.executable.parent)]
    root = installation.root
    platform = installation.platform.ise_platform
    return [
        str(root / "studio" / "spec" / platform / "bin"),
        str(root / "tools" / "spec" / platform / "bin"),
        str(root / "library" / "gobo" / "spec" / platform / "bin"),
    ]


def _installation_sort_key(installation: ToolchainInstallation) -> tuple[object, ...]:
    version = tuple(int(part) for part in installation.revision.split(".") if part.isdigit())
    managed_priority = installation.kind is InstallationKind.MANAGED
    return installation.provider, version, managed_priority


def _remove_linked_registration(installation: ToolchainInstallation) -> None:
    path = user_toolchain_root() / _LINKED_FILE
    document = _load_document(path)
    entries = document.get("toolchain", [])
    retained = [
        item
        for item in entries
        if not isinstance(item, Mapping) or item.get("root") != str(installation.root)
    ]
    if len(retained) == len(entries):
        raise EvmError(f"linked toolchain is not registered: {installation.root}")
    document["toolchain"] = retained
    atomic_write(path, tomlkit.dumps(document).encode())


def _gobo_alias_path(installation: ToolchainInstallation) -> Path:
    override = os.environ.get("EVM_TOOLCHAIN_ALIAS_HOME")
    alias_root = Path(override).expanduser() if override else Path.home() / _GOBO_ALIAS_DIRECTORY
    fingerprint = hashlib.sha256(str(installation.root.resolve()).encode()).hexdigest()[:12]
    return alias_root / "gobo" / f"{installation.revision}-{fingerprint}"


def _remove_gobo_alias(installation: ToolchainInstallation) -> None:
    if installation.provider != "gobo" or not _contains_whitespace(installation.root):
        return
    alias = _gobo_alias_path(installation)
    if alias.is_symlink() and alias.resolve() == installation.root.resolve():
        alias.unlink()


def _contains_whitespace(path: Path) -> bool:
    return any(character.isspace() for character in str(path))


def _dotenv_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'
