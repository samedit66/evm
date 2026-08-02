from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from evm.cli import main
from evm.errors import EvmError
from evm.lockfile import LockedToolchain, LockFile, load_lock, serialize_lock
from evm.manifest import load_manifest
from evm.project.creation import ProjectCreationRequest, create_project
from evm.toolchain import installation as installation_module
from evm.toolchain.cli import InstallationProgressRenderer
from evm.toolchain.commands import (
    configure_project_toolchains,
    locked_toolchains_for_current_platform,
    project_toolchain_selectors,
    record_installed_toolchains,
)
from evm.toolchain.installation import (
    InstallationProgress,
    available_artifacts,
    find_serpent_bison,
    install_bison_with_homebrew,
    install_locked_toolchain,
    install_serpent_python_with_uv,
    install_toolchain,
    resolve_artifact,
)
from evm.toolchain.selection import toolchain_environment_values
from evm.toolchain.store import (
    gobo_shell_root,
    legacy_toolchain_roots,
    list_installations,
    probe_linked_installation,
    register_linked_installation,
    remove_installation,
    render_environment,
    save_managed_installation,
    select_installation,
    toolchain_environment,
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
)


def test_selector_accepts_provider_and_exact_version() -> None:
    provider = ToolchainSelector.parse("GOBO")
    exact = ToolchainSelector.parse("ise@25.12.98922")
    serpent = ToolchainSelector.parse("serpent@c95ab517")

    assert provider.provider == "gobo"
    assert provider.requested_version == "latest"
    assert str(provider) == "gobo"
    assert exact.version == "25.12.98922"
    assert str(exact) == "ise@25.12.98922"
    assert serpent.version == "c95ab517"


@pytest.mark.parametrize("value", ["", "gec", "gobo@", "gobo >=26", "ise/path"])
def test_selector_rejects_ambiguous_values(value: str) -> None:
    with pytest.raises(EvmError, match="invalid toolchain selector"):
        ToolchainSelector.parse(value)


@pytest.mark.parametrize(
    ("system", "machine", "identifier", "ise_platform"),
    [
        ("Linux", "AMD64", "linux-x86_64", "linux-x86-64"),
        ("Linux", "aarch64", "linux-arm64", "linux-arm64"),
        ("Darwin", "x86_64", "macos-x86_64", "macosx-x86-64"),
        ("Darwin", "arm64", "macos-arm64", "macosx-armv6"),
        ("Windows", "x64", "windows-x86_64", "win64"),
        ("Windows", "armv8", "windows-arm64", "win64"),
    ],
)
def test_platform_normalization(
    system: str,
    machine: str,
    identifier: str,
    ise_platform: str,
) -> None:
    result = current_toolchain_platform(system, machine)

    assert result.identifier == identifier
    assert result.ise_platform == ise_platform


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Plan9", "x86_64"), ("Linux", "sparc")],
)
def test_platform_rejects_unsupported_targets(system: str, machine: str) -> None:
    with pytest.raises(EvmError, match="unsupported toolchain"):
        current_toolchain_platform(system, machine)


def test_user_store_honors_explicit_override(tmp_path: Path) -> None:
    assert user_toolchain_root({"EVM_TOOLCHAIN_HOME": str(tmp_path)}) == tmp_path


def test_user_store_uses_platform_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.store.os", SimpleNamespace(name="nt"))
    assert user_toolchain_root({"LOCALAPPDATA": str(tmp_path)}) == (tmp_path / "evm" / "toolchains")
    with pytest.raises(EvmError, match="LOCALAPPDATA"):
        user_toolchain_root({})

    monkeypatch.setattr("evm.toolchain.store.os", SimpleNamespace(name="posix"))
    monkeypatch.setattr("evm.toolchain.store.platform.system", lambda: "Linux")
    assert user_toolchain_root({"XDG_DATA_HOME": str(tmp_path)}) == (
        tmp_path / "evm" / "toolchains"
    )
    monkeypatch.setattr("evm.toolchain.store.platform.system", lambda: "Darwin")
    assert user_toolchain_root({}) == Path.home() / ".local/share/evm/toolchains"
    assert legacy_toolchain_roots({}) == (
        Path.home() / "Library/Application Support/evm/toolchains",
    )


def test_store_discovers_and_removes_legacy_macos_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    legacy_store = home / "Library/Application Support/evm/toolchains"
    installation = _managed_installation(legacy_store, "gobo", "26.06", "26.06.30")
    save_managed_installation(installation)
    monkeypatch.delenv("EVM_TOOLCHAIN_HOME", raising=False)
    monkeypatch.setattr("evm.toolchain.store.Path.home", lambda: home)
    monkeypatch.setattr("evm.toolchain.store.platform.system", lambda: "Darwin")

    assert list_installations() == (installation,)

    remove_installation(installation)

    assert list_installations() == ()


def test_managed_installation_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(store))
    installation = _managed_installation(store, "gobo", "26.06", "26.06.30")

    save_managed_installation(installation)
    loaded = list_installations()

    assert loaded == (installation,)
    assert select_installation(ToolchainSelector.parse("gobo@26.06")) == installation

    remove_installation(installation)
    assert list_installations() == ()


def test_store_rejects_wrong_registration_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    managed = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    linked = ToolchainInstallation(
        managed.provider,
        managed.version,
        managed.revision,
        managed.platform,
        managed.root,
        managed.executable,
        InstallationKind.LINKED,
    )

    with pytest.raises(EvmError, match="only managed"):
        save_managed_installation(linked)
    with pytest.raises(EvmError, match="only linked"):
        register_linked_installation(managed)


def test_store_refuses_to_delete_managed_record_outside_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    installation = _installation(
        tmp_path / "outside",
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )

    with pytest.raises(EvmError, match="refusing to remove unmanaged"):
        remove_installation(installation)


def test_selection_reports_missing_and_incomplete_installations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    with pytest.raises(EvmError, match="is not installed"):
        select_installation(ToolchainSelector.parse("ise@25.12"))

    installation = _managed_installation(
        tmp_path / "store", "ise", "25.12", "25.12.98922", executable=False
    )
    save_managed_installation(installation)
    with pytest.raises(EvmError, match="incomplete"):
        select_installation(ToolchainSelector.parse("ise@25.12"))


def test_link_probe_registration_and_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    root = tmp_path / "gobo"
    _version_executable(root / "bin" / "gec", "Gobo Eiffel Compiler 26.06.30")

    installation = probe_linked_installation(root)
    register_linked_installation(installation)

    assert installation.kind is InstallationKind.LINKED
    assert list_installations() == (installation,)
    with pytest.raises(EvmError, match="already linked"):
        register_linked_installation(installation)

    remove_installation(installation)
    assert list_installations() == ()
    assert root.is_dir()


def test_link_probe_rejects_invalid_roots(tmp_path: Path) -> None:
    with pytest.raises(EvmError, match="not a directory"):
        probe_linked_installation(tmp_path / "missing")
    with pytest.raises(EvmError, match="no supported Eiffel compiler"):
        probe_linked_installation(tmp_path)


