"""Command-line boundary for installing and managing Eiffel toolchains."""

from __future__ import annotations

import json
from pathlib import Path

import click

from evm.cli_support import command_errors
from evm.errors import EvmError
from evm.lockfile import LOCK_NAME, ensure_lock_matches, load_lock
from evm.model import Project
from evm.toolchain.commands import (
    configure_project_toolchains,
    locked_toolchains_for_current_platform,
    project_toolchain_selectors,
    record_installed_toolchains,
)
from evm.toolchain.compilers import compiler_adapter_names
from evm.toolchain.installation import (
    available_artifacts,
    install_locked_toolchain,
    install_toolchain,
)
from evm.toolchain.store import (
    list_installations,
    probe_linked_installation,
    register_linked_installation,
    remove_installation,
    render_environment,
    select_installation,
    toolchain_environment,
    verify_installation,
)
from evm.toolchain.types import (
    ToolchainArtifact,
    ToolchainInstallation,
    ToolchainSelector,
)
from evm.workspace import ProjectContext, load_project_context


@click.group("toolchain")
def toolchain_group() -> None:
    """Install and manage user-level Eiffel toolchains."""


@toolchain_group.command("list")
@click.argument("provider", required=False, type=click.Choice(compiler_adapter_names()))
@click.option("--available", is_flag=True, help="Show releases available for download.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def toolchain_list_command(
    provider: str | None,
    available: bool,
    output_json: bool,
) -> None:
    """List installed toolchains or official releases."""
    if available:
        _list_available_toolchains(provider, output_json)
        return
    _list_installed_toolchains(provider, output_json)


def _list_available_toolchains(provider: str | None, output_json: bool) -> None:
    providers = (provider,) if provider else compiler_adapter_names()
    artifacts = tuple(
        artifact
        for selected_provider in providers
        for artifact in available_artifacts(selected_provider)
    )
    if output_json:
        click.echo(
            json.dumps(
                {"toolchains": [_artifact_data(item) for item in artifacts]},
                sort_keys=True,
            )
        )
        return
    for artifact in artifacts:
        click.echo(
            f"{artifact.provider}@{artifact.version}\t{artifact.channel}\t"
            f"{artifact.platform.identifier}"
        )


def _list_installed_toolchains(provider: str | None, output_json: bool) -> None:
    installations = tuple(
        item for item in list_installations() if provider is None or item.provider == provider
    )
    if output_json:
        click.echo(
            json.dumps(
                {"toolchains": [_installation_data(item) for item in installations]},
                sort_keys=True,
            )
        )
        return
    if not installations:
        click.echo("No EVM-managed or linked toolchains installed.")
        return
    click.echo("TOOLCHAIN\tREVISION\tKIND\tPLATFORM\tLOCATION")
    for installation in installations:
        click.echo(
            f"{installation.selector}\t{installation.revision}\t{installation.kind.value}\t"
            f"{installation.platform.identifier}\t{installation.root}"
        )


@toolchain_group.command("install")
@click.argument("selectors", nargs=-1)
@click.option("--project", "project_mode", is_flag=True, help="Install the project matrix.")
@click.option("--locked", is_flag=True, help="Require exact artifacts from Eiffel.lock.")
@click.option("--offline", is_flag=True, help="Use only the download cache.")
@command_errors
def toolchain_install_command(
    selectors: tuple[str, ...],
    project_mode: bool,
    locked: bool,
    offline: bool,
) -> None:
    """Install toolchains into the shared user store."""
    if project_mode and selectors:
        raise EvmError("SELECTOR and --project are mutually exclusive")
    if locked and not project_mode:
        raise EvmError("--locked requires --project")
    if project_mode:
        project = _require_project(load_project_context())
        installed = _install_project_toolchains(project, locked=locked, offline=offline)
        record_installed_toolchains(project, installed)
    else:
        if not selectors:
            raise EvmError("provide at least one SELECTOR or use --project")
        installed = tuple(
            install_toolchain(ToolchainSelector.parse(value), offline=offline)
            for value in selectors
        )
    for installation in installed:
        click.echo(f"Installed {installation.identity} at {installation.root}")


@toolchain_group.command("use")
@click.argument("selectors", nargs=-1, required=True)
@click.option("--install", "install_missing", is_flag=True, help="Install configured releases.")
@command_errors
def toolchain_use_command(selectors: tuple[str, ...], install_missing: bool) -> None:
    """Set the default and compilation matrix for the current project."""
    project = _require_project(load_project_context())
    parsed = tuple(ToolchainSelector.parse(value) for value in selectors)
    proposed, _ = configure_project_toolchains(project, parsed)
    click.echo(f"Updated {proposed.manifest_path}")
    click.echo(f"Updated {proposed.directory / LOCK_NAME}")
    if install_missing:
        installed = []
        for selector in project_toolchain_selectors(proposed):
            installation = install_toolchain(selector)
            installed.append(installation)
            click.echo(f"Installed {installation.identity} at {installation.root}")
        record_installed_toolchains(proposed, tuple(installed))


@toolchain_group.command("link")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@command_errors
def toolchain_link_command(path: Path) -> None:
    """Register an existing EiffelStudio or Gobo installation."""
    installation = probe_linked_installation(path)
    register_linked_installation(installation)
    click.echo(f"Linked {installation.identity} at {installation.root}")