def test_link_probe_rejects_ambiguous_and_unversioned_compilers(tmp_path: Path) -> None:
    platform = current_toolchain_platform()
    _version_executable(tmp_path / "bin" / "gec", "gobo without version")
    with pytest.raises(EvmError, match="numeric version"):
        probe_linked_installation(tmp_path)

    _version_executable(tmp_path / "bin" / "gec", "gobo 26.06")
    _version_executable(
        tmp_path / "studio" / "spec" / platform.ise_platform / "bin" / "ec",
        "ise 25.12",
    )
    with pytest.raises(EvmError, match="multiple Eiffel compilers"):
        probe_linked_installation(tmp_path)


def test_toolchain_environment_for_gobo_and_ise(tmp_path: Path) -> None:
    platform = current_toolchain_platform()
    gobo = _installation(tmp_path / "gobo", "gobo", "26.06", "26.06.30", platform)
    ise = _installation(tmp_path / "ise", "ise", "25.12", "25.12.98922", platform)

    gobo_environment = toolchain_environment(gobo, {"PATH": "/usr/bin"})
    ise_environment = toolchain_environment(ise, {})

    assert gobo_environment["GOBO"] == str(gobo.root)
    assert gobo_environment["PATH"].endswith(os.pathsep + "/usr/bin")
    assert gobo_environment["EVM_TOOLCHAIN"] == "gobo@26.06"
    assert gobo_environment["ISE_PLATFORM"] == platform.ise_platform
    assert "GOBO_CC" not in gobo_environment
    assert ise_environment["ISE_EIFFEL"] == str(ise.root)
    assert ise_environment["ISE_LIBRARY"] == str(ise.root)
    assert ise_environment["ISE_PLATFORM"] == platform.ise_platform


def test_gobo_environment_replaces_incompatible_ise_platform(tmp_path: Path) -> None:
    platform = current_toolchain_platform()
    installation = _installation(tmp_path / "gobo", "gobo", "26.06", "26.06.30", platform)

    environment = toolchain_environment(
        installation,
        {
            "PATH": "/usr/bin",
            "ISE_PLATFORM": "linux-x86-64",
            "GOBO_CC": "zig",
        },
    )

    assert environment["ISE_PLATFORM"] == platform.ise_platform
    assert environment["GOBO_CC"] == "zig"


def test_gobo_environment_uses_space_free_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Gobo Distribution" / "root"
    installation = _installation(
        root,
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )
    root.mkdir(parents=True)
    alias_home = tmp_path / "aliases"
    monkeypatch.setenv("EVM_TOOLCHAIN_ALIAS_HOME", str(alias_home))

    environment = toolchain_environment(installation, {"PATH": "/usr/bin"})
    alias = Path(environment["GOBO"])

    assert " " not in str(alias)
    assert alias.is_symlink()
    assert alias.resolve() == root.resolve()
    assert environment["PATH"].split(os.pathsep)[0] == str(alias / "bin")
    assert gobo_shell_root(installation) == alias


def test_linked_gobo_environment_uses_space_free_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Linked Gobo" / "root"
    managed = _installation(
        root,
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )
    installation = ToolchainInstallation(
        managed.provider,
        managed.version,
        managed.revision,
        managed.platform,
        managed.root,
        managed.executable,
        InstallationKind.LINKED,
    )
    root.mkdir(parents=True)
    monkeypatch.setenv("EVM_TOOLCHAIN_ALIAS_HOME", str(tmp_path / "aliases"))

    alias = Path(toolchain_environment(installation, {})["GOBO"])

    assert alias.is_symlink()
    assert alias.resolve() == root.resolve()
    assert root.is_dir()


def test_compiler_environment_preserves_system_path_and_gobo_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _installation(
        tmp_path / "gobo",
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )
    monkeypatch.setattr("evm.toolchain.selection.list_installations", lambda: (installation,))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("GOBO_CC", "gcc")
    monkeypatch.setenv("ISE_PLATFORM", "linux-x86-64")

    environment = dict(toolchain_environment_values([str(installation.executable)]))

    assert environment["PATH"].endswith("/usr/local/bin:/usr/bin")
    assert environment["GOBO_CC"] == "gcc"
    assert environment["ISE_PLATFORM"] == installation.platform.ise_platform


def test_gobo_alias_repairs_stale_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Gobo Distribution" / "root"
    root.mkdir(parents=True)
    installation = _installation(
        root,
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )
    monkeypatch.setenv("EVM_TOOLCHAIN_ALIAS_HOME", str(tmp_path / "aliases"))
    alias = gobo_shell_root(installation)
    wrong_root = tmp_path / "wrong"
    wrong_root.mkdir()
    alias.unlink()
    alias.symlink_to(wrong_root, target_is_directory=True)

    repaired = gobo_shell_root(installation)

    assert repaired == alias
    assert repaired.resolve() == root.resolve()


def test_gobo_alias_refuses_to_replace_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Gobo Distribution" / "root"
    root.mkdir(parents=True)
    installation = _installation(
        root,
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )
    monkeypatch.setenv("EVM_TOOLCHAIN_ALIAS_HOME", str(tmp_path / "aliases"))
    alias = gobo_shell_root(installation)
    alias.unlink()
    alias.write_text("occupied")

    with pytest.raises(EvmError, match="occupied by a non-symlink"):
        gobo_shell_root(installation)


def test_removing_managed_gobo_removes_its_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store with space"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(store))
    monkeypatch.setenv("EVM_TOOLCHAIN_ALIAS_HOME", str(tmp_path / "aliases"))
    installation = _managed_installation(store, "gobo", "26.06", "26.06.30")
    save_managed_installation(installation)
    alias = Path(toolchain_environment(installation, {})["GOBO"])

    remove_installation(installation)

    assert not os.path.lexists(alias)


def test_verify_reports_unsafe_gobo_alias_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Gobo Distribution" / "root"
    installation = _installation(
        root,
        "gobo",
        "26.06",
        "26.06.30",
        current_toolchain_platform(),
    )
    _version_executable(installation.executable, "gobo 26.06.30")
    monkeypatch.setenv("EVM_TOOLCHAIN_ALIAS_HOME", str(tmp_path / "alias home"))

    diagnostics = verify_installation(installation)

    assert "alias path contains whitespace" in "\n".join(diagnostics)


def test_environment_renderers_quote_values() -> None:
    environment = {"GOBO": "/path with space/'quoted'"}

    assert render_environment(environment, "sh").startswith("export GOBO=")
    assert "$env:GOBO=" in render_environment(environment, "powershell")
    assert render_environment(environment, "dotenv") == ("GOBO=\"/path with space/'quoted'\"\n")
    with pytest.raises(EvmError, match="unsupported shell"):
        render_environment(environment, "fish")


def test_verify_reports_missing_and_changed_compiler(tmp_path: Path) -> None:
    platform = current_toolchain_platform()
    missing = _installation(tmp_path / "missing", "gobo", "26.06", "26.06.30", platform)
    assert "root is missing" in "\n".join(verify_installation(missing))

    root = tmp_path / "gobo"
    executable = root / "bin" / "gec"
    _version_executable(executable, "Gobo Eiffel Compiler 26.07.1")
    changed = ToolchainInstallation(
        "gobo",
        "26.06",
        "26.06.30",
        platform,
        root,
        executable,
        InstallationKind.LINKED,
    )
    assert "reports version 26.07.1" in "\n".join(verify_installation(changed))

    executable.write_text("not executable")
    executable.chmod(0o644)
    assert "cannot run" in "\n".join(verify_installation(changed))