@toolchain_group.command("remove")
@click.argument("selector")
@click.option("--force", is_flag=True, help="Remove a toolchain selected by this project.")
@command_errors
def toolchain_remove_command(selector: str, force: bool) -> None:
    """Remove a managed toolchain or linked registration."""
    installation = select_installation(ToolchainSelector.parse(selector))
    project = _optional_project()
    if not force and project is not None and project.toolchain is not None:
        configured = {ToolchainSelector.parse(item) for item in project.toolchain.matrix}
        if any(installation.matches(item) for item in configured):
            raise EvmError(
                f"{installation.selector} is selected by the current project; use --force"
            )
    remove_installation(installation)
    action = "Unlinked" if installation.kind.value == "linked" else "Removed"
    click.echo(f"{action} {installation.identity}")


@toolchain_group.command("verify")
@click.argument("selector", required=False)
@click.option("--project", "project_mode", is_flag=True, help="Verify the project matrix.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def toolchain_verify_command(
    selector: str | None,
    project_mode: bool,
    output_json: bool,
) -> None:
    """Verify compiler executables and reported versions."""
    if selector is not None and project_mode:
        raise EvmError("SELECTOR and --project are mutually exclusive")
    results: list[dict[str, object]] = []
    failed = False
    for installation in _verification_installations(selector, project_mode):
        diagnostics = verify_installation(installation)
        failed = failed or bool(diagnostics)
        results.append(
            {
                "toolchain": installation.identity,
                "status": "failed" if diagnostics else "passed",
                "diagnostics": list(diagnostics),
            }
        )
        if not output_json:
            click.echo(f"{installation.identity}: {'FAILED' if diagnostics else 'passed'}")
            for diagnostic in diagnostics:
                click.echo(f"  {diagnostic}")
    if output_json:
        click.echo(json.dumps({"status": "failed" if failed else "passed", "toolchains": results}))
    if failed:
        raise click.exceptions.Exit(1)


@toolchain_group.command("env")
@click.argument("selector", required=False)
@click.option(
    "--shell",
    type=click.Choice(["bash", "zsh", "sh", "powershell", "dotenv"]),
    default="sh",
    show_default=True,
)
@command_errors
def toolchain_env_command(selector: str | None, shell: str) -> None:
    """Print environment changes for a selected toolchain."""
    installation = _selected_or_project_installation(selector)
    environment = toolchain_environment(installation, {})
    click.echo(render_environment(environment, shell), nl=False)


def _install_project_toolchains(
    project: Project,
    *,
    locked: bool,
    offline: bool,
) -> tuple[ToolchainInstallation, ...]:
    if locked:
        lock = load_lock(project.directory / LOCK_NAME)
        ensure_lock_matches(project, lock)
        locked_toolchains = locked_toolchains_for_current_platform(lock)
        if not locked_toolchains:
            raise EvmError("Eiffel.lock has no toolchain artifact for the current platform")
        return tuple(install_locked_toolchain(item, offline=offline) for item in locked_toolchains)
    return tuple(
        install_toolchain(selector, offline=offline)
        for selector in project_toolchain_selectors(project)
    )


def _verification_installations(
    selector: str | None,
    project_mode: bool,
) -> tuple[ToolchainInstallation, ...]:
    if selector is not None:
        return (select_installation(ToolchainSelector.parse(selector)),)
    if project_mode:
        project = _require_project(load_project_context())
        return tuple(select_installation(item) for item in project_toolchain_selectors(project))
    installations = list_installations()
    if not installations:
        raise EvmError("no EVM-managed or linked toolchains installed")
    return installations


def _selected_or_project_installation(selector: str | None) -> ToolchainInstallation:
    if selector is not None:
        return select_installation(ToolchainSelector.parse(selector))
    project = _optional_project()
    if project is not None and project.toolchain is not None:
        return select_installation(ToolchainSelector.parse(project.toolchain.default))
    installations = list_installations()
    if len(installations) == 1:
        return installations[0]
    raise EvmError("select a toolchain explicitly or configure toolchain.default")


def _optional_project() -> Project | None:
    try:
        return load_project_context().project
    except EvmError as error:
        if "Eiffel.toml not found" in str(error):
            return None
        raise


def _require_project(context: ProjectContext) -> Project:
    if context.project is not None:
        return context.project
    raise EvmError("this command requires a package; run it inside a workspace member")


def _installation_data(installation: ToolchainInstallation) -> dict[str, object]:
    return {
        "provider": installation.provider,
        "version": installation.version,
        "revision": installation.revision,
        "kind": installation.kind.value,
        "platform": installation.platform.identifier,
        "root": str(installation.root),
        "executable": str(installation.executable),
        "checksum": installation.checksum,
        "source": installation.source,
    }


def _artifact_data(artifact: ToolchainArtifact) -> dict[str, object]:
    return {
        "provider": artifact.provider,
        "version": artifact.version,
        "revision": artifact.revision,
        "channel": artifact.channel,
        "platform": artifact.platform.identifier,
        "url": artifact.url,
        "checksum": artifact.checksum,
    }