def test_verify_runs_liberty_in_managed_environment(tmp_path: Path) -> None:
    root = tmp_path / "liberty"
    executable = root / "target" / "bin" / "se"
    expected_home = root / ".home"
    expected_home.mkdir(parents=True)
    configuration = expected_home / ".config" / "liberty-eiffel"
    configuration.parent.mkdir()
    configuration.write_text("configured")
    executable.parent.mkdir(parents=True)
    executable.write_text(f'#!/bin/sh\ntest "$HOME" = "{expected_home}" || exit 2\nexit 0\n')
    executable.chmod(0o755)
    installation = ToolchainInstallation(
        "liberty",
        "0.0",
        "21b0813",
        current_toolchain_platform(),
        root,
        executable,
        InstallationKind.MANAGED,
    )

    assert verify_installation(installation) == ()


@pytest.mark.parametrize(
    ("root_name", "configuration_exists", "expected"),
    [
        ("liberty with space", True, "path contains whitespace"),
        ("liberty", False, "managed configuration is missing"),
    ],
)
def test_verify_reports_unsupported_liberty_layout(
    tmp_path: Path,
    root_name: str,
    configuration_exists: bool,
    expected: str,
) -> None:
    root = tmp_path / root_name
    executable = root / "target" / "bin" / "se"
    _version_executable(executable, "Liberty Eiffel")
    if configuration_exists:
        configuration = root / ".home" / ".config" / "liberty-eiffel"
        configuration.parent.mkdir(parents=True)
        configuration.write_text("configured")
    installation = ToolchainInstallation(
        "liberty",
        "0.0",
        "21b0813",
        current_toolchain_platform(),
        root,
        executable,
        InstallationKind.MANAGED,
    )

    assert expected in "\n".join(verify_installation(installation))


def test_verify_reports_broken_serpent_environment(tmp_path: Path) -> None:
    root = tmp_path / "serpent"
    executable = root / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho 'missing serpent' >&2\nexit 1\n")
    executable.chmod(0o755)
    installation = ToolchainInstallation(
        "serpent",
        "0.1.0",
        "c95ab51",
        current_toolchain_platform(),
        root,
        executable,
        InstallationKind.MANAGED,
    )

    diagnostics = verify_installation(installation)

    assert "Serpent Python environment is not usable" in diagnostics[0]
    assert "missing serpent" in diagnostics[0]


def test_gobo_catalog_filters_platform_asset() -> None:
    platform = current_toolchain_platform("Linux", "x86_64")
    releases = [
        {
            "tag_name": "gobo-26.06",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "gobo-linux-x86_64-26.06.30.tar.xz",
                    "browser_download_url": "https://example.test/gobo.tar.xz",
                    "digest": "sha256:abc",
                },
                {
                    "name": "gobo-windows-x86_64-26.06.30.7z",
                    "browser_download_url": "https://example.test/gobo.7z",
                },
            ],
        },
        {"tag_name": "draft", "draft": True, "assets": []},
    ]
    client = _json_client(releases)

    artifacts = available_artifacts("gobo", client, platform)

    assert len(artifacts) == 1
    assert artifacts[0].revision == "26.06.30"
    assert artifacts[0].checksum == "sha256:abc"


def test_eiffel_catalog_resolves_channels_and_exact_revision() -> None:
    script = "\n".join(
        (
            "ISE_MAJOR_MINOR_LATEST=25.12",
            "ISE_BUILD_LATEST=98922",
            "ISE_MAJOR_MINOR_BETA=26.01",
            "ISE_BUILD_BETA=100001",
            "ISE_MAJOR_MINOR_NIGHTLY=26.01",
            "ISE_BUILD_NIGHTLY=100002",
        )
    )
    client = _text_client(script)
    platform = current_toolchain_platform("Linux", "x86_64")

    artifacts = available_artifacts("ise", client, platform)
    nightly = resolve_artifact(ToolchainSelector.parse("ise@nightly"), client, platform)
    exact = resolve_artifact(ToolchainSelector.parse("ise@25.12.98922"), client, platform)

    assert [item.channel for item in artifacts] == ["stable", "beta", "nightly"]
    assert nightly.revision == "26.01.100002"
    assert exact.url.endswith("/25.12/98922/Eiffel_25.12_rev_98922-linux-x86-64.tar.bz2")


def test_serpent_catalog_resolves_latest_and_exact_commit() -> None:
    revision = "c95ab517a5914ebc9e8d2ccf767a6d2e6caf49f2"
    platform = current_toolchain_platform("Linux", "x86_64")

    latest = available_artifacts("serpent", _json_client({"sha": revision}), platform)
    exact = resolve_artifact(ToolchainSelector.parse(f"serpent@{revision}"), platform=platform)

    assert latest[0].version == "0.1.0"
    assert latest[0].revision == revision
    assert exact.url.endswith(f"/{revision}.zip")


@pytest.mark.parametrize(
    ("version", "compatible"),
    [("2.3", False), ("3.6.4", False), ("3.7", True), ("3.8.2", True)],
)
def test_serpent_requires_modern_gnu_bison(
    version: str,
    compatible: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: "/usr/bin/bison" if name == "bison" else None,
    )
    monkeypatch.setattr("evm.toolchain.installation.system_platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "evm.toolchain.installation.subprocess.run",
        lambda command, **options: SimpleNamespace(
            returncode=0,
            stdout=f"bison (GNU Bison) {version}\n",
            stderr="",
        ),
    )

    result = find_serpent_bison()

    assert (result is not None) is compatible


def test_serpent_finds_keg_only_homebrew_bison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.installation.system_platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: {"bison": "/usr/bin/bison", "brew": "/opt/homebrew/bin/brew"}.get(name),
    )

    def run(command: list[str], **options: object) -> SimpleNamespace:
        if command == ["/opt/homebrew/bin/brew", "--prefix", "bison"]:
            return SimpleNamespace(returncode=0, stdout="/opt/homebrew/opt/bison\n", stderr="")
        version = "3.8.2" if command[0].startswith("/opt/homebrew/opt") else "2.3"
        return SimpleNamespace(
            returncode=0,
            stdout=f"bison (GNU Bison) {version}\n",
            stderr="",
        )

    monkeypatch.setattr("evm.toolchain.installation.subprocess.run", run)

    assert find_serpent_bison() == "/opt/homebrew/opt/bison/bin/bison"


@pytest.mark.parametrize(
    ("system", "executable", "expected"),
    [
        ("Darwin", "/usr/bin/bison", "found Bison 2.3 at /usr/bin/bison"),
        ("Linux", None, "system package manager"),
    ],
)
def test_serpent_bison_error_is_actionable(
    system: str,
    executable: str | None,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.installation.system_platform.system", lambda: system)
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: executable if name == "bison" else None,
    )
    monkeypatch.setattr(
        "evm.toolchain.installation.subprocess.run",
        lambda command, **options: SimpleNamespace(
            returncode=0,
            stdout="bison (GNU Bison) 2.3\n",
            stderr="",
        ),
    )

    message = installation_module.serpent_bison_requirement_error()

    assert expected in message


def test_homebrew_bison_install_is_explicit_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr("evm.toolchain.installation.system_platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
    )
    monkeypatch.setattr(
        "evm.toolchain.installation._run_install_command",
        lambda command, **options: commands.append(command),
    )
    monkeypatch.setattr(
        "evm.toolchain.installation.find_serpent_bison",
        lambda: "/opt/homebrew/opt/bison/bin/bison",
    )

    executable = install_bison_with_homebrew()

    assert executable == "/opt/homebrew/opt/bison/bin/bison"
    assert commands == [["/opt/homebrew/bin/brew", "install", "bison"]]


def test_homebrew_bison_install_requires_homebrew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.installation.system_platform.system", lambda: "Darwin")
    monkeypatch.setattr("evm.toolchain.installation.shutil.which", lambda name: None)

    with pytest.raises(EvmError, match=r"GNU Bison 3\.7"):
        install_bison_with_homebrew()


def test_homebrew_bison_install_verifies_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.installation.system_platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
    )
    monkeypatch.setattr(
        "evm.toolchain.installation._run_install_command",
        lambda command, **options: None,
    )
    monkeypatch.setattr("evm.toolchain.installation.find_serpent_bison", lambda: None)

    with pytest.raises(EvmError, match="could not find GNU Bison"):
        install_bison_with_homebrew()


def test_serpent_install_creates_isolated_python_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "c95ab517a5914ebc9e8d2ccf767a6d2e6caf49f2"
    archive = _serpent_zip(revision)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=archive))
    )
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: f"/tools/{name}",
    )
    monkeypatch.setattr(
        "evm.toolchain.installation._find_serpent_python", lambda: "/tools/python3.13"
    )
    monkeypatch.setattr("evm.toolchain.installation.find_serpent_bison", lambda: "/tools/bison")

    build_environments: list[dict[str, str]] = []

    def run_install(command: list[str], **options: object) -> None:
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python")
        if command[1:4] == ["-m", "pip", "install"]:
            build_environments.append(options["environment"])

    monkeypatch.setattr("evm.toolchain.installation._run_install_command", run_install)

    installation = install_toolchain(ToolchainSelector.parse(f"serpent@{revision}"), client=client)

    assert installation.provider == "serpent"
    assert installation.version == "0.1.0"
    assert installation.revision == revision
    assert installation.executable.name == "python"
    assert build_environments[0]["PATH"].startswith("/tools")
    assert list_installations() == (installation,)


def test_uv_python_install_returns_verified_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_commands: list[list[str]] = []
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: "/tools/uv" if name == "uv" else None,
    )
    monkeypatch.setattr(
        "evm.toolchain.installation._run_install_command",
        lambda command, **options: install_commands.append(command),
    )

    def run(command: list[str], **options: object) -> SimpleNamespace:
        if command[:3] == ["/tools/uv", "python", "find"]:
            return SimpleNamespace(returncode=0, stdout="/managed/python3.13\n", stderr="")
        if command == ["/managed/python3.13", "--version"]:
            return SimpleNamespace(returncode=0, stdout="Python 3.13.7\n", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("evm.toolchain.installation.subprocess.run", run)

    executable = install_serpent_python_with_uv()

    assert executable == "/managed/python3.13"
    assert install_commands == [["/tools/uv", "python", "install", "3.13"]]


def test_long_install_command_reports_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[InstallationProgress] = []

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.polls = 0

        def communicate(self, timeout: int) -> tuple[str, str]:
            self.polls += 1
            if self.polls == 1:
                raise installation_module.subprocess.TimeoutExpired("build", timeout)
            return "done", ""

    process = Process()
    monkeypatch.setattr(installation_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monotonic = iter((10.0, 12.0))
    monkeypatch.setattr(installation_module.time, "monotonic", lambda: next(monotonic))

    installation_module._run_install_command(
        ["build"],
        progress=events.append,
        progress_message="Building toolchain",
    )

    assert [event.completed for event in events] == [0, 2]
    assert events[-1].message.endswith("2s elapsed")


def test_liberty_install_bootstraps_exact_git_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "21b081378ec12798080128e7f39878d5d2097cb7"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def run_install(command: list[str], **options: object) -> None:
        if command[0] == "bash":
            root = Path(options["working_directory"])
            executable = root / "target" / "bin" / "se"
            executable.parent.mkdir(parents=True)
            executable.write_text("se")

    monkeypatch.setattr("evm.toolchain.installation._run_install_command", run_install)

    installation = install_toolchain(ToolchainSelector.parse(f"liberty@{revision}"))

    assert installation.provider == "liberty"
    assert installation.revision == revision
    assert installation.executable == installation.root / "target" / "bin" / "se"
    assert (installation.root / ".home").is_dir()


def test_liberty_rejects_whitespace_store_before_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "21b081378ec12798080128e7f39878d5d2097cb7"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "Application Support"))
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "evm.toolchain.installation._run_install_command",
        lambda command, **options: commands.append(command),
    )

    with pytest.raises(EvmError, match="path containing whitespace"):
        install_toolchain(ToolchainSelector.parse(f"liberty@{revision}"))

    assert commands == []


def test_liberty_failure_preserves_bootstrap_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "21b081378ec12798080128e7f39878d5d2097cb7"
    store = tmp_path / "store"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(store))
    monkeypatch.setattr(
        "evm.toolchain.installation.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def fail_bootstrap(command: list[str], **options: object) -> None:
        if command[0] != "bash":
            return
        root = Path(options["working_directory"])
        log = root / "target" / "log" / "install-test.log"
        log.parent.mkdir(parents=True)
        log.write_text("bootstrap failed")
        raise EvmError("bootstrap command failed")

    monkeypatch.setattr("evm.toolchain.installation._run_install_command", fail_bootstrap)

    with pytest.raises(EvmError, match="Full log:"):
        install_toolchain(ToolchainSelector.parse(f"liberty@{revision}"))

    preserved = tuple(store.glob("managed/liberty/*/*-install.log"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "bootstrap failed"
    assert not (preserved[0].parent / current_toolchain_platform().identifier).exists()


def test_catalog_reports_unknown_provider_and_missing_release() -> None:
    platform = current_toolchain_platform("Linux", "x86_64")
    with pytest.raises(EvmError, match="unknown toolchain provider"):
        available_artifacts("other", _json_client([]), platform)
    with pytest.raises(EvmError, match="no gobo artifact matches"):
        resolve_artifact(ToolchainSelector.parse("gobo@1.0"), _json_client([]), platform)


def test_catalog_reports_invalid_and_failed_responses() -> None:
    platform = current_toolchain_platform("Linux", "x86_64")
    with pytest.raises(EvmError, match="invalid response"):
        available_artifacts("gobo", _json_client({"invalid": True}), platform)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
    )
    with pytest.raises(EvmError, match="cannot read toolchain catalog"):
        available_artifacts("gobo", client, platform)


def test_eiffel_catalog_requires_all_release_variables() -> None:
    platform = current_toolchain_platform("Linux", "x86_64")
    with pytest.raises(EvmError, match="does not define ISE_MAJOR_MINOR_LATEST"):
        available_artifacts("ise", _text_client(""), platform)


def test_install_gobo_downloads_verifies_and_reuses_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    archive = _toolchain_tar("gobo", "gobo/bin/gec", "Gobo Eiffel Compiler 26.06.30")
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/releases"):
            return httpx.Response(
                200,
                json=[
                    {
                        "tag_name": "gobo-26.06",
                        "draft": False,
                        "prerelease": False,
                        "assets": [
                            {
                                "name": "gobo-linux-x86_64-26.06.30.tar.gz",
                                "browser_download_url": "https://example.test/gobo.tar.gz",
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(200, content=archive)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    platform = current_toolchain_platform("Linux", "x86_64")

    installed = install_toolchain(
        ToolchainSelector.parse("gobo@26.06"), client=client, platform=platform
    )
    reused = install_toolchain(
        ToolchainSelector.parse("gobo@26.06"), client=client, platform=platform
    )

    assert installed == reused
    assert installed.executable.is_file()
    assert installed.checksum is not None
    assert len([url for url in requests if url.endswith("gobo.tar.gz")]) == 1


def test_install_reports_measurable_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    archive = _toolchain_tar("gobo", "gobo/bin/gec", "Gobo Eiffel Compiler 26.06.30")
    client = _gobo_install_client(archive)
    events: list[InstallationProgress] = []

    install_toolchain(
        ToolchainSelector.parse("gobo"),
        client=client,
        platform=current_toolchain_platform("Linux", "x86_64"),
        progress=events.append,
    )

    stages = [event.stage for event in events]
    downloads = [event for event in events if event.stage == "download"]
    assert stages[0] == "resolve"
    assert {"download", "extract", "verify", "complete"} <= set(stages)
    assert downloads[0].completed == 0
    assert downloads[-1].completed == len(archive)
    assert downloads[-1].total == len(archive)


def test_progress_renderer_emits_stable_noninteractive_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = InstallationProgressRenderer()
    renderer.interactive = False

    renderer(InstallationProgress("download", "Downloading archive", 0, 1024))
    renderer(InstallationProgress("download", "Downloading archive", 512, 1024))
    renderer(InstallationProgress("download", "Downloading archive", 1024, 1024))
    renderer(InstallationProgress("bootstrap", "Building", 0))
    renderer(InstallationProgress("bootstrap", "Building · 1s elapsed", 1))
    renderer(InstallationProgress("complete", "Installed"))

    output = capsys.readouterr().out
    assert "0%" in output
    assert "100%" in output
    assert "50%" not in output
    assert output.count("Building") == 1
    assert "✓ Installed" in output


def test_progress_renderer_updates_interactive_activity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = InstallationProgressRenderer()
    renderer.interactive = True

    renderer(InstallationProgress("download", "Downloading archive", 2048))
    renderer(InstallationProgress("bootstrap", "Building", 1))
    renderer(InstallationProgress("extract", "Extracting"))
    renderer(InstallationProgress("download", "Downloading archive", 1, 2))
    renderer.finish()

    output = capsys.readouterr().out
    assert "2.0 KiB" in output
    assert "Building" in output
    assert "Extracting" in output
    assert "50%" in output
    assert output.endswith("\n")


def test_install_rejects_broken_existing_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    archive = _toolchain_tar("gobo", "gobo/bin/gec", "Gobo Eiffel Compiler 26.06.30")
    client = _gobo_install_client(archive)
    selector = ToolchainSelector.parse("gobo")
    platform = current_toolchain_platform("Linux", "x86_64")
    installed = install_toolchain(selector, client=client, platform=platform)
    installed.executable.write_text("broken")
    installed.executable.chmod(0o644)

    with pytest.raises(EvmError, match=r"existing installation.*is not usable"):
        install_toolchain(selector, client=client, platform=platform)


def test_offline_install_uses_verified_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    cache = tmp_path / "cache"
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(cache))
    cache.mkdir()
    archive = cache / "gobo-linux-x86_64-26.06.30.tar.gz"
    archive.write_bytes(_toolchain_tar("gobo", "gobo/bin/gec", "gobo 26.06.30"))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".gz.sha256").write_text(checksum + "\n")

    installed = install_toolchain(
        ToolchainSelector.parse("gobo@26.06"),
        offline=True,
        platform=current_toolchain_platform("Linux", "x86_64"),
    )

    assert installed.revision == "26.06.30"


def test_offline_install_reports_empty_and_invalid_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    with pytest.raises(EvmError, match="not available in the offline"):
        install_toolchain(ToolchainSelector.parse("gobo@26.06"), offline=True)

    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / "gobo-macos-arm64-26.06.30.tar.gz"
    archive.write_bytes(b"invalid")
    archive.with_suffix(".gz.sha256").write_text("wrong\n")
    with pytest.raises(EvmError, match="cached archive is missing or invalid"):
        install_toolchain(ToolchainSelector.parse("gobo@26.06"), offline=True)


def test_download_rejects_catalog_checksum_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    platform = current_toolchain_platform("Linux", "x86_64")
    releases = [
        {
            "tag_name": "gobo-26.06",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "gobo-linux-x86_64-26.06.30.tar.gz",
                    "browser_download_url": "https://example.test/gobo.tar.gz",
                    "digest": "sha256:wrong",
                }
            ],
        }
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("releases"):
            return httpx.Response(200, json=releases)
        return httpx.Response(200, content=b"archive")

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises(EvmError, match="checksum mismatch"):
        install_toolchain(ToolchainSelector.parse("gobo"), client=client, platform=platform)


def test_install_rejects_unsafe_and_incomplete_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    platform = current_toolchain_platform("Linux", "x86_64")
    unsafe = _single_file_tar("../escape", b"bad")
    incomplete = _single_file_tar("gobo/readme", b"none")

    for content, message in ((unsafe, "unsafe path"), (incomplete, "exactly one gec")):
        client = _gobo_install_client(content)
        with pytest.raises(EvmError, match=message):
            install_toolchain(ToolchainSelector.parse("gobo"), client=client, platform=platform)
        shutil.rmtree(tmp_path / "cache", ignore_errors=True)


def test_install_accepts_zip_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(tmp_path / "cache"))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        executable = zipfile.ZipInfo("gobo/bin/gec")
        executable.external_attr = 0o755 << 16
        archive.writestr(executable, "#!/bin/sh\necho 'gobo 26.06.30'\n")
    platform = current_toolchain_platform("Linux", "x86_64")
    client = _gobo_install_client(stream.getvalue(), filename="gobo-linux-x86_64-26.06.30.zip")

    installation = install_toolchain(
        ToolchainSelector.parse("gobo"), client=client, platform=platform
    )

    assert "gobo 26.06.30" in installation.executable.read_text()


def test_locked_installation_rejects_another_platform() -> None:
    current = current_toolchain_platform()
    other = "windows" if current.operating_system != "windows" else "linux"
    locked = LockedToolchain("gobo", "26.06", "26.06.30", other, "x86_64", "https://x")

    with pytest.raises(EvmError, match="targets"):
        install_locked_toolchain(locked)


def test_locked_installation_uses_exact_cached_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = current_toolchain_platform()
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(tmp_path / "store"))
    cache = tmp_path / "cache"
    monkeypatch.setenv("EVM_TOOLCHAIN_CACHE", str(cache))
    cache.mkdir()
    filename = "gobo-current-26.06.30.tar.gz"
    archive = cache / filename
    archive.write_bytes(_toolchain_tar("gobo", "gobo/bin/gec", "gobo 26.06.30"))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".gz.sha256").write_text(checksum + "\n")
    locked = LockedToolchain(
        "gobo",
        "26.06",
        "26.06.30",
        platform.operating_system,
        platform.architecture,
        f"https://example.test/{filename}",
        f"sha256:{checksum}",
    )

    installation = install_locked_toolchain(locked, offline=True)

    assert installation.identity == "gobo@26.06.30"


def test_manifest_toolchain_configuration_and_validation(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    path = project.manifest_path
    path.write_text(
        path.read_text()
        + '\n[compatibility]\ncompilers = ["ise >=25.12", "gobo >=26.06"]\n'
        + '\n[toolchain]\ndefault = "gobo@26.06"\nmatrix = ["gobo@26.06", "ise@25.12"]\n'
    )

    loaded = load_manifest(path)

    assert loaded.toolchain is not None
    assert loaded.toolchain.default == "gobo@26.06"
    assert loaded.toolchain.matrix == ("gobo@26.06", "ise@25.12")


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ('default = "gobo"', "exact numeric version"),
        ('default = "gobo@latest"', "exact numeric version"),
        ('default = "gobo@26.06"\nmatrix = ["ise@25.12"]', "must appear"),
        (
            'default = "gobo@26.06"\nmatrix = ["gobo@26.06", "gobo@26.06"]',
            "duplicate",
        ),
    ],
)
def test_manifest_rejects_invalid_toolchain_policy(
    tmp_path: Path,
    declaration: str,
    message: str,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[toolchain]\n{declaration}\n"
    )

    with pytest.raises(EvmError, match=message):
        load_manifest(project.manifest_path)


def test_lock_round_trips_toolchain_artifact(tmp_path: Path) -> None:
    locked = LockedToolchain(
        "gobo",
        "26.06",
        "26.06.30",
        "linux",
        "x86_64",
        "https://example.test/gobo.tar.xz",
        "sha256:abc",
    )
    path = tmp_path / "Eiffel.lock"
    path.write_bytes(serialize_lock(LockFile("fingerprint", (), (locked,))))

    loaded = load_lock(path)

    assert loaded.toolchains == (locked,)


def test_lock_supports_multiple_versions_of_one_provider(tmp_path: Path) -> None:
    toolchains = (
        LockedToolchain("gobo", "26.05", "26.05.1", "linux", "x86_64", "https://x/1"),
        LockedToolchain("gobo", "26.06", "26.06.1", "linux", "x86_64", "https://x/2"),
    )
    path = tmp_path / "Eiffel.lock"
    path.write_bytes(serialize_lock(LockFile("fingerprint", (), toolchains)))

    assert load_lock(path).toolchains == toolchains


def test_configure_project_toolchains_writes_exact_policy_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    platform = current_toolchain_platform()

    def resolve(
        selector: ToolchainSelector, client: httpx.Client | None = None
    ) -> ToolchainArtifact:
        version = "25.12" if selector.provider == "ise" else "26.06"
        return ToolchainArtifact(
            selector.provider,
            version,
            version + ".1",
            platform,
            f"https://example.test/{selector.provider}.tar.gz",
            f"{selector.provider}.tar.gz",
        )

    monkeypatch.setattr("evm.toolchain.commands.resolve_artifact", resolve)

    proposed, lock = configure_project_toolchains(
        project,
        (ToolchainSelector.parse("gobo@latest"), ToolchainSelector.parse("ise@25.12")),
    )

    assert proposed.toolchain is not None
    assert proposed.toolchain.matrix == ("gobo@26.06", "ise@25.12")
    assert [item.provider for item in lock.toolchains] == ["gobo", "ise"]
    assert project_toolchain_selectors(proposed) == (
        ToolchainSelector("gobo", "26.06"),
        ToolchainSelector("ise", "25.12"),
    )


def test_project_configuration_commands_reject_missing_and_duplicate_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    with pytest.raises(EvmError, match="at least one"):
        configure_project_toolchains(project, ())
    with pytest.raises(EvmError, match="does not configure"):
        project_toolchain_selectors(project)

    platform = current_toolchain_platform()
    artifact = ToolchainArtifact("gobo", "26.06", "26.06.30", platform, "x", "x")
    monkeypatch.setattr(
        "evm.toolchain.commands.resolve_artifact", lambda selector, client: artifact
    )
    with pytest.raises(EvmError, match="duplicate releases"):
        configure_project_toolchains(
            project,
            (ToolchainSelector.parse("gobo"), ToolchainSelector.parse("gobo@latest")),
        )


def test_toolchain_cli_lists_links_env_and_removes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    monkeypatch.setenv("EVM_TOOLCHAIN_HOME", str(store))
    root = tmp_path / "gobo"
    _version_executable(root / "bin" / "gec", "Gobo Eiffel Compiler 26.06.30")
    runner = CliRunner()

    linked = runner.invoke(main, ["toolchain", "link", str(root)])
    listed = runner.invoke(main, ["toolchain", "list", "--json"])
    environment = runner.invoke(main, ["toolchain", "env", "gobo@26.06"])
    removed = runner.invoke(main, ["toolchain", "remove", "gobo@26.06"])

    assert linked.exit_code == 0, linked.output
    assert json.loads(listed.output)["toolchains"][0]["kind"] == "linked"
    assert "export GOBO=" in environment.output
    assert removed.exit_code == 0, removed.output
    assert root.is_dir()


def test_toolchain_cli_matrix_build_runs_selected_toolchains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    compiled: list[str | None] = []

    def compile_without_toolchain(project, request, check_only=False, *, announce=True):
        compiled.append(request.compiler)
        return None

    monkeypatch.setattr("evm.cli.compile_project", compile_without_toolchain)

    result = CliRunner().invoke(
        main,
        ["build", "--toolchain", "ise", "--toolchain", "gobo"],
    )

    assert result.exit_code == 0, result.output
    assert compiled == ["ise", "gobo"]


def test_toolchain_all_builds_every_installed_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    older = _managed_installation(tmp_path / "store", "gobo", "26.05", "26.05.1")
    newer = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.1")
    compiled: list[str | None] = []
    monkeypatch.setattr("evm.cli.list_installations", lambda: (newer, older))
    monkeypatch.setattr(
        "evm.cli.compile_project",
        lambda project, request, check_only=False, *, announce=True: compiled.append(
            request.compiler
        ),
    )

    result = CliRunner().invoke(main, ["build", "--toolchain", "all"])

    assert result.exit_code == 0, result.output
    assert compiled == ["gobo@26.06.1", "gobo@26.05.1"]


def test_toolchain_cli_lists_available_releases_in_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = current_toolchain_platform()
    artifact = ToolchainArtifact(
        "gobo",
        "26.06",
        "26.06.30",
        platform,
        "https://example.test/gobo.tar.gz",
        "gobo.tar.gz",
    )
    calls: list[str] = []

    def available(provider: str) -> tuple[ToolchainArtifact, ...]:
        calls.append(provider)
        return (artifact,) if provider == "gobo" else ()

    monkeypatch.setattr("evm.toolchain.cli.available_artifacts", available)
    runner = CliRunner()

    text_result = runner.invoke(main, ["toolchain", "list", "--available"])
    json_result = runner.invoke(main, ["toolchain", "list", "gobo", "--available", "--json"])

    assert "gobo@26.06" in text_result.output
    assert json.loads(json_result.output)["toolchains"][0]["revision"] == "26.06.30"
    assert calls == ["ise", "gobo", "serpent", "liberty", "gobo"]


def test_toolchain_cli_lists_empty_and_text_installations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    runner = CliRunner()
    monkeypatch.setattr("evm.toolchain.cli.list_installations", lambda: ())
    empty = runner.invoke(main, ["toolchain", "list"])
    monkeypatch.setattr("evm.toolchain.cli.list_installations", lambda: (installation,))
    listed = runner.invoke(main, ["toolchain", "list", "gobo"])

    assert "No EVM-managed" in empty.output
    assert "TOOLCHAIN" in listed.output
    assert "gobo@26.06" in listed.output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["toolchain", "install"], "provide at least one"),
        (["toolchain", "install", "gobo", "--project"], "mutually exclusive"),
        (["toolchain", "install", "gobo", "--locked"], "requires --project"),
    ],
)
def test_toolchain_install_cli_validates_modes(arguments: list[str], message: str) -> None:
    result = CliRunner().invoke(main, arguments)

    assert result.exit_code == 1
    assert message in result.output


def test_toolchain_install_cli_offers_uv_python_for_serpent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _managed_installation(tmp_path / "store", "serpent", "0.1.0", "abc1234")
    calls: list[str] = []
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: "/tools/bison")
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_python", lambda: None)
    monkeypatch.setattr("evm.toolchain.cli.uv_can_install_serpent_python", lambda: True)
    monkeypatch.setattr("evm.toolchain.cli._input_is_interactive", lambda: True)
    monkeypatch.setattr(
        "evm.toolchain.cli.install_serpent_python_with_uv",
        lambda progress: calls.append("python") or "/uv/python3.13",
    )
    monkeypatch.setattr(
        "evm.toolchain.cli.install_toolchain",
        lambda selector, *, offline, progress: calls.append(str(selector)) or installation,
    )

    result = CliRunner().invoke(main, ["toolchain", "install", "serpent"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Install Python 3.13 with uv?" in result.output
    assert calls == ["python", "serpent"]


def test_toolchain_install_cli_prepares_bison_before_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _managed_installation(tmp_path / "store", "serpent", "0.1.0", "abc1234")
    calls: list[str] = []
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: None)
    monkeypatch.setattr("evm.toolchain.cli.homebrew_can_install_bison", lambda: True)
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_python", lambda: None)
    monkeypatch.setattr("evm.toolchain.cli.uv_can_install_serpent_python", lambda: True)
    monkeypatch.setattr("evm.toolchain.cli._input_is_interactive", lambda: True)
    monkeypatch.setattr(
        "evm.toolchain.cli.install_bison_with_homebrew",
        lambda progress: calls.append("bison") or "/homebrew/bison",
    )
    monkeypatch.setattr(
        "evm.toolchain.cli.install_serpent_python_with_uv",
        lambda progress: calls.append("python") or "/uv/python3.13",
    )
    monkeypatch.setattr(
        "evm.toolchain.cli.install_toolchain",
        lambda selector, *, offline, progress: calls.append("serpent") or installation,
    )

    result = CliRunner().invoke(
        main,
        ["toolchain", "install", "serpent"],
        input="y\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert calls == ["bison", "python", "serpent"]


def test_toolchain_install_cli_checks_bison_before_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_checks: list[bool] = []
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: None)
    monkeypatch.setattr("evm.toolchain.cli.homebrew_can_install_bison", lambda: False)
    monkeypatch.setattr(
        "evm.toolchain.cli.find_serpent_python",
        lambda: python_checks.append(True),
    )
    monkeypatch.setattr(
        "evm.toolchain.cli.serpent_bison_requirement_error",
        lambda: "Serpent requires GNU Bison 3.7 or newer",
    )

    result = CliRunner().invoke(main, ["toolchain", "install", "serpent"])

    assert result.exit_code == 1
    assert "GNU Bison 3.7" in result.output
    assert python_checks == []


def test_toolchain_install_cli_respects_declined_homebrew_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installs: list[bool] = []
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: None)
    monkeypatch.setattr("evm.toolchain.cli.homebrew_can_install_bison", lambda: True)
    monkeypatch.setattr("evm.toolchain.cli._input_is_interactive", lambda: True)
    monkeypatch.setattr(
        "evm.toolchain.cli.install_bison_with_homebrew",
        lambda progress: installs.append(True),
    )

    result = CliRunner().invoke(
        main,
        ["toolchain", "install", "serpent"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Serpent installation cancelled" in result.output
    assert installs == []


def test_toolchain_install_cli_does_not_install_bison_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: None)

    result = CliRunner().invoke(main, ["toolchain", "install", "serpent", "--offline"])

    assert result.exit_code == 1
    assert "--offline prevents installing it with Homebrew" in result.output


def test_toolchain_install_cli_never_prompts_for_python_in_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: "/tools/bison")
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_python", lambda: None)
    monkeypatch.setattr("evm.toolchain.cli.uv_can_install_serpent_python", lambda: True)
    monkeypatch.setattr("evm.toolchain.cli._input_is_interactive", lambda: False)

    result = CliRunner().invoke(main, ["toolchain", "install", "serpent"])

    assert result.exit_code == 1
    assert "run interactively" in result.output
    assert "[y/N]" not in result.output


def test_toolchain_install_cli_keeps_offline_mode_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_bison", lambda: "/tools/bison")
    monkeypatch.setattr("evm.toolchain.cli.find_serpent_python", lambda: None)

    result = CliRunner().invoke(main, ["toolchain", "install", "serpent", "--offline"])

    assert result.exit_code == 1
    assert "--offline prevents installing it with uv" in result.output


def test_toolchain_install_cli_installs_direct_and_project_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    direct_calls: list[str] = []

    def install(selector: ToolchainSelector, *, offline: bool = False, progress=None):
        direct_calls.append(f"{selector}:{offline}")
        return installation

    monkeypatch.setattr("evm.toolchain.cli.install_toolchain", install)
    runner = CliRunner()
    direct = runner.invoke(main, ["toolchain", "install", "gobo", "--offline"])

    monkeypatch.setattr(
        "evm.toolchain.cli._install_project_toolchains",
        lambda selected_project, *, locked, offline, progress: (installation,),
    )
    project_result = runner.invoke(
        main, ["toolchain", "install", "--project", "--locked", "--offline"]
    )

    assert direct.exit_code == 0, direct.output
    assert project_result.exit_code == 0, project_result.output
    assert direct_calls == ["gobo:True"]


def test_toolchain_use_cli_updates_and_optionally_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    project.manifest_path.write_text(
        project.manifest_path.read_text()
        + '\n[toolchain]\ndefault = "gobo@26.06"\nmatrix = ["gobo@26.06"]\n'
    )
    proposed = load_manifest(project.manifest_path)
    monkeypatch.chdir(project.directory)
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    monkeypatch.setattr(
        "evm.toolchain.cli.configure_project_toolchains",
        lambda selected_project, selectors: (proposed, LockFile("x", ())),
    )
    monkeypatch.setattr(
        "evm.toolchain.cli.install_toolchain",
        lambda selector, *, progress: installation,
    )

    result = CliRunner().invoke(main, ["toolchain", "use", "gobo@latest", "--install"])

    assert result.exit_code == 0, result.output
    assert "Updated" in result.output
    assert "Installed gobo@26.06.30" in result.output


def test_toolchain_remove_cli_protects_project_matrix_and_force_removes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    project.manifest_path.write_text(
        project.manifest_path.read_text()
        + '\n[toolchain]\ndefault = "gobo@26.06"\nmatrix = ["gobo@26.06"]\n'
    )
    monkeypatch.chdir(project.directory)
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    removed: list[str] = []
    monkeypatch.setattr("evm.toolchain.cli.select_installation", lambda selector: installation)
    monkeypatch.setattr(
        "evm.toolchain.cli.remove_installation", lambda selected: removed.append(selected.identity)
    )
    runner = CliRunner()

    protected = runner.invoke(main, ["toolchain", "remove", "gobo@26.06"])
    forced = runner.invoke(main, ["toolchain", "remove", "gobo@26.06", "--force"])

    assert protected.exit_code == 1
    assert "selected by the current project" in protected.output
    assert forced.exit_code == 0
    assert removed == ["gobo@26.06.30"]


def test_toolchain_verify_cli_reports_success_failure_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    monkeypatch.setattr("evm.toolchain.cli.list_installations", lambda: (installation,))
    runner = CliRunner()

    monkeypatch.setattr("evm.toolchain.cli.verify_installation", lambda selected: ())
    passed = runner.invoke(main, ["toolchain", "verify"])
    passed_json = runner.invoke(main, ["toolchain", "verify", "--json"])
    monkeypatch.setattr("evm.toolchain.cli.verify_installation", lambda selected: ("broken",))
    failed = runner.invoke(main, ["toolchain", "verify"])

    assert "passed" in passed.output
    assert json.loads(passed_json.output)["status"] == "passed"
    assert failed.exit_code == 1
    assert "broken" in failed.output


def test_toolchain_verify_cli_validates_selection_and_empty_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    conflict = runner.invoke(main, ["toolchain", "verify", "gobo", "--project"])
    monkeypatch.setattr("evm.toolchain.cli.list_installations", lambda: ())
    empty = runner.invoke(main, ["toolchain", "verify"])

    assert "mutually exclusive" in conflict.output
    assert "no EVM-managed" in empty.output


def test_toolchain_env_cli_uses_single_installation_without_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")
    monkeypatch.setattr("evm.toolchain.cli.list_installations", lambda: (installation,))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["toolchain", "env", "--shell", "dotenv"])

    assert result.exit_code == 0, result.output
    assert "GOBO=" in result.output


def test_matrix_commands_validate_all_and_aggregate_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    monkeypatch.chdir(project.directory)

    def fail_gobo(project, request, check_only=False, *, announce=True):
        if request.compiler == "gobo":
            raise EvmError("gobo failed")
        return None

    monkeypatch.setattr("evm.cli.compile_project", fail_gobo)
    runner = CliRunner()

    failed = runner.invoke(
        main,
        ["check", "--toolchain", "ise", "--toolchain", "gobo", "--json"],
    )
    invalid = runner.invoke(
        main,
        ["build", "--toolchain", "all", "--toolchain", "gobo"],
    )
    configuration = runner.invoke(
        main,
        ["check", "--configuration-only", "--toolchain", "gobo"],
    )

    assert failed.exit_code == 1
    assert json.loads(failed.output)["status"] == "failed"
    assert "cannot be combined" in invalid.output
    assert "cannot be combined" in configuration.output


def test_locked_toolchain_filter_uses_current_platform() -> None:
    platform = current_toolchain_platform()
    matching = LockedToolchain(
        "gobo", "26.06", "26.06.30", platform.operating_system, platform.architecture, "x"
    )
    other = LockedToolchain("gobo", "26.06", "26.06.30", "other", "other", "x")

    assert locked_toolchains_for_current_platform(LockFile("x", (), (matching, other))) == (
        matching,
    )


def test_installed_checksum_is_persisted_in_project_lock(tmp_path: Path) -> None:
    project = create_project(ProjectCreationRequest(tmp_path / "hello"))
    platform = current_toolchain_platform()
    locked = LockedToolchain(
        "gobo",
        "26.06",
        "26.06.30",
        platform.operating_system,
        platform.architecture,
        "https://example.test/gobo.tar.gz",
    )
    project_lock = project.directory / "Eiffel.lock"
    project_lock.write_bytes(serialize_lock(LockFile("fingerprint", (), (locked,))))
    installation = _managed_installation(tmp_path / "store", "gobo", "26.06", "26.06.30")

    updated = record_installed_toolchains(project, (installation,))

    assert updated.toolchains[0].checksum == "sha256:test"
    assert load_lock(project_lock).toolchains[0].checksum == "sha256:test"


def _managed_installation(
    store: Path,
    provider: str,
    version: str,
    revision: str,
    *,
    executable: bool = True,
) -> ToolchainInstallation:
    platform = current_toolchain_platform()
    root = store / "managed" / provider / revision / platform.identifier / "root"
    installation = _installation(root, provider, version, revision, platform)
    if executable:
        _version_executable(installation.executable, f"{provider} {revision}")
    else:
        root.mkdir(parents=True)
    return installation


def _installation(
    root: Path,
    provider: str,
    version: str,
    revision: str,
    platform: ToolchainPlatform,
) -> ToolchainInstallation:
    executable = (
        root / "bin" / "gec"
        if provider == "gobo"
        else root / "studio" / "spec" / platform.ise_platform / "bin" / "ec"
    )
    return ToolchainInstallation(
        provider,
        version,
        revision,
        platform,
        root,
        executable,
        InstallationKind.MANAGED,
        "sha256:test",
        "https://example.test/archive",
    )


def _version_executable(path: Path, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{output}'\n")
    path.chmod(0o755)


def _json_client(value: object) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=value))
    )


def _text_client(value: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=value))
    )


def _toolchain_tar(root: str, executable: str, output: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        content = f"#!/bin/sh\necho '{output}'\n".encode()
        member = tarfile.TarInfo(executable)
        member.mode = 0o755
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def _single_file_tar(name: str, content: bytes) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def _serpent_zip(revision: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w") as archive:
        archive.writestr(f"serpent-{revision}/pyproject.toml", "[project]\nname='serpent'\n")
    return stream.getvalue()


def _gobo_install_client(
    archive: bytes,
    filename: str = "gobo-linux-x86_64-26.06.30.tar.gz",
) -> httpx.Client:
    releases = [
        {
            "tag_name": "gobo-26.06",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": filename,
                    "browser_download_url": f"https://example.test/{filename}",
                }
            ],
        }
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("releases"):
            return httpx.Response(200, json=releases)
        return httpx.Response(200, content=archive)

    return httpx.Client(transport=httpx.MockTransport(handle))
